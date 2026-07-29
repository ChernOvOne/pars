# wlfinder

**IP-roulette** для российских хостеров: создаёт VPS, проверяет, попал ли его
публичный IPv4 в «белый список» мобильных операторов РФ, и при удаче
**уведомляет администратора в Telegram** — у какого хостера и какой IP оказался
в белом списке. Сервер при этом остаётся запущенным.

> **Статус:** реализованы все 6 хостеров — **Timeweb Cloud**, **REG.ru
> CloudVPS**, **Selectel**, **Cloud.ru**, **CLO.ru**, **1cloud** — а также
> `asn-stats`, `destroy --all` и интерактивное меню `pars`.
>
> ⚠️ Полноценно проверены на боевом API только Timeweb и REG.ru. Для
> Selectel / Cloud.ru / CLO.ru / 1cloud интеграции написаны по
> документации (ТЗ даёт по ним мало деталей) — перед продакшеном сверьте
> запросы с реальным API; в `config.example.yaml` они идут `enabled: false`.

---

## ⚠️ Дисклеймер о юридических и TOS-рисках

Прочитайте до использования:

- **TOS хостеров.** Ряд российских хостеров в пользовательском соглашении
  ограничивает определённые сценарии использования VPS. Нарушение TOS может
  привести к блокировке аккаунта без возврата средств — ответственность
  пользователя.
- **Регулирование.** Законодательство РФ в этой области меняется — следите за
  актуальным состоянием.
- **Ответственность.** Инструмент создаётся для образовательных целей и для
  личного использования на **легально оплаченных** аккаунтах. Вся
  ответственность за применение лежит на пользователе (см. `LICENSE`).
- Сценарии free-trial / abuse — **вне scope** проекта.

---

## Установка одной командой

```bash
curl -LsSf https://raw.githubusercontent.com/ChernOvOne/pars/main/install.sh | bash
```

