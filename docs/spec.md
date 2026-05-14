# wlfinder — ТЗ для Claude Code

> Документ-спецификация. Передаётся Claude Code на VPS как контекст для самостоятельной реализации проекта `wlfinder`. После прочтения Claude Code должен последовательно создать структуру, реализовать модули, написать тесты и документацию.

---

## 1. Что строим

Инструмент `wlfinder` решает одну задачу: **найти у одного из российских хостеров VPS, чей публичный IPv4 попадает в "белый список" мобильных операторов РФ, и при удаче автоматически развернуть на нём прокси-сервер**.

Логика — IP-roulette:

1. Загрузить актуальные whitelist'ы (CIDR + IP) из открытых источников.
2. Создать VPS через API выбранного хостера в выбранной локации.
3. Получить выданный публичный IP.
4. Проверить, попадает ли он в whitelist.
5. **Если да** → запустить на нём cloud-init c прокси-сервером (VLESS+Reality по умолчанию), сохранить креды, вывести готовый клиентский конфиг/ссылку, выйти.
6. **Если нет** → удалить сервер, пауза, переход к (2).

Бонусом — копится статистика hit-rate по (хостер, локация, тариф), которая полезна для последующих запусков.

## 2. Целевая платформа и стек

| Слой | Технология | Зачем именно так |
|------|-----------|------------------|
| Язык | Python 3.11+ | широкая адаптация, простой деплой на VPS, лучшая поддержка `typing` |
| Менеджер пакетов | `uv` (или `pip` + `pyproject.toml`) | быстрый, современный |
| Async | `asyncio` + `httpx` | параллельные запросы к хостерам, неблокирующий polling |
| Валидация | `pydantic` v2 | конфиг + DTO + парсинг ответов API |
| CLI | `typer` | type-hints → CLI, без бойлерплейта |
| Логи / вывод | `rich` + `structlog` | красивый stdout + json-логи на диск |
| База | `sqlite` через `aiosqlite` | один файл, никакого DBA |
| Конфиг | YAML (`pyyaml`) + `.env` | разделение секретов и параметров |
| Шаблоны cloud-init | `jinja2` | подстановка ключей/UUID в YAML cloud-init |
| Тесты | `pytest` + `pytest-asyncio` + `respx` | мокаем HTTP, не дёргаем боевые API |
| Linting | `ruff` + `mypy --strict` | строгая типизация обязательна |

Прокси на удачном сервере:
- **Xray-core** с VLESS+Reality (основной, по умолчанию) — `XTLS/Xray-install`
- **Hysteria2** (опциональный fallback) — `apernet/hysteria`

Эти два протокола в 2026 году считаются устойчивыми к ТСПУ DPI.

## 3. Структура репозитория

```
wlfinder/
├── pyproject.toml
├── README.md
├── LICENSE                       # MIT
├── .gitignore
├── .env.example
├── config.example.yaml
├── src/wlfinder/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                    # точка входа Typer
│   ├── config.py                 # Pydantic-модели конфига
│   ├── models.py                 # общие DTO (Server, Attempt, ...)
│   ├── db.py                     # SQLite (миграции, репозитории)
│   ├── checker.py                # CIDR matching
│   ├── orchestrator.py           # главный цикл IP-roulette
│   ├── keeper.py                 # provisioning успешного сервера
│   ├── whitelist/
│   │   ├── __init__.py
│   │   ├── base.py               # protocol WhitelistSource
│   │   ├── github_source.py      # hxehex, openlibrecommunity
│   │   ├── file_source.py        # локальный .txt
│   │   └── store.py              # объединение, кэш, ip_network[]
│   ├── hosters/
│   │   ├── __init__.py
│   │   ├── base.py               # protocol Hoster
│   │   ├── registry.py           # фабрика по имени из конфига
│   │   ├── timeweb.py
│   │   ├── regru.py
│   │   ├── selectel.py
│   │   ├── cloudru.py
│   │   ├── clo.py
│   │   └── onecloud.py
│   ├── proxies/
│   │   ├── __init__.py
│   │   ├── base.py               # protocol ProxyProvisioner
│   │   ├── xray_reality.py       # генерация конфига + cloud-init
│   │   └── hysteria2.py
│   └── templates/
│       ├── xray_reality.cloud-init.yaml.j2
│       └── hysteria2.cloud-init.yaml.j2
└── tests/
    ├── conftest.py
    ├── test_checker.py
    ├── test_whitelist.py
    ├── test_hosters/
    │   ├── test_timeweb.py
    │   ├── test_regru.py
    │   └── test_selectel.py
    └── test_orchestrator.py
```

