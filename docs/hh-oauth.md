# HH.ru OAuth — Authorization Code Flow

## Схема авторизации (3 шага)

```
1. Приложение → браузер         GET /oauth/authorize?response_type=code&client_id=...
                                 Пользователь логинится и нажимает «Разрешить»

2. hh.ru → redirect_uri         GET https://localhost:8080/callback?code=AUTH_CODE&state=...
                                 Получаем одноразовый code

3. Приложение → hh.ru           POST /oauth/token  (grant_type=authorization_code, code=...)
                                 Получаем access_token + refresh_token
```

## Параметры нашего приложения

| Параметр | Значение |
|----------|---------|
| `client_id` | хранится в `.env` (переменная `HH_CLIENT_ID`) |
| `redirect_uri` | `https://localhost:8080/callback` (dev) |
| `response_type` | `code` |
| `grant_type` | `authorization_code` / `refresh_token` |
| `scope` | не задаётся явно — выдаётся по умолчанию для employer-приложения |

`client_secret` хранится в `.env` (переменная `HH_CLIENT_SECRET`) и **никогда не попадает в git**.

## Первичная авторизация через CLI

```bash
poetry run hh-monitor hh auth
```

Команда:
1. Генерирует случайный `state` (защита от CSRF).
2. Печатает полный URL авторизации — откройте его в браузере под корпоративной учётной записью hh.ru.
3. После того как hh.ru перенаправит вас на `redirect_uri`, скопируйте полный URL из адресной строки браузера и вставьте его в терминал.
4. Команда извлечёт `code`, проверит `state`, выполнит обмен на токены и сохранит их в таблицу `oauth_tokens`.
5. Выведет: `Token saved. Expires in <N> seconds.`

> **Примечание:** `redirect_uri=https://localhost:8080/callback` не требует запущенного сервера. hh.ru перенаправит браузер на этот адрес (страница не откроется), но вам нужен только URL из адресной строки.

## Автоматический refresh

`get_valid_token(session)` вызывается перед **каждым** запросом через `HHClient`. Если до истечения токена осталось менее 60 секунд, функция автоматически выполняет:

```
POST /oauth/token  grant_type=refresh_token
```

и обновляет строку в `oauth_tokens`. Приложение никогда не делает запросы с просроченным токеном.

## Проактивный refresh (systemd timer)

`get_valid_token` обновляет токен только когда приложение **само** делает запрос к HH.
Если запросов нет (тихий период), токен может протухнуть. Поэтому на сервере крутится
oneshot-таска, запускаемая таймером каждые 6 часов:

```bash
hh-monitor hh refresh --if-due
```

- `--if-due` — обновляет токен **только** если до истечения осталось меньше порога
  (`--threshold-hours`, по умолчанию 72 ч). Иначе — no-op, выход 0, HH не дёргается,
  алерт не шлётся (лог `hh.oauth.refresh.skipped reason=ttl_above_threshold`).
- Ручной `hh refresh` (без `--if-due`) обновляет токен безусловно — поведение не изменилось.
- Если HH отвечает 400 «token not expired» (в любом режиме) — это безобидный no-op,
  выход 0, алерт **не** шлётся.
- Реальная ошибка (отозванный refresh_token, сетевой сбой, нет токена в БД) → CRITICAL-алерт
  через production-gated путь + ненулевой exit (таска помечается failed в `systemctl status`).

Unit-файлы **не** хранятся в репозитории — применяются вручную на сервере (как у hh-digest).
Содержимое (зеркалит `hh-monitor-bot.service`: `User/Group=skadmin`, `.venv` python,
`.env` читается из `WorkingDirectory`, без `EnvironmentFile`):

```ini
# /etc/systemd/system/hh-oauth-refresh.service
[Unit]
Description=hh-monitor proactive HH OAuth token refresh (oneshot)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=skadmin
Group=skadmin
WorkingDirectory=/home/skadmin/hh-monitor
ExecStart=/home/skadmin/hh-monitor/.venv/bin/python -u -m hh_monitor.cli hh refresh --if-due
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hh-oauth-refresh
```

```ini
# /etc/systemd/system/hh-oauth-refresh.timer
[Unit]
Description=Run hh-monitor proactive OAuth refresh every 6 hours

[Timer]
OnCalendar=*-*-* 00/6:00:00
Persistent=true
Unit=hh-oauth-refresh.service

[Install]
WantedBy=timers.target
```

Установка на сервере:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hh-oauth-refresh.timer
systemctl list-timers hh-oauth-refresh.timer
```

## Если refresh-token отозван

Признаки: `hh-monitor hh me` возвращает `Error: Token refresh failed: ...` или `401`.

Решение — повторная авторизация:

```bash
poetry run hh-monitor hh auth
```

Это удалит старую строку из `oauth_tokens` и сохранит новую.

## Таблица oauth_tokens

Хранит не более одной строки (команда `auth` выполняет `DELETE` перед `INSERT`). Поля:

| Поле | Тип | Описание |
|------|-----|----------|
| `access_token` | TEXT | Bearer-токен для API запросов |
| `refresh_token` | TEXT | Для обновления access_token |
| `expires_at` | TIMESTAMPTZ | Момент истечения access_token |
| `scope` | TEXT | Разрешённые scope (может быть NULL) |
| `updated_at` | TIMESTAMPTZ | Обновляется при каждом refresh |

## Безопасность .env на сервере

Файл `.env` содержит OAuth-токены, клиентский секрет HH и API-ключи **в открытом виде**.
Ограничьте доступ сразу после первого деплоя:

```bash
chmod 700 /home/skadmin/hh-monitor
chmod 600 /home/skadmin/hh-monitor/.env
# Проверить владельца:
stat /home/skadmin/hh-monitor/.env
# Ожидаемый результат: Access: (0600/-rw-------), Uid: skadmin, Gid: skadmin
```

После этого файл доступен только пользователю `skadmin`; другие системные пользователи
доступа не имеют.

**Шифрование токенов at rest** — принятый MVP-риск. Токены хранятся в plaintext;
единственный контроль — права файловой системы и изоляция пользователя `skadmin`.
Полноценное шифрование at rest (например, через systemd credentials или HashiCorp Vault)
вынесено за рамки MVP.
