# Как получить ключи API и токены

Все секреты wlfinder хранит **только** в файле `.env` (он в `.gitignore`).
В `config.yaml` указывается лишь имя переменной окружения, например
`token_env: TIMEWEB_TOKEN`. Скопируйте шаблон и заполняйте его:

```bash
cp .env.example .env
$EDITOR .env
```

> UI панелей хостеров со временем меняется — если пункт меню называется
> чуть иначе, ищите по словам «API», «Токен», «Ключ».
>
> Полноценно проверены на боевом API только **Timeweb** и **REG.ru**.
> Интеграции **Selectel / Cloud.ru / CLO.ru / 1cloud** написаны по
> документации — перед боевым запуском сверьте запросы с реальным API.

---

## 1. Timeweb Cloud — `TIMEWEB_TOKEN`

1. Войдите в панель Timeweb Cloud: <https://timeweb.cloud/>
2. Меню профиля (правый верхний угол) → раздел **«API и терминал»**
   (прямая ссылка обычно <https://timeweb.cloud/my/api-keys>).
3. **«Создать токен»**, задайте название (например `wlfinder`).
4. Скопируйте токен **сразу** — он показывается один раз.
5. В `.env`:
   ```
   TIMEWEB_TOKEN=ВАШ_ТОКЕН
   ```
6. Для `config.yaml` там же посмотрите: `preset_id` — ID тарифа,
   `os_id` — ID образа ОС (Ubuntu).
7. Проверка: `pars hoster ping`.

---

## 2. REG.ru CloudVPS (Рег.облако) — `REGRU_TOKEN`

1. Личный кабинет REG.ru → раздел **«Облачные серверы»** (Рег.облако):
   <https://cloudvps.reg.ru/>
2. **Настройки** → **«Токен для API»**.
3. Сгенерируйте токен, скопируйте.
4. В `.env`:
   ```
   REGRU_TOKEN=ВАШ_ТОКЕН
   ```
5. Проверка: `pars hoster ping` (баланс REG.ru через API не отдаёт —
   будет прочерк, это норма).

Документация API: <https://developers.cloudvps.reg.ru/>

---

## 3. Selectel — service user (OpenStack)

Авторизация двухступенчатая: сервисный пользователь → токен Keystone.
Нужны **4 переменные**:

```
SELECTEL_ACCOUNT_ID=     # номер аккаунта Selectel (видно в панели)
SELECTEL_SERVICE_USER=   # имя сервисного пользователя
SELECTEL_SERVICE_PASS=   # его пароль
SELECTEL_PROJECT_ID=     # ID облачного проекта
```

1. Панель Selectel → **Управление доступом** → **Пользователи** →
   создайте **сервисного пользователя**, задайте пароль.
2. Дайте ему доступ к нужному облачному проекту; ID проекта возьмите в
   разделе проекта.
3. `ACCOUNT_ID` — номер вашего аккаунта Selectel (в шапке панели).
4. В `config.yaml` дополнительно нужны OpenStack-UUID:
   `flavor_id` (тариф), `image_id` (образ ОС), `network_id` (публичная
   сеть) — посмотрите в панели или через OpenStack API проекта.
5. Проверка: `pars hoster ping`.

Документация: <https://developers.selectel.ru/docs/selectel-cloud-platform/>

---

## 4. Cloud.ru Evolution — Key ID + Key Secret (OAuth2)

```
CLOUDRU_KEY_ID=          # идентификатор ключа
CLOUDRU_KEY_SECRET=      # секрет ключа
CLOUDRU_PROJECT_ID=      # ID проекта
```

1. Личный кабинет Cloud.ru → раздел управления доступом / **ключи API**.
2. Создайте пару **Key ID + Key Secret** (секрет показывается один раз).
3. `PROJECT_ID` возьмите в свойствах вашего проекта.
4. В `config.yaml` укажите `flavor` и `image` (id/слаг тарифа и образа).
5. Проверка: `pars hoster ping`.

Документация: <https://cloud.ru/docs/foundation/ug/topics/api-list.html>

---

## 5. CLO.ru — `CLO_TOKEN`

1. Личный кабинет CLO.ru → раздел **API** → создайте токен.
2. В `.env`:
   ```
   CLO_TOKEN=ВАШ_ТОКЕН
   ```
3. В `config.yaml` укажите `flavor` и `image` (id/слаг тарифа и образа).
4. Проверка: `pars hoster ping`.

Документация: <https://clo.ru/docs/>

---

## 6. 1cloud.ru — `ONECLOUD_TOKEN`

1. Панель 1cloud → раздел **API** → получите ключ.
2. В `.env`:
   ```
   ONECLOUD_TOKEN=ВАШ_ТОКЕН
   ```
3. В `config.yaml` укажите `image_id` (ID образа ОС из 1cloud), при
   необходимости поправьте `cpu` / `ram` / `hdd` / `dc_location`.
4. Проверка: `pars hoster ping`.

Документация: <https://1cloud.ru/api>

---

## 7. Telegram-бот — `TELEGRAM_BOT_TOKEN` + `chat_id`

### 7.1. Токен бота

1. В Telegram откройте **[@BotFather](https://t.me/BotFather)**.
2. Отправьте `/newbot`, задайте имя и username (username оканчивается
   на `bot`).
3. BotFather пришлёт токен вида `123456789:AAExxxxxxxxxxxxxxxxxxxxx`.
4. В `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxx
   ```

### 7.2. chat_id (куда слать уведомления)

Бот **не может написать первым** — сначала откройте диалог с вашим
ботом и отправьте `/start` (или добавьте бота в группу).

Самый надёжный способ узнать `chat_id`:

```bash
curl -s "https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates"
```

В ответе найдите `"chat":{"id":...}` — это и есть `chat_id`
(личка — положительное число, группа — отрицательное).

Альтернатива: написать **[@userinfobot](https://t.me/userinfobot)** —
он покажет ваш числовой ID.

`chat_id` идёт **не в `.env`, а в `config.yaml`**:
```yaml
notify:
  telegram:
    enabled: true
    bot_token_env: TELEGRAM_BOT_TOKEN
    chat_id: "123456789"
```

Проверка: `pars notify test` — в Telegram придёт
`✅ wlfinder: Telegram подключён`.

---

## Итоговая проверка

```bash
pars hoster ping     # токены хостеров + баланс
pars notify test     # доставка в Telegram
pars run --dry-run   # весь пайплайн без создания серверов
```