## 4. Источники whitelist'ов

Загружать из этих репозиториев, кэшировать в `~/.cache/wlfinder/`, TTL по умолчанию 24 часа:

| Источник | URL (raw) | Формат |
|----------|-----------|--------|
| hxehex/CIDR | `https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt` | CIDR, по строке |
| hxehex/IP | `https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/ipwhitelist.txt` | IP, по строке |
| openlibrecommunity/twl | `https://github.com/openlibrecommunity/twl` (структура папок: `cccidrs/`, `subnet/`) | см. README репо |

В `whitelist/store.py`:
- Загрузка из всех включённых источников.
- Нормализация: одинокий IP → `/32`.
- Дедуп через `ipaddress.collapse_addresses`.
- Сериализация в pickle-кэш (`whitelist.pkl`).
- Метод `is_whitelisted(ip: str) -> bool` — O(log n) бинарным поиском по отсортированному списку `ip_network`.

Объединённый список — порядка 10–20 тыс. сетей, держится в памяти спокойно.

## 5. Конфигурация

`config.yaml` (пример):

```yaml
general:
  log_level: INFO
  db_path: ~/.local/share/wlfinder/wlfinder.db
  cache_dir: ~/.cache/wlfinder

whitelist:
  sources:
    - type: github
      name: hxehex-cidr
      url: https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt
    - type: github
      name: hxehex-ip
      url: https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/ipwhitelist.txt
  refresh_ttl_hours: 24

orchestrator:
  max_attempts: 30
  delay_between_attempts_sec: 15
  parallel_workers: 1            # >1 поднимает несколько серверов параллельно
  bail_on_balance_threshold_rub: 50

hosters:
  - name: timeweb-spb
    type: timeweb
    enabled: true
    token_env: TIMEWEB_TOKEN
    preset_id: 4795             # минимальный тариф
    os_id: 99                   # Ubuntu 24.04
    region: ru-1                # СПб
    bandwidth: 100

  - name: regru-msk
    type: regru
    enabled: true
    token_env: REGRU_TOKEN
    size: cloud-1
    image: ubuntu-22-04-amd64
    region_slug: msk1
    ssh_key_fingerprints: []

  - name: selectel-spb
    type: selectel
    enabled: false              # требует service-user, OpenStack
    service_user_env: SELECTEL_SERVICE_USER
    service_pass_env: SELECTEL_SERVICE_PASS
    project_id_env: SELECTEL_PROJECT_ID
    region: ru-2                # СПб
    flavor_name: SL1.1-1024-15
    image_name: Ubuntu 24.04 LTS 64-bit

  - name: cloudru-msk
    type: cloudru
    enabled: false
    key_id_env: CLOUDRU_KEY_ID
    key_secret_env: CLOUDRU_KEY_SECRET
    project_id_env: CLOUDRU_PROJECT_ID

proxy:
  type: xray_reality
  listen_port: 443
  fake_domain: www.microsoft.com   # SNI, под который мимикрируем
  fake_dest: www.microsoft.com:443
  uuid: ""                          # пусто = сгенерировать

output:
  save_client_config: ./out/client.json
  print_share_link: true
  qr_code: true
```

`.env`:
```
TIMEWEB_TOKEN=eyJ...
REGRU_TOKEN=...
SELECTEL_SERVICE_USER=...
SELECTEL_SERVICE_PASS=...
SELECTEL_PROJECT_ID=...
```

