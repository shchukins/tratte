# Аудит сравнения расходов — issue #2

Артефакты `expense_comparison_audit_v1`:

- `audit.sql` — агрегаты PostgreSQL, без названий товаров, строк чеков и реквизитов;
- `run_audit.py` — запуск в одной `REPEATABLE READ READ ONLY` транзакции;
- `superset_audit.sql` — безопасная выборка метаданных BI без SQL источников и credentials;
- `CONTRACT.md` — периоды, знаменатели, исключения и требования к наблюдениям;
- `ADR-001-analytical-identity.md` — минимальная схема для следующего issue #3;
- `IDENTITY.md` — реализованный контракт #3, CLI ручной проверки, миграция и откат;
- `DECOMPOSITION.md` — контракт #5: факторы, мост, сценарий и CLI `analytics decompose`;
- `sql/decomposition_v1.sql` — параметризованный SQL факторов внутри валидированного снимка;
- `MARTS.md` — контракт #4: grains, поля, знаменатели, CLI, установка и права BI;
- `sql/views_v1.sql` — обычные безопасные PostgreSQL views;
- `sql/comparison_v1.sql` — параметризованный расчёт с обязательным validate gate;
- `sql/grants_v1.sql` — явные минимальные grants для отдельного BI role;
- `reports/2026-10-26-identity.md` — локальные проверки и ручное подтверждение scope #3;
- `reports/2026-10-25.md` — факты аудита и границы проверки Superset;
- `reports/2026-10-25.json` и `reports/2026-10-25-superset.json` — агрегаты среза.

## Повторить аудит

Нужен Python 3.12+ и `psql` на машине выполнения. Python-зависимости Tratte не
загружаются. Можно использовать локальный `psql`, контейнер или существующий SSH
доступ; пароль не передаётся в аргументах. Для локального PostgreSQL настройте
стандартный `.pgpass` / окружение вне репозитория. Предпочтителен read-only DB user.

```bash
.venv/bin/python analytics/run_audit.py \
  --ssh-host human-engine-home --container tratte-db-1 \
  --as-of '2026-10-25T20:11:15.782328+00:00' \
  --base-month 2026-08-01 --current-month 2026-09-01
```

Без параметров месяцев выбираются два последних завершённых месяца в timezone
`Europe/Moscow`; `--as-of` по умолчанию — время запуска с UTC offset. Wrapper
отклоняет незавершённые/несоседние месяцы и даты не первого числа. Он не запускает
импорт, миграции, application factory, `CREATE VIEW` или обновление mapping.
`psql -X`, `ON_ERROR_STOP` и таймаут запроса исключают пользовательский psqlrc,
продолжение после ошибки и бесконечное ожидание. Все сессии завершаются rollback.

Сам SQL использует psql-параметры `as_of`, `timezone`, `base_month`, `current_month`.
При ручном запуске нужно самостоятельно проверить границы по `CONTRACT.md` и
обернуть запрос в `BEGIN ... READ ONLY; ... ROLLBACK;` (см. `transaction()` wrapper).
Не вставляйте его без адаптации в Superset SQL Lab: это диагностический psql-запрос,
а не production dataset из issue #4.

Метаданные Superset читаются непосредственно из его PostgreSQL:

```bash
{ printf '%s\n' 'BEGIN READ ONLY;' "SET LOCAL statement_timeout = '30s';";
  cat analytics/superset_audit.sql;
  printf '%s\n' 'ROLLBACK;';
} | ssh -o BatchMode=yes human-engine-home \
  'docker exec -i superset_db psql -X -qAt -v ON_ERROR_STOP=1 -U superset -d superset'
```

Имена контейнеров и DB users — параметры текущей установки, перед повторением
проверьте их. Версия устанавливается без импорта приложения:

```bash
ssh human-engine-home \
  "docker exec superset_app python -c 'import importlib.metadata; print(importlib.metadata.version(\"apache-superset\"))'"
```

Не вызывайте `superset.app.create_app()` для read-only аудита: при инициализации
Superset 6.1.0 выполняет конфигурационную синхронизацию, в том числе системных тем.

## Что означает повторяемость

Один запуск видит согласованный снимок БД. Это **срез текущего состояния**, а не
историческое восстановление: Receipt и ProductAlias изменяемы, у ReceiptItem нет
created_at/истории ревизий. Фильтр `created_at <= as_of` не восстанавливает старые
значения после reprocess или изменения alias. Для сверки используйте сохранённые
агрегаты, параметры и SHA256 SQL; для исторического воспроизведения понадобился бы
отдельно разрешённый снимок данных. Immutable ledger не входит в #2.

Новые результаты сначала просмотрите вне репозитория. Сохранять можно только
агрегаты после проверки; не добавляйте имена, индивидуальные строки, SQL дампы,
OAuth, DSN, данные писем и необработанные логи.

## Проверки

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
# SQL на PostgreSQL через существующий SSH, только синтетические CTE:
TRATTE_AUDIT_TEST_SSH_HOST=human-engine-home \
  .venv/bin/python -m pytest tests/test_analytics_audit.py
```

В CI поднимается отдельный PostgreSQL 16 с одноразовыми тестовыми credentials;
`TRATTE_AUDIT_TEST_DSN` включает SQL-тесты. Локально без DSN/SSH эти два теста
пропускаются явно. PostgreSQL-тесты затеняют реальные таблицы CTE-фикстурами и не
создают таблиц даже при использовании существующего read-only соединения.
