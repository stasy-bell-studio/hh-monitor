# TG-бот: архитектура и эксплуатация

## Поток данных

```
pipeline run
  └─> events (llm_enriched=TRUE, score_total ≥ threshold)
        └─> tg send-pending
              ├─> notifications_sent (idempotency gate)
              ├─> send_card → Telegram group (HTML + 4-button inline keyboard)
              └─> callback clicks
                    ├─> screen:{id}:{status}  → first-click-wins UPDATE
                    │         └─> reason menu (edit_reply_markup)
                    ├─> reason:{id}:{status}:{code} → INSERT screening_reasons
                    │         └─> card edit (edit_message_text)
                    └─> back:{id}   → restore status keyboard
```

## Топик-раскладка (сессия 8)

Группа конвертирована в супергруппу с топиками. Каждый тип контента идёт в свой топик:

| Топик | thread_id | Контент | Env var |
|-------|-----------|---------|---------|
| 📥 Кандидаты | 9 | Карточки кандидатов | `TELEGRAM_CARDS_TOPIC_ID=9` |
| 📊 Дайджесты | 10 | Weekly digest (PDF и текст) | `TELEGRAM_DIGEST_TOPIC_ID=10` |
| 🎛 Управление | 7 | Все admin-команды и ответы бота | `TELEGRAM_ADMIN_TOPIC_ID=7` |

`thread_id=0` (значение по умолчанию) означает «без топика» — для обратной совместимости.

## Модули

| Файл | Назначение |
|------|-----------|
| `hh_monitor/tg/client.py` | `make_bot()`, `is_admin()`, `get_session_factory()`, `send_card()` с retry/error handling |
| `hh_monitor/tg/cards.py` | `build_card_html()`, `build_inline_keyboard()` |
| `hh_monitor/tg/sender.py` | `send_new_candidate_card()`, `send_pending_cards()`, `get_current_threshold()` |
| `hh_monitor/tg/handlers.py` | aiogram Router: screen/reason/back callbacks, custom reply, /threshold, /digest, /help |
| `hh_monitor/tg/reasons.py` | `STATUS_LABELS`, `PRESETS`, `build_reason_keyboard()`, `format_final_text()` |
| `hh_monitor/tg/commands.py` | admin_router: /active /archive /stats /settings /help + все adm: callbacks |

## Таблицы БД

### `notifications_sent`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `event_id` | BIGINT PK FK → events.id | Один ряд на отправленное событие |
| `tg_message_id` | BIGINT NOT NULL | ID сообщения в Telegram |
| `sent_at` | TIMESTAMPTZ | Время отправки |
| `screening_status` | TEXT nullable | `approve` / `reject` / `doubt` / `stop_list` |
| `screened_at` | TIMESTAMPTZ nullable | Время разметки |
| `screened_by` | BIGINT nullable | Telegram user_id разметчика |
| `screened_by_username` | TEXT nullable | @username разметчика |

### `screening_reasons`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | BIGSERIAL PK | |
| `event_id` | BIGINT UNIQUE FK → notifications_sent(event_id) CASCADE | Одна причина на событие |
| `status` | TEXT NOT NULL | Статус из `ScreeningStatus` |
| `reason_code` | VARCHAR(64) nullable | Slug preset-причины; NULL для custom |
| `reason_text` | TEXT NOT NULL | Текст причины (preset label или ввод пользователя) |
| `screened_by` | BIGINT NOT NULL | Telegram user_id |
| `screened_by_username` | TEXT nullable | @username |
| `created_at` | TIMESTAMPTZ NOT NULL | |

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

### Общие (handlers.py, любой топик)

| Команда | Доступ | Действие |
|---------|--------|----------|
| `/threshold` | Все | Показать текущий порог score |
| `/threshold N` | Только админы | Установить порог (0–100). UPSERT в `app_config` |
| `/digest` | Все | Топ-5 кандидатов за последние 24ч (HTML-список в группу) |
| `/digest force` | Только админы | Запустить полный PDF-дайджест немедленно |
| `/help` | Все | Список команд |

### Админ-панель (commands.py, только топик 🎛 Управление)

| Команда | Доступ | Действие |
|---------|--------|----------|
| `/active` | Только админы | Список активных и приостановленных поисков с метриками |
| `/archive` | Только админы | Последние 20 архивных поисков (read-only) |
| `/stats` | Только админы | Статистика за 24ч/7д/30д, гистограмма score, топ причин |
| `/settings` | Только админы | Показать конфиг; изменить порог через ForceReply |
| `/hh_refresh` | Только админы | Форсировать refresh HH OAuth токена; вывести было/стало |
| `/help` | Только админы | Справка по admin-командам |

### Inline callbacks (adm:*)