Pydantic-модель `Config` валидирует всё, разворачивает `*_env` в `SecretStr`, чекает что enabled-хостеры имеют все нужные креды.

## 6. Интерфейс хостера

```python
# hosters/base.py
from typing import Protocol
from pydantic import BaseModel

class CreatedServer(BaseModel):
    hoster: str
    server_id: str
    public_ipv4: str
    public_ipv6: str | None = None
    region: str
    raw: dict       # сырой ответ, для отладки

class Hoster(Protocol):
    name: str

    async def create(self, *, name: str, ssh_pub_key: str,
                     user_data: str | None) -> CreatedServer: ...

    async def delete(self, server_id: str) -> None: ...

    async def health_check(self) -> bool:
        """ping API, проверка токена/баланса"""

    async def estimate_cost_per_hour(self) -> float | None:
        """опционально, рубли"""
```

Каждая реализация — отдельный класс с конструктором, принимающим конкретный кусок конфига. Регистрация в `registry.py`:

```python
HOSTERS: dict[str, type[Hoster]] = {
    "timeweb": TimewebHoster,
    "regru": RegruHoster,
    "selectel": SelectelHoster,
    "cloudru": CloudRuHoster,
    "clo": CloHoster,
    "1cloud": OneCloudHoster,
}
```

## 7. Хостеры — детали интеграции

### 7.1. Timeweb Cloud

- **Base URL**: `https://api.timeweb.cloud/api/v1`
- **Auth**: `Authorization: Bearer <TIMEWEB_TOKEN>`
- **Документация**: <https://timeweb.cloud/api-docs>

**Создание:**
```
POST /servers
Content-Type: application/json

{
  "name": "wlfinder-probe-<ts>",
  "preset_id": 4795,
  "os_id": 99,
  "bandwidth": 100,
  "is_ddos_guard": false,
  "is_local_network": false,
  "ssh_keys_ids": [<id>],
  "cloud_init": "<base64 cloud-init>"  // если поддерживается, иначе пустой
}
```
Ответ `201` с `{"server": {"id": ..., "networks": [{"type": "public", "ips": [...]}], ...}}`. **IP может быть не сразу** — поллим `GET /servers/{id}` раз в 2с до 60с пока `networks[].ips` не наполнится.

**Удаление:** `DELETE /servers/{id}`

**SSH-ключи:** загрузить отдельным POST в `/ssh-keys` один раз, потом использовать `ssh_keys_ids`.

**Получение preset/os:** `GET /presets/servers`, `GET /os/servers` — кэшировать на запуск.

**Получение баланса:** `GET /account/status` (поле `balance`). Если ниже `bail_on_balance_threshold_rub` — прервать запуск с ошибкой.

### 7.2. REG.ru CloudVPS (Рег.облако)

- **Base URL**: `https://api.cloudvps.reg.ru/v1`
- **Auth**: `Authorization: Bearer <REGRU_TOKEN>` (токен берётся в ЛК → Облачные серверы → Настройки → Токен для API)
- **Документация**: <https://developers.cloudvps.reg.ru/>

**Создание:**
```
POST /reglets
{
  "name": "wlfinder-<ts>",
  "size": "cloud-1",
  "image": "ubuntu-22-04-amd64",
  "ssh_keys": ["<fingerprint>"],
  "user_data": "<cloud-init plain>"
}
```
Ответ — JSON с объектом `reglet`, где сразу есть `ip` и `ipv6`. Поллинг не нужен, кроме случая статуса `new` → ждём `active` (нам важен только IP, который выдаётся сразу).

**Удаление:** `DELETE /reglets/{id}`

**SSH-ключи:** GET/POST `/account/keys`. Регистрировать по fingerprint.

**Опционально:** есть готовая обёртка `pip install regru_cloudapi` (`plvskiy/regru_cloudapi`) — можно посмотреть как пример, но в проде лучше тонкий httpx-клиент, чтобы не зависеть от чужого пакета.

### 7.3. Selectel

