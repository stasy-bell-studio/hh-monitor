# Weekly Digest: архитектура и эксплуатация

## Назначение

Еженедельный PDF-отчёт отправляется в Telegram-группу HR + руководство.
Содержит: сводную таблицу по позициям, топ-5 кандидатов per позиция, статистику парсера.

## Расписание

В сессии 7 (deployment) настраивается systemd timer.
Unit-файлы не хранятся в репозитории — применяются вручную на сервере.

```ini
# /etc/systemd/system/hh-digest.service
[Unit]
Description=hh-monitor weekly digest sender (oneshot)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=skadmin
Group=skadmin
WorkingDirectory=/home/skadmin/hh-monitor
ExecStart=/home/skadmin/hh-monitor/.venv/bin/python -u -m hh_monitor.cli digest weekly
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hh-digest
```

```ini
# /etc/systemd/system/hh-digest.timer
[Unit]
Description=Run hh-monitor weekly digest every Friday at 15:00 Moscow time

[Timer]
OnCalendar=Fri *-*-* 15:00:00
TimeZone=Europe/Moscow
AccuracySec=1min
Persistent=true
Unit=hh-digest.service

[Install]
WantedBy=timers.target
```

Установка на сервере:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hh-digest.timer
systemctl list-timers hh-digest.timer
```

Для ручного запуска:
```bash
# Локально (dev):
poetry run python -m hh_monitor.cli digest now

# На сервере (прямой venv):
/home/skadmin/hh-monitor/.venv/bin/python -u -m hh_monitor.cli digest now
```

Или прямо из Telegram-группы (только для админов):
```
/digest force
```

## Модули

| Файл | Назначение |
|------|-----------|
| `hh_monitor/weekly_digest/run.py` | `run_weekly_digest(session, bot)`, `_collect_data()` |
| `templates/weekly_digest.html.j2` | Jinja2 шаблон с inline CSS |

## Поток выполнения

```
run_weekly_digest(session, bot)
  ├─> _collect_data(session, date_from, date_to)
  │     ├─> SELECT Event JOIN Resume JOIN Search WHERE llm_enriched=TRUE AND created_at IN [date_from, date_to]
  │     ├─> Группировка по position_code, сортировка по score_total DESC, top_candidates[:5]
  │     └─> SELECT ParserRun WHERE started_at >= date_from → stats
  ├─> [сессия 6.5] Если total_candidates == 0
  │     └─> bot.send_message(chat_id=HR_GROUP_ID, text=..., message_thread_id=DIGEST_TOPIC_ID) → return
  ├─> Jinja2 render → HTML string (in-memory)
  ├─> WeasyPrint HTML(string=html).write_pdf() → bytes (in-memory, диск не используется)
  └─> bot.send_document(chat_id=HR_GROUP_ID, document=BufferedInputFile(pdf_bytes),
                        caption=..., message_thread_id=DIGEST_TOPIC_ID)
```

Сессия 8: оба пути (text и PDF) передают `message_thread_id=TELEGRAM_DIGEST_TOPIC_ID` для
роутинга в топик «📊 Дайджесты». При `TELEGRAM_DIGEST_TOPIC_ID=0` (дефолт) топик не указывается.

## Empty digest branch

Если за неделю не было ни одного кандидата с LLM-оценкой (`total_candidates == 0`),
PDF не генерируется. Группа получает короткое text-сообщение:

```
📭 Weekly Digest DD.MM–DD.MM

За неделю не было одобренных кандидатов (статус ✅ Подходит).
Если что-то по работе — нажми на карточку в этой группе или напиши Лукину.
```

## Контекст шаблона

```python
{
    "week_number": int,           # ISO week number
    "date_from": "DD.MM.YYYY",
    "date_to": "DD.MM.YYYY",
    "generated_at": "DD.MM.YYYY HH:MM UTC",
    "total_candidates": int,
    "positions": [
        {
            "position_code": str,
            "position_name": str,
            "count": int,         # новых за неделю
            "avg_score": int,     # средний score
            "top_candidates": [   # max 5
                {
                    "hh_resume_id": str,
                    "verdict": str,
                    "real_role": str,
                    "score_total": int | None,
                    "comment": str,
                    "url": "https://hh.ru/resume/{id}",
                }
            ],
        }
    ],
    "parser_stats": {
        "runs": int,
        "snapshots_inserted": int,
        "dedup_rate": int,   # процент дедупликации
        "errors": int,
    },
}
```

## Статистика парсера

Берётся из таблицы `parser_runs` за последние 7 дней:
- `runs` — количество строк с `started_at >= date_from`
- `snapshots_inserted` — сумма `snapshots_inserted`
- `dedup_rate` — `round(skipped / (inserted + skipped) * 100)`
- `errors` — количество строк с `status != 'ok'`

## Изменение шаблона

Шаблон: `templates/weekly_digest.html.j2`

- Используется Jinja2 autoescape — HTML-инъекции из данных БД безопасны.
- Inline CSS — WeasyPrint не поддерживает внешние стили.
- После изменения шаблона: `poetry run python -m hh_monitor.cli digest now` → проверить PDF.

## Локальное тестирование

```bash
# Полный дайджест сейчас (требует реального BOT_TOKEN и GROUP_ID в .env)
poetry run python -m hh_monitor.cli digest now

# Только smoke-тест (без реального TG)
poetry run pytest tests/test_weekly_digest.py -v
```

## Конфигурация

```env
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_HR_GROUP_ID=-100XXXXXXXXX
WEEKLY_DIGEST_CRON=0 15 * * 5      # пятница 15:00 (только справочно, не используется в коде)
WEEKLY_DIGEST_TZ=Europe/Moscow      # только справочно
# Сессия 8: топик-роутинг
TELEGRAM_DIGEST_TOPIC_ID=10        # thread_id топика «📊 Дайджесты»; 0 = без топика
```

## Известные ограничения

- **PDF in-memory**: WeasyPrint держит весь PDF в RAM. При очень большом числе кандидатов (>500 за неделю) может потребовать много памяти. Это MVP-трейдоф, в сессии 7 можно добавить `--limit N` на `top_candidates`.
- **WeasyPrint system libs**: требует `pango`, `cairo`, `gobject-introspection`. На сервере установить через `apt install python3-weasyprint` или собрать зависимости вручную.
