# HH.ru OAuth — Authorization Code Flow

## Схема авторизации (3 шага)

```
1. Приложение → браузер         GET /oauth/authorize?response_type=code&client_id=...
                                 Пользователь логинится и нажимает «Разрешить»

2. hh.ru → redirect_uri         GET https://localhost:8080/callback?code=AUTH_CODE&state=...
                                 Получаем одноразовый code

3. Приложение → api.hh.ru       POST /oauth/token  (grant_type=authorization_code, code=...)
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