- **Auth**: двухступенчатая. Сначала service-user → Keystone token, потом этим токеном — в OpenStack-совместимый API.
- **Документация**:
  - API облака: <https://developers.selectel.ru/docs/selectel-cloud-platform/>
  - Cloud: <https://docs.selectel.ru/en/cloud-servers/>

**Получение токена:**
```
POST https://cloud.api.selcloud.ru/identity/v3/auth/tokens
{
  "auth": {
    "identity": {
      "methods": ["password"],
      "password": {
        "user": {
          "name": "<service_user>",
          "domain": {"name": "<account_id>"},
          "password": "<service_pass>"
        }
      }
    },
    "scope": {"project": {"id": "<project_id>"}}
  }
}
```
Токен в заголовке ответа `X-Subject-Token`. Срок жизни ~часы — кэшируем.

**Создание сервера** (OpenStack Nova):
```
POST https://<region>.cloud.api.selcloud.ru/compute/v2.1/servers
X-Auth-Token: <token>
{
  "server": {
    "name": "wlfinder-<ts>",
    "flavorRef": "<flavor_id>",
    "imageRef": "<image_id>",
    "networks": [{"uuid": "<public_net_id>"}],
    "key_name": "<key_name>",
    "user_data": "<base64 cloud-init>"
  }
}
```

**Удаление:** `DELETE /compute/v2.1/servers/{id}`

**Получение IP:** `GET /compute/v2.1/servers/{id}` → `addresses` (поллить до появления плавающего IP).

Это самый сложный хостер из-за OpenStack. Реализовать в последнюю очередь. Альтернатива — использовать готовый Python SDK `openstacksdk` или `terraform-provider-selectel` через subprocess. **Рекомендую** для первой версии — тонкий клиент на httpx, OpenStack только в части serverов.

### 7.4. Cloud.ru Evolution

- **Документация**: <https://cloud.ru/docs/foundation/ug/topics/api-list.html>
- **Auth**: пара `Key ID` + `Key Secret` → exchange на access_token (OAuth2 client_credentials grant).
- **Базовая сложность** между REG.ru и Selectel.

Эндпоинты:
```
POST https://iam.api.cloud.ru/api/v1/auth/system/openid/token
  client_id=<key_id>&client_secret=<secret>&grant_type=client_credentials
→ access_token

POST https://api.cloud.ru/compute/v1/instances  (тут проверить точный path в swagger)
  Authorization: Bearer <token>
  X-Project-Id: <project_id>
```
В первой итерации можно поставить флаг `enabled: false` для этого хостера в `config.example.yaml` и реализовать после того, как Timeweb/REG.ru заработают.

### 7.5. CLO.ru

- **Документация**: <https://clo.ru/docs/> → раздел API
- **Auth**: API-токен в личном кабинете → Bearer.
- Эндпоинты в стиле OpenStack-обёртки. Аналогично Selectel, но проще.

### 7.6. 1cloud

- **Документация**: <https://1cloud.ru/api>
- **Auth**: bearer-токен.
- `POST /server` для создания. IP в ответе.

## 8. Cloud-init для VLESS+Reality

Шаблон `templates/xray_reality.cloud-init.yaml.j2`:

```yaml
#cloud-config
package_update: true
package_upgrade: false
packages:
  - curl
  - ufw

write_files:
  - path: /usr/local/etc/xray/config.json
    permissions: '0644'
    content: |
      {
        "log": {"loglevel": "warning"},
        "inbounds": [{
          "listen": "0.0.0.0",
          "port": {{ listen_port }},
          "protocol": "vless",
          "settings": {
            "clients": [{"id": "{{ uuid }}", "flow": "xtls-rprx-vision"}],
            "decryption": "none"
          },
          "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
              "show": false,
              "dest": "{{ fake_dest }}",
              "xver": 0,
              "serverNames": ["{{ fake_domain }}"],
              "privateKey": "{{ reality_priv }}",
              "shortIds": ["{{ short_id }}"]
            }
          },
          "sniffing": {"enabled": true, "destOverride": ["http","tls","quic"]}
        }],
        "outbounds": [{"protocol": "freedom"}]
      }

runcmd:
  - bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
  - systemctl enable --now xray
  - ufw --force enable
  - ufw allow 22/tcp
  - ufw allow {{ listen_port }}/tcp
```

