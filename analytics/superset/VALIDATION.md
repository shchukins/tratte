# Проверка issue #6 — 27.10.2026

## Среда и границы

Проверка выполнена на отдельной Docker network `tratte-issue6`, одноразовом
PostgreSQL 16 и отдельном Superset 6.1.0, используя тот же установленный image:
`sha256:5822dff49c41fd745ce33e38af502f9c64df30d133aeba148c5d89b35a1004ef`.
Superset слушал только loopback домашнего сервера; доступ через SSH tunnel.
Все записи вымышлены. Существующие Superset metadata, dashboards, users,
production receipts и mappings не менялись. Временные контейнеры удалены после QA.

## Результаты

- 194 pytest checks passed с настоящим одноразовым PostgreSQL 16; Ruff и
  `git diff --check` прошли. Alembic выдаёт существующее предупреждение
  `No path_separator found`; SQL-проверки не пропускались.
- Actual source SQL + все 11 BI SQL datasets: scope магазина/категории,
  полночь Москвы на границах месяцев, пороги разных чеков, исключённая операция,
  пустой scope, NULL total, нулевые знаменатели и сохранение Decimal.
- Stale mapping блокирует публикацию и сохраняет предыдущую. Новый snapshot
  не меняет dataset, уже привязанный к предыдущему ID. Невалидный ID/SQL injection
  отклоняется генератором; unbound archive читает пустой scope, без latest.
- Медиана проверена отдельно: цены наблюдений 10/30 дают медиану 20, тогда как
  факторная цена SUM(S)/SUM(Q)=25; текущие 20/40 дают медиану 30 и факторную 35.
- Синтетический пример: базовые покупки 850 ₽, текущие 1 410 ₽, Δ=+560 ₽ (+65.9%).
  Компоненты: цена +100, количество +420, появившиеся +70, исчезнувшие −30,
  unmatched 0, сверка 0, округление 0. Сумма сходится до копейки.
- Охват: 800/850 = 94.1% и 1320/1410 = 93.6%; проценты не усредняются.
- Браузерный сценарий: итог → мост → вклад `demo-milk-1l` → detail → native
  product filter → 8 ценовых наблюдений, 4 месячных/store медианы и 8 строк.
  В source SQL каждое наблюдение имеет свой item_id; scatter не объединяет их.
- Открыта вкладка качества: unmatched/unknown operation, сверка чеков, metadata
  и свежесть. Demo без sync имеет NULL последней успешной синхронизации.
- Пустой store `synthetic-empty` проверен в UI: comparison_status=insufficient_data,
  0 известных сумм, процент/coverage N/A, графики/таблицы товаров без наблюдений.
  Нулевой известный мост не объявляется доказательством отсутствия расходов.
- Native cross-filter и drill-through не используются как связка факторов:
  разрешённый маршрут — отдельный detail dashboard с явным native product_id.
  Native product filter проверен в реальном UI, без фильтрации общих KPI/факторов.

## Export → clean import

Сначала импортирован repo-generated bundle, затем выполнен **нативный Superset
Dashboard Export** обоих dashboards. В экспортированном архиве connection object
целиком заменён placeholder через `sanitize_export.py`; остальные объекты сохранены.
Все YAML представлены JSON (валидный YAML 1.2) для удобства ревью.

Полученный `tratte-expenses.zip` импортирован через `/api/v1/dashboard/import/`
в ещё одну **чистую Superset metadata DB**: HTTP 200. Проверены 1 database,
11 datasets, 15 charts, 2 dashboards и равенство всех UUID исходной конфигурации.
После импорта открыты основной dashboard и вкладка качества; связанные datasets
читают привязанный synthetic snapshot. Связи charts/datasets/native filter
перенесены через UUID, ссылки используют устойчивые slugs.

Проверка содержимого ZIP исключила password/encrypted_extra, реальные DSN/host,
имена/тела писем, исходные названия и credentials. В архиве нет source rows.
`tests/test_analytical_dashboard.py` проверяет именно checked-in native export,
а не только генератор. Синтетические store/product IDs в screenshots допустимы.

Dev image выдаёт 404 для отсутствующего service-worker.js; это не ошибка данных
или chart query. Наблюдалось в исходном image и не исправлялось в рамках #6.

## Скриншоты

Все скриншоты сделаны в реальном Superset на синтетических данных:

- [Итоги и охват](screenshots/overview.png)
- [Мост и вклады](screenshots/factors.png)
- [Наблюдения и медианы](screenshots/detail.png)
- [Вкладка качества](screenshots/quality.png)
- [Пустой scope](screenshots/empty.png)

## Production

CI → deploy сервиса проверяется отдельно для коммита реализации. Этот отчёт
не объявляет production BI установленным: автоматический деплой Tratte доставляет
CLI/SQL, а отдельная BI DB, reader binding, публикация реального среза и Superset
import выполняются по README администратором. Production Superset публично не открыт.
