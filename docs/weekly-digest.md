# Weekly Digest: архитектура и эксплуатация

## Назначение

Еженедельный Excel-отчёт отправляется в Telegram-группу HR + руководство.
Содержит 4 листа: «Кандидаты» (все оценённые за неделю, сорт. по рейтингу),
«По позициям» (воронка по позициям), «Воронка» (KPI + диаграмма), «Динамика»
(тренд за 4 недели). Лист «Кандидаты» включает колонку «Регион» (берётся из
последнего снапшота: `snapshots.payload->'area'->>'name'`, «—» если отсутствует).

## Расписание (systemd timer, Пт 12:00 MSK)

Unit-файлы версионируются в `deploy/systemd/` и являются зеркалом
`/etc/systemd/system/` на сервере:

- `deploy/systemd/hh-digest.service` — `Type=oneshot`, запускает
  `python -u -m hh_monitor.cli digest weekly`.
- `deploy/systemd/hh-digest.timer` — `OnCalendar=Fri *-*-* 12:00:00 Europe/Moscow`,
  `Persistent=true` (пропущенный из-за простоя запуск отрабатывает на старте).

Timezone-в-OnCalendar требует systemd ≥ 240 (на проде ок).

Установка/обновление на сервере (после `git pull --ff-only`):
```bash
sudo cp /home/skadmin/hh-monitor/deploy/systemd/hh-digest.service /etc/systemd/system/
sudo cp /home/skadmin/hh-monitor/deploy/systemd/hh-digest.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hh-digest.timer
systemctl list-timers hh-digest.timer
```

Ручной запуск:
```bash
# Локально (dev):
poetry run python -m hh_monitor.cli digest weekly

# На сервере (прямой venv):
/home/skadmin/hh-monitor/.venv/bin/python -u -m hh_monitor.cli digest weekly
```

`digest now` — алиас `digest weekly` (немедленная отправка).

## Модули

| Файл | Назначение |
|------|-----------|
| `hh_monitor/weekly_digest/run.py` | `run_weekly_digest(session, bot)`, `_collect_data()`, сборка HR-сообщения |
| `hh_monitor/weekly_digest/excel.py` | `build_digest_workbook()` — 4-листовый XLSX |

## Поток выполнения

```
run_weekly_digest(session, bot)
  ├─> _collect_data(session, date_from, date_to)   # окно 7 дней
  │     ├─> SELECT Event JOIN Resume JOIN Search (+LATERAL latest snapshot → region)
  │     │     WHERE llm_enriched AND created_at ∈ [date_from, date_to) AND score_total >= порог
  │     ├─> воронка + агрегаты по позициям
  │     └─> _collect_parser_stats() → статистика парсера
  ├─> _collect_weekly_series(session)              # тренд за 4 недели
  ├─> Если кандидатов нет → короткое text-сообщение (см. Empty digest branch) → return
  ├─> build_digest_workbook(data, weekly_series)   # bytes XLSX (in-memory)
  └─> bot.send_message(HR summary) + bot.send_document(BufferedInputFile(xlsx_bytes))
```

Оба пути (text и Excel) передают `message_thread_id=TELEGRAM_DIGEST_TOPIC_ID` для
роутинга в топик «📊 Дайджесты». При `TELEGRAM_DIGEST_TOPIC_ID=0` (дефолт) топик
не указывается. Отправка Excel не подавляется ошибкой отправки текста.

## Empty digest branch

Если за неделю не было ни одного кандидата с LLM-оценкой, файл не генерируется.
Группа получает короткое text-сообщение:

```
📭 Weekly Digest DD.MM–DD.MM

За неделю не было одобренных кандидатов (статус ✅ Подходит).
Если что-то по работе — нажми на карточку в этой группе или напиши Лукину.
```

## Структура данных

`_collect_data` возвращает `_DigestData` (TypedDict в `run.py`):
`funnel`, `per_position`, `candidates_all`, `pending`, `parser_stats`.
Каждый кандидат — `_Candidate` (включает `region`). Колонки листа «Кандидаты»
формируются в `excel.py::_sheet_candidates` строго по списку `headers`.

## Статистика парсера

Берётся из таблицы `parser_runs` за последние 7 дней:
- `runs` — количество строк с `started_at >= date_from`
- `snapshots_inserted` — сумма `snapshots_inserted`
- `dedup_rate` — `round(skipped / (inserted + skipped) * 100)`

## Локальное тестирование

```bash
# Полный дайджест сейчас (требует реального BOT_TOKEN и GROUP_ID в .env)
poetry run python -m hh_monitor.cli digest weekly

# Юнит-тесты (без реального TG)
poetry run pytest tests/test_weekly_digest.py tests/weekly_digest/ -v
```

## Конфигурация

```env
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_HR_GROUP_ID=-100XXXXXXXXX
WEEKLY_DIGEST_CRON=0 12 * * 5      # справочно; реальное расписание — systemd timer (Пт 12:00 MSK)
WEEKLY_DIGEST_TZ=Europe/Moscow      # справочно
TELEGRAM_DIGEST_TOPIC_ID=10        # thread_id топика «📊 Дайджесты»; 0 = без топика
```