**Где взять `reality_priv`/`public`/`short_id`:** сгенерировать локально перед заливкой:
- `private/public`: `xray x25519` (или питоновская обёртка `nacl`/`cryptography`). Можно посчитать на питоне:
  ```python
  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
  from cryptography.hazmat.primitives import serialization
  import base64
  priv = X25519PrivateKey.generate()
  priv_b = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
  pub_b = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
  priv_b64 = base64.urlsafe_b64encode(priv_b).rstrip(b"=").decode()
  pub_b64  = base64.urlsafe_b64encode(pub_b).rstrip(b"=").decode()
  ```
- `short_id`: `secrets.token_hex(8)`.
- `uuid`: `uuid.uuid4()` либо из конфига.

**Клиентская ссылка** (выводится в конце успеха):
```
vless://<uuid>@<ip>:<port>?security=reality&encryption=none&pbk=<pub_b64>&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=<fake_domain>&sid=<short_id>#wlfinder-<ip>
```

Сохранять также `client.json` для импорта в v2rayN/Nekobox.

## 9. Главный цикл (orchestrator)

Псевдокод:

```python
async def run(cfg: Config):
    wl = await load_whitelist(cfg.whitelist)
    hosters = build_hosters(cfg.hosters)         # только enabled
    proxy = build_proxy(cfg.proxy)               # генерит cloud-init + креды
    ssh_pair = ensure_local_ssh_key()            # ~/.ssh/wlfinder

    for attempt in range(cfg.orchestrator.max_attempts):
        hoster = pick_hoster(hosters, attempt)   # round-robin
        await check_balance_or_bail(hoster, cfg)
        user_data = proxy.render_cloud_init()
        server = await hoster.create(name=f"wlfinder-{ts()}",
                                      ssh_pub_key=ssh_pair.pub,
                                      user_data=user_data)
        db.record_attempt(hoster.name, server, ...)
        hit = wl.is_whitelisted(server.public_ipv4)
        if hit:
            log.info(f"HIT! {server.public_ipv4} on {hoster.name}")
            await wait_for_cloud_init(server)    # SSH probe :443 или 22
            client = proxy.build_client(server.public_ipv4)
            save_outputs(client, cfg.output)
            return
        else:
            log.info(f"miss {server.public_ipv4} on {hoster.name}")
            await hoster.delete(server.server_id)
            await asyncio.sleep(cfg.orchestrator.delay_between_attempts_sec)

    raise NoHitError("Exceeded max_attempts")
```

Ключевые детали:
- Все логи и попытки пишутся в SQLite — потом можно строить статистику.
- `pick_hoster` — round-robin или взвешенный (вес можно класть в конфиг).
- `wait_for_cloud_init` — пробуем TCP `:443` (наш VLESS) с таймаутом 90с после удачного create.
- Если cloud-init не успел и порт молчит — это не повод выкидывать сервер, можно ретраить probe.
- При SIGINT (Ctrl-C) во время работы — гарантировано удаляем последний созданный сервер. `try/finally` или `atexit`.

## 10. Префильтр по ASN (оптимизация)

До любых попыток создания серверов — посчитать пересечение анонсируемых хостером префиксов с whitelist'ом. Если у Timeweb (например) **0 префиксов** в whitelist, нет смысла туда стучаться.

Источник BGP-префиксов: `ipverse/asn-ip` на GitHub.
- AS Timeweb: 9123, 197695 (проверить, может измениться)
- AS REG.ru / Reg.Cloud: 197695, 47593
- AS Selectel: 49505, 50340
- AS Cloud.ru / Sbercloud: 199524, 209156

CLI-команда `wlfinder asn-stats` — для каждого включённого хостера выводит:
```
timeweb (AS9123, AS197695):
  total announced /24: 1024
  in whitelist:        37  (3.6%)
  matched prefixes: 87.250.224.0/20, 92.53.96.0/19, ...
```

