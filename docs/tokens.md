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

---

## 1. Timeweb Cloud — `TIMEWEB_TOKEN`

1. Войдите в панель Timeweb Cloud: <https://timeweb.cloud/>
2. В правом верхнем углу откройте меню профиля → раздел **«API и терминал»**
   (прямая ссылка обычно <https://timeweb.cloud/my/api-keys>).
3. Нажмите **«Создать токен»**, задайте название (например `wlfinder`),
   при необходимости — срок действия.
4. Скопируйте токен **сразу** — он показывается только один раз.
5. Впишите в `.env`:
   ```
   TIMEWEB_TOKEN=ВАШ_ТОКЕН
   ```
6. Проверка: `wlfinder hoster ping` — для Timeweb должно показать `ok` и баланс.

Там же, в панели, посмотрите нужные для `config.yaml` значения:
- `preset_id` — ID тарифа (раздел создания сервера / `GET /presets/servers`);
- `os_id` — ID образа ОС (`GET /os/servers`), для Ubuntu.

---

## 2. REG.ru CloudVPS (Рег.облако) — `REGRU_TOKEN`

1. Войдите в личный кабинет REG.ru и перейдите в раздел **«Облачные
   серверы»** (Рег.облако): <https://cloudvps.reg.ru/>
2. Откройте **Настройки** → **«Токен для API»** (раздел API).
3. Сгенерируйте токен, скопируйте его.
4. Впишите в `.env`:
   ```
   REGRU_TOKEN=ВАШ_ТОКЕН
   ```
5. Проверка: `wlfinder hoster ping` — для REG.ru должно показать `ok`
   (баланс REG.ru CloudVPS через API не отдаёт — будет прочерк, это норма).

Документация API: <https://developers.cloudvps.reg.ru/>

---

## 3. Telegram-бот — `TELEGRAM_BOT_TOKEN` + `chat_id`

### 3.1. Токен бота

1. В Telegram откройте **[@BotFather](https://t.me/BotFather)**.
2. Отправьте `/newbot`, задайте имя и username бота (username должен
   оканчиваться на `bot`).
3. BotFather пришлёт токен вида `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`.
4. Впишите в `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx
   ```

### 3.2. chat_id (куда слать уведомления)

Бот **не может написать первым** — сначала откройте диалог с вашим ботом
и отправьте ему `/start` (или добавьте бота в группу).

Самый надёжный способ узнать `chat_id`:

```bash
curl -s "https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates"
```

В ответе найдите `"chat":{"id":...}` — это и есть ваш `chat_id`
(для лички — положительное число, для группы — отрицательное).

Альтернатива: написать боту **[@userinfobot](https://t.me/userinfobot)** —
он покажет ваш числовой ID.

Впишите его в `config.yaml`:
```yaml
notify:
  telegram:
    enabled: true
    bot_token_env: TELEGRAM_BOT_TOKEN
    chat_id: "123456789"
```

5. Проверка: `wlfinder notify test` — в Telegram должно прийти
   `✅ wlfinder: Telegram подключён`.

---

## Итоговая проверка

```bash
wlfinder hoster ping     # токены хостеров + баланс
wlfinder notify test     # доставка в Telegram
wlfinder run --dry-run   # весь пайплайн без создания серверов
```
