# TG-бот: архитектура и эксплуатация

## Поток данных

```
pipeline run
  └─> events (llm_enriched=TRUE, score_total ≥ threshold)
        └─> tg send-pending
              ├─> notifications_sent (idempotency gate)
              ├─> send_card → Telegram group (HTML + inline keyboard)
              └─> callback clicks → UPDATE notifications_sent WHERE screening_status IS NULL RETURNING
```

## Модули

| Файл | Назначение |
|------|-----------|
| `hh_monitor/tg/client.py` | `make_bot()`, `is_admin()`, `send_card()` с retry/error handling |
| `hh_monitor/tg/cards.py` | `build_card_html()`, `build_inline_keyboard()` |
| `hh_monitor/tg/sender.py` | `send_new_candidate_card()`, `send_pending_cards()`, `get_current_threshold()` |
| `hh_monitor/tg/handlers.py` | aiogram Router: callback, /threshold, /digest, /help |

## Таблицы БД

### `notifications_sent`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `event_id` | BIGINT PK FK → events.id | Один ряд на отправленное событие |
| `tg_message_id` | BIGINT NOT NULL | ID сообщения в Telegram |
| `sent_at` | TIMESTAMPTZ | Время отправки |
| `screening_status` | TEXT nullable | `approve` / `reject` / `doubt` |
| `screened_at` | TIMESTAMPTZ nullable | Время разметки |
| `screened_by` | BIGINT nullable | Telegram user_id разметчика |
| `screened_by_username` | TEXT nullable | @username разметчика |

### `app_config`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `key` | TEXT PK | Ключ настройки, например `telegram_score_threshold` |
| `value` | TEXT NOT NULL | Строковое значение |
| `updated_at` | TIMESTAMPTZ | Время последнего изменения |

## Idempotency

`send_new_candidate_card` проверяет `notifications_sent WHERE event_id = ?` перед отправкой.
Если строка уже есть — карточка не отправляется повторно, функция возвращает `False`.

## First-click-wins (inline кнопки)

```sql
UPDATE notifications_sent
   SET screening_status = :status,
       screened_at = NOW(),
       screened_by = :user_id,
       screened_by_username = :username
 WHERE event_id = :event_id
   AND screening_status IS NULL
RETURNING event_id
```

- Если `RETURNING` вернул строку → пользователь увидит подтверждение.
- Если пусто → кто-то уже разметил; пользователь увидит `show_alert` с именем первого разметчика.
- Атомарность гарантируется PostgreSQL row-level locking.

## Команды бота

| Команда | Доступ | Действие |
|---------|--------|----------|
| `/threshold` | Все | Показать текущий порог score |
| `/threshold N` | Только админы | Установить порог (0–100). UPSERT в `app_config` |
| `/digest` | Все | Топ-5 кандидатов за последние 24ч (HTML-список в группу) |
| `/digest force` | Только админы | Запустить полный PDF-дайджест немедленно |
| `/help` | Все | Список команд |

## Конфигурация (.env)

```env
TELEGRAM_BOT_TOKEN=<bot token от @BotFather>
TELEGRAM_HR_GROUP_ID=-100XXXXXXXXX    # отрицательный для супергруппы
TELEGRAM_ADMIN_USER_IDS=123456789,987654321   # comma-separated
TELEGRAM_SCORE_THRESHOLD=60           # дефолт; перезаписывается через /threshold N
```

## Права бота в группе

Бот должен быть добавлен в группу с правами:
- **Отправка сообщений** — для карточек и дайджеста
- **Чтение сообщений** — для обработки команд
- **Отправка файлов** — для PDF-дайджеста

## Добавить нового администратора

Добавить Telegram user_id в `TELEGRAM_ADMIN_USER_IDS` в `.env` и перезапустить бот:
```bash
# .env
TELEGRAM_ADMIN_USER_IDS=111111111,222222222,333333333
poetry run python -m hh_monitor.cli tg run
```

## Формат callback_data

```
screen:{event_id}:{status}
```

- `event_id` — BigInteger (max 19 цифр)
- `status` — `approve` | `reject` | `doubt`
- Максимальная длина: `7 + 19 + 1 + 6 = 33 байта` ≪ 64-байтный лимит Telegram

## CLI-команды

```bash
# Отправить накопленные карточки (до N штук)
poetry run python -m hh_monitor.cli tg send-pending --limit 10

# Запустить long-polling бот
poetry run python -m hh_monitor.cli tg run
```

## Обработка ошибок Telegram API в `send_card`

| Исключение | Действие |
|-----------|----------|
| `TelegramRetryAfter` | sleep(retry_after + 1), один retry; если снова — log WARNING + raise |
| `TelegramBadRequest` | log WARNING (HTML рендер некорректен) + raise |
| `TelegramForbiddenError` | log CRITICAL (бот удалён из группы) + raise → `send_pending_cards` прерывает цикл |
| Прочие `TelegramAPIError` | log WARNING + raise → `send_pending_cards` продолжает со следующим |