Это даёт честную оценку вероятности hit на этого хостера ещё до трат денег.

## 11. CLI

```
wlfinder init                    # копирует config.example.yaml → config.yaml
wlfinder whitelist update        # форсит обновление кэша
wlfinder whitelist stats         # сколько сетей, source breakdown
wlfinder hoster ping             # проверка токенов всех enabled
wlfinder asn-stats               # пересечение хостеров с whitelist
wlfinder run                     # основной IP-roulette
wlfinder run --dry-run           # без реальных create — только проверка пайплайна
wlfinder run --hoster timeweb-spb --max-attempts 5
wlfinder destroy --all           # снести все wlfinder-* серверы (паник-кнопка)
wlfinder stats                   # hit-rate по хостерам из истории SQLite
```

## 12. База данных (SQLite)

```sql
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                 -- ISO8601 UTC
  hoster TEXT NOT NULL,
  region TEXT,
  server_id TEXT NOT NULL,
  ipv4 TEXT NOT NULL,
  ipv6 TEXT,
  hit INTEGER NOT NULL,             -- 0/1
  deleted INTEGER NOT NULL DEFAULT 0,
  cost_estimate_rub REAL,
  raw_create TEXT,                  -- json
  notes TEXT
);

CREATE INDEX idx_attempts_hoster_hit ON attempts(hoster, hit);

CREATE TABLE whitelist_cache_meta (
  source_name TEXT PRIMARY KEY,
  last_fetched TEXT NOT NULL,
  network_count INTEGER NOT NULL,
  sha256 TEXT NOT NULL
);

CREATE TABLE successful_deployments (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  hoster TEXT NOT NULL,
  server_id TEXT NOT NULL,
  ipv4 TEXT NOT NULL,
  proxy_type TEXT NOT NULL,
  client_config_path TEXT
);
```

## 13. Тесты

Минимальный набор:

1. `test_checker.py` — `is_whitelisted` для in/out/edge сетей.
2. `test_whitelist.py` — парсинг разных форматов (CIDR / одиночный IP / комментарии / пустые строки), дедупликация, объединение из нескольких источников.
3. `test_hosters/*` — для каждого хостера: с `respx` мокаем HTTP, проверяем правильность тела запроса, корректный парсинг ответа, обработку ошибок (401, 429, 5xx).
4. `test_orchestrator.py` — мок-хостер, мок-whitelist; проверяем что:
   - на hit → не вызывает delete, вызывает provisioner
   - на miss → вызывает delete, спит, переходит дальше
   - превышение max_attempts → NoHitError
   - SIGINT → cleanup создаёт DELETE для незавершённого
5. `test_proxies/test_xray_reality.py` — генерация валидного JSON-конфига и валидной vless-ссылки.

Coverage — стремиться к 80%+.

## 14. Безопасность и эксплуатация

- Все токены — только через переменные окружения (`token_env: TIMEWEB_TOKEN`). В конфиге — только имя переменной.
- `config.yaml` не должен попадать в git (`.gitignore`).
- Логи **никогда** не должны содержать токены: при логе ответов API — маскировать `Authorization`.
- При `wlfinder destroy --all` — двойное подтверждение (`--yes` обязателен).
- Лимит на параллельные create'ы (`parallel_workers`), иначе можно поймать rate-limit и потратить баланс на впустую.
- Default `max_attempts: 30` — не больше, иначе при плохом хостере набегает за час.
- Биллинг почасовой почти везде — каждая попытка ≈ 1 час самого дешёвого тарифа. Прикинуть бюджет: Timeweb cloud-1 ≈ 2-3 руб/час → 30 попыток ≈ 100 руб.

## 15. Гетчи, которые я (Claude Code) **должен** учесть при имплементации