Скрипт поставит [`uv`](https://docs.astral.sh/uv/) (если его нет) и установит
CLI `wlfinder`. После установки может потребоваться добавить в `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Установка для разработки

```bash
git clone https://github.com/ChernOvOne/pars && cd pars
uv venv && uv pip install -e ".[dev]"
```

## Интерактивное меню

Самый простой способ — запустить **`pars`** без аргументов: откроется
понятное меню со всеми действиями (настройка, обновление whitelist, проверка
хостеров и Telegram, ASN-статистика, пробный прогон, запуск, статистика).

```bash
pars
```

## Первый запуск

Через меню (`pars` → пункты по порядку) или командами:

```bash
pars init                        # config.example.yaml -> config.yaml
$EDITOR config.yaml              # выбрать хостеров, тарифы, регионы, chat_id
cp .env.example .env && $EDITOR .env  # вписать токены (см. docs/tokens.md)
pars whitelist update            # загрузить и закэшировать whitelist'ы
pars hoster ping                 # проверить токены / баланс
pars notify test                 # проверить доставку в Telegram
pars asn-stats                   # оценить шансы попадания по хостерам
pars run                         # запустить IP-roulette
```

Безопасная проверка пайплайна без реальных трат:

```bash
pars run --dry-run
```

> `pars` и `wlfinder` — одна и та же команда (псевдонимы).

## Получение токенов

**Пошаговая инструкция по всем ключам:** команда **`pars tokens`** (показывает
гайд прямо в терминале) или файл [`docs/tokens.md`](docs/tokens.md) — Timeweb
Cloud, REG.ru CloudVPS, Selectel, Cloud.ru, CLO.ru, 1cloud, Telegram-бот + `chat_id`.

Кратко:

| Что | Где взять |
|-----|-----------|
| Timeweb Cloud | Панель → API и терминал → [Токены API](https://timeweb.cloud/my/api-keys) |
| REG.ru CloudVPS | ЛК → Облачные серверы → Настройки → Токен для API |
| Selectel | Панель → Управление доступом → сервисный пользователь (OpenStack). **Роли обязательны:** `compute.admin` + `vpc.external_access.admin` (иначе `create_floatingip` вернёт 403). |
| Cloud.ru | Личный кабинет → ключи доступа: Key ID + Key Secret (OAuth2) |
| CLO.ru | Личный кабинет → раздел API → токен |
| 1cloud | Панель → раздел API → ключ |
| Telegram-бот | [@BotFather](https://t.me/BotFather) → `/newbot` → токен; `chat_id` — через `getUpdates` или [@userinfobot](https://t.me/userinfobot) |

Токены кладутся **только** в `.env`; в `config.yaml` хранится лишь имя
переменной окружения (`token_env: TIMEWEB_TOKEN`, `bot_token_env:
TELEGRAM_BOT_TOKEN`). `config.yaml` и `.env` добавлены в `.gitignore`.

## CLI

```
pars                          интерактивное меню (без аргументов)
pars init                     копирует config.example.yaml -> config.yaml
pars whitelist update         форсит обновление кэша whitelist
pars whitelist stats          размер кэша, разбивка по источникам
pars hoster ping              проверка токенов / баланса всех enabled-хостеров
pars notify test              тестовое сообщение в Telegram
pars asn-stats                пересечение префиксов хостеров с whitelist (оценка шансов)
pars run                      основной IP-roulette
pars run --dry-run            проверка пайплайна без create
pars run --hoster timeweb-spb --max-attempts 5
pars stats                    hit-rate по хостерам из истории SQLite
pars destroy --all --yes      [Phase 4] снести все wlfinder-* серверы
```

## Как это работает

1. Загружаются whitelist'ы (CIDR + IP) из открытых источников, кэшируются в
   `~/.cache/wlfinder/` (TTL 24 ч), сворачиваются в компактный набор сетей.
2. Оркестратор по кругу создаёт VPS у включённых хостеров.
3. Полученный IPv4 проверяется бинарным поиском по whitelist — O(log n).
4. **Hit** → сервер **оставляется запущенным**, в Telegram уходит сообщение
   (хостер, IP, регион, server_id, время, SSH-доступ, оценка цены/час), запуск
   останавливается.
5. **Miss** → сервер удаляется, пауза, следующая попытка.
6. Каждая попытка пишется в SQLite — `wlfinder stats` строит hit-rate.

## Конфигурация

Всё настраивается двумя файлами:

- **`config.yaml`** — параметры: хостеры, регионы, тарифы, `max_attempts`,
  паузы, источники whitelist, `notify.telegram.chat_id`, порог баланса.
- **`.env`** — секреты: API-токены хостеров, токен Telegram-бота.

### Selectel: subnet-camp (гарантированное попадание в конкретную /24)

Обычная рулетка выдаёт случайный floating IP из пула региона — шанс попасть в
whitelist'ную /24 обычно 1-3%. Selectel позволяет **прицельно** запросить IP
из конкретной подсети через `subnet_id` в Neutron API. Включается опцией
`target_subnet_cidr` на уровне хостера:

```yaml
- name: selectel-ru3-87.228.101
  type: selectel
  enabled: true
  account_id_env: SELECTEL_ACCOUNT_ID
  service_user_env: SELECTEL_SERVICE_USER
  service_pass_env: SELECTEL_SERVICE_PASS
  project_id_env: SELECTEL_PROJECT_ID
  region: ru-3
  target_subnet_cidr: 87.228.101.0/24    # ← пиннит выдачу в эту /24
  batch_size: 1
```

Как это работает:
1. На первом запросе `create()` тянет `GET /floatingip_pools` и находит
   `subnet_id` целевой /24 (или падает с `HosterError`, если её нет в пуле
   региона).
2. Каждый `POST /floatingips` шлётся с этим `subnet_id`.
3. Популярные whitelist'ные /24 обычно 100% заняты клиентами Selectel —
   Neutron отвечает `409 IpAddressGenerationFailure`. Это НЕ ошибка: адаптер
   бросает `HosterError("pool_exhausted — ...")`, orchestrator считает батч
   soft-miss и берёт следующий слот. Как только чей-то IP освобождается —
   wlfinder ловит его первым же запросом с **гарантированным попаданием** в
   /24.

Дефолтный `config.example.yaml` кампит 8 самых плотных /24 в Selectel (см.
`src/wlfinder/config.example.yaml`, снимок на 2026-07-29). Свежая карта
подсетей vs whitelist — `pars asn-stats`.

**Роли service-user'а обязательно:** `compute.admin` + `vpc.external_access.admin`
(вторая роль появилась в требованиях Selectel в июле 2026; без неё
`POST /floatingips` возвращает 403).

## FAQ

- **Rate-limit (429).** Клиент хостера сам делает exponential backoff; такие
  ответы не считаются попыткой.
- **IP «залипает».** После удаления тот же аккаунт может выдавать тот же или
  соседний IP 5–30 минут — поэтому `delay_between_attempts_sec` ≥ 10 и
  чередование хостеров/регионов.
- **Статистика.** `wlfinder stats` — hit-rate по хостерам из SQLite.
- **Новый источник whitelist.** Добавьте запись в `whitelist.sources`:
  `type: github` (raw-текст с IP/CIDR по строке, `url`), `type: file`
  (локальный файл, `path`) или `type: twl_subnets` (JSON-подсети
  openlibrecommunity/twl, `url` + `min_percent` — порог доли проверенных IP).
  По умолчанию основной источник — `openlibrecommunity/twl` (проверенные IP,
  обновляется ~ежедневно); `hxehex/*` подключён как дополнительное покрытие.

## Разработка

```bash
uv run pytest          # тесты
uv run ruff check src tests
uv run mypy            # strict
```

### CONTRIBUTING — как добавить нового хостера

1. Создайте `src/wlfinder/hosters/<name>.py` с классом, реализующим протокол
   `Hoster` (`hosters/base.py`): `create`, `delete`, `health_check`,
   `get_balance`, `estimate_cost_per_hour`.
2. Дайте классу `classmethod from_config(raw, client)` и собственную
   Pydantic-модель конфига.
3. Зарегистрируйте тип в `hosters/registry.py` (`_BUILDERS`).
4. Добавьте тесты в `tests/test_hosters/test_<name>.py` с моками `respx`.

## Лицензия

MIT — см. `LICENSE`.
