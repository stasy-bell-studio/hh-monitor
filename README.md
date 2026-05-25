# hh-monitor

Внутренний сервис ООО «Страховая компания 21 век» для автоматического мониторинга базы резюме hh.ru по шести ключевым должностям. Ежедневно парсит резюме, определяет изменения (новые, обновлённые, удалённые), вычисляет Fit Score и отправляет дайджест в Notion и Telegram HR-команде.

Подробный контекст: [CLAUDE.md](CLAUDE.md).

## Запуск локально

```bash
# 1. Поднять БД
docker compose up -d db

# 2. Установить зависимости
poetry install

# 3. Скопировать и заполнить переменные окружения
cp .env.example .env  # заполнить HH_CLIENT_ID, HH_CLIENT_SECRET и т.д.

# 4. Накатить миграции и проверить
poetry run alembic upgrade head
poetry run pytest -v
```

## Запуск тестов

Тесты ходят в отдельную БД `hh_monitor_test`, не в dev `hh_monitor`.

### Одноразовая настройка (существующий Docker volume)

```bash
docker compose exec db psql -U hh_monitor -d hh_monitor \
  -c "CREATE DATABASE hh_monitor_test;"
```

Убедись, что в `.env` есть строка:
```
TEST_DATABASE_URL=postgresql+asyncpg://hh_monitor:hh_monitor_dev@localhost:5432/hh_monitor_test
```

### Запуск

```bash
poetry run pytest -v
```

Миграции на test-БД накатываются автоматически один раз за pytest-сессию (`alembic upgrade head`). Каждый тест работает в транзакции, которая откатывается после завершения — данные не накапливаются.

Если `TEST_DATABASE_URL` не задан, все DB-тесты пропускаются с предупреждением в stderr.

## CLI: примеры команд

```bash
# Пересобрать критическую линзу для позиции (сохраняется в searches.llm_critic_prompt)
poetry run hh-monitor search rebuild-critic-lens --search-code branch_director_21vek
```