1. **IP "залипает".** После delete тот же аккаунт у того же хостера может выдавать тот же или соседний IP 5-30 минут. Поэтому `delay_between_attempts_sec` ≥ 10, а лучше чередовать хостеров/регионы.
2. **Пул IP различается по локациям.** Российские локации (СПб, Москва) выдают IP из российских AS. Зарубежные — нет смысла, в БС не попадут.
3. **Cloud-init не всегда поддерживается через API.** У REG.ru параметр `user_data` есть; у Timeweb — через `cloud_init` поле в create-request (в т.ч. через CLI `twc`); у Selectel — через OpenStack `user_data` (base64). Если хостер не принимает user_data в create — провижионить через SSH после поднятия (запасной путь).
4. **Возможен redirect/redirect-policy в http-клиенте.** Хостеры иногда отдают 301/307 — нужно следовать.
5. **Rate-limits.** При получении 429 — exponential backoff, **не считать как попытку**.
6. **Ошибка биллинга.** При нехватке баланса API возвращает 402 / специфичный код — должны парсить и прерывать запуск с понятным сообщением, а не молча ретраить.
7. **TOS хостеров.** Multiple Russian hosters в TOS прямо запрещают VPN/прокси. Это риск пользователя, не наша забота на уровне кода, **но** в README надо чётко предупредить.
8. **SIGTERM на VPS.** Если запускаешь под systemd — обработать корректное завершение, успеть удалить probe-сервер.
9. **Часовые пояса.** Всё в UTC внутри, выводить локальное только в CLI.
10. **Идемпотентность delete.** Если сервер уже удалён или ещё не появился — не падать с 5xx, ретраить delete с бэкоффом.

## 16. README что должен содержать

- Что это.
- **Дисклеймер о юридических и TOS-рисках.** Пользователь должен понимать: некоторые хостеры в TOS прямо запрещают VPN; РКН в РФ имеет полномочия требовать блокировки; ответственность пользователя.
- Установка (`uv pip install -e .`).
- Получение токенов от каждого хостера (с прямыми ссылками на ЛК).
- Первый запуск: `wlfinder init`, отредактировать `config.yaml`, `wlfinder asn-stats`, `wlfinder run`.
- Импорт клиентского конфига в v2rayN / Nekobox / Streisand.
- FAQ (что делать при rate-limit, как смотреть статистику, как добавить новый источник whitelist).
- CONTRIBUTING — как добавить нового хостера: реализовать `Hoster` protocol + регистрация в `registry.py` + тесты.

## 17. Дорожная карта реализации (по фазам)

**Фаза 1 — MVP (один хостер):**
1. Скелет проекта, pyproject, Typer CLI с заглушками.
2. `whitelist/` + `checker.py` + тесты.
3. `hosters/timeweb.py` (только Timeweb) + тесты с respx.
4. `orchestrator.py` (без proxy provisioning, только find-and-keep).
5. `wlfinder run` работает: находит IP, оставляет сервер, выводит SSH-команду.

**Фаза 2 — Provisioning:**
6. `proxies/xray_reality.py` + cloud-init шаблон.
7. Генерация vless-ссылки и client.json.
8. SSH probe `wait_for_cloud_init`.

**Фаза 3 — Расширение:**
9. `hosters/regru.py`.
10. `hosters/selectel.py`.
11. ASN-stats команда.
12. `hosters/cloudru.py`, `clo.py`, `onecloud.py`.

**Фаза 4 — Качество:**
13. SQLite + stats команда.
14. `destroy --all`.
15. Hysteria2 как альтернативный proxy.
16. Полное покрытие тестами + CI.

## 18. Что вне scope

- GUI / web-панель.
- Управление уже работающим зоопарком прокси (это задача отдельного инструмента — typeof Marzban/3X-UI).
- Автоматический ротейт прокси по расписанию.
- IPv6-only сценарии (не работают по существу — мобильные операторы РФ не вкладывались в IPv6).
- Любые "free trial" / abuse-сценарии. Только обычные платные аккаунты.

---

**Готово. Claude Code: начинай с Фазы 1, шаг 1.** Любой неоднозначный момент в этом ТЗ — спроси у пользователя, не додумывай молча. После каждой фазы — короткий отчёт (что сделано / какие тесты прошли / что осталось).