| Callback data | Действие |
|--------------|----------|
| `adm:stop:{id}` | Остановить поиск (`active=FALSE`) |
| `adm:resume:{id}` | Возобновить поиск (`active=TRUE`) |
| `adm:archive:{id}` | Запросить подтверждение архивации |
| `adm:yes_arch:{id}` | Подтвердить архивацию (`archived_at=NOW()`) |
| `adm:no_arch:{id}` | Отменить, вернуть карточку |
| `adm:detail:{id}` | Подробная статистика (stub, Сессия 9) |
| `adm:threshold` | ForceReply для ввода нового порога |
| `adm:close` | Удалить сообщение бота |

Все `adm:` callbacks принимаются только от пользователей из `TELEGRAM_ADMIN_USER_IDS`.
Fail-soft: если `UPDATE ... RETURNING` возвращает пусто → `show_alert("⚠️ Состояние поиска изменилось, обнови /active")`.

### Lifecycle поиска (searches)

```
active=TRUE, archived_at=NULL  →  [⏸ Остановить]  →  active=FALSE, archived_at=NULL
active=FALSE, archived_at=NULL →  [🔄 Возобновить] →  active=TRUE, archived_at=NULL
(любой active)                 →  [🗑 Архив] + confirm → active=FALSE, archived_at=NOW()  (безвозвратно)
```

## Конфигурация (.env)

```env
TELEGRAM_BOT_TOKEN=<bot token от @BotFather>
TELEGRAM_HR_GROUP_ID=-100XXXXXXXXX      # отрицательный для супергруппы
TELEGRAM_ADMIN_USER_IDS=123456789,987654321   # comma-separated
TELEGRAM_SCORE_THRESHOLD=60             # дефолт; перезаписывается через /settings
# Топики (сессия 8)
TELEGRAM_CARDS_TOPIC_ID=9              # thread_id топика «📥 Кандидаты»
TELEGRAM_DIGEST_TOPIC_ID=10            # thread_id топика «📊 Дайджесты»
TELEGRAM_ADMIN_TOPIC_ID=7              # thread_id топика «🎛 Управление»
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

## Inline-клавиатура (сессия 6.5)

Первый шаг — статус:
```
screen:{event_id}:{status}
```
- `status` — `approve` | `reject` | `doubt` | `stop_list`

Второй шаг — причина (меню после выбора статуса):
```
reason:{event_id}:{status}:{reason_code}   # preset
reason:{event_id}:{status}:custom          # свободный ввод
back:{event_id}                             # назад к статусам
```

- Все callback_data ≤ 64 байт (проверено тестом).

## Двухшаговый FSM для сбора причин

```
[карточка] → клик статуса
  → first-click-wins UPDATE notifications_sent WHERE screening_status IS NULL
  → edit_reply_markup → меню причин
    → клик preset → INSERT screening_reasons → edit_message_text карточки
    → клик «✍️ Своя» → ForceReply prompt → пользователь отвечает
      → handle_custom_reason_message → INSERT + edit карточки
    → клик «← Назад» → восстановить исходную клавиатуру статусов
```

### Конкурентная защита

- **Статус**: UPDATE WHERE IS NULL RETURNING — атомарно, первый захватывает.
  Проигравший видит `⚠️ Уже заскринено: @X`.
- **Причина**: INSERT ON CONFLICT (event_id) DO NOTHING RETURNING id.
  Если `id = None` → `⚠️ Причина уже записана`.

### Кнопка «← Назад»

Доступна только автору захвата (`screened_by == user.id`) и только пока причина ещё не записана.

### Custom ForceReply TTL

In-memory FSM: `_custom_fsm: dict[int, _FsmState]`. TTL = 300 сек.
Если пользователь ответил через >5 мин → `⌛ Сессия истекла, нажми кнопку статуса заново`.

## Сбор причин (preset-каталог)

| Статус | Preset-причины |
|--------|---------------|
| ✅ Подходит | Релевантный опыт / Точная должность / Нужный регион / Адекватные ожидания |
| ❌ Мимо | Слабый опыт / Не тот регион / Завышенные ожидания / Стоп-индустрия |
| 🤔 Спорно | Нужно обсудить / Пограничный опыт / Нестандартный профиль |
| 🚫 Стоп-лист | Конкурент / Прошлый плохой опыт / Несовместимость по портрету |

Для каждого статуса доступна «✍️ Своя» (свободный текст).

После записи причины карточка перерисовывается:
```
{emoji} {status_label}: {reason_text} — @username

<оригинальное тело карточки>
```
Inline-клавиатура удаляется.

## Формат callback_data

```
screen:{event_id}:{status}
```

- `event_id` — BigInteger (max 19 цифр)
- `status` — `approve` | `reject` | `doubt` | `stop_list`
- Максимальная длина: `7 + 19 + 1 + 9 = 36 байт` ≪ 64-байтный лимит Telegram

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
