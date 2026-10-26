# SQL-витрины — issue #4

Контракт `expense_marts_v1`, PostgreSQL 16+. Расширяет [CONTRACT.md](CONTRACT.md)
и использует проверку [IDENTITY.md](IDENTITY.md). Обычные views и SELECT, без
materialized views, scheduler, исправления исходных значений или расчёта факторов #5.

## Установка и обновление

Views устанавливает additive-миграция `0004_analytical_marts`:

```bash
.venv/bin/python -m alembic upgrade head
```

На сервер она поступает только через существующий CI → deploy. Миграция создаёт
четыре views, отнимает права PUBLIC только на них, не применяет review и не меняет
исходные чеки/каталог. На SQLite это явно пустая миграция: расчёт требует PostgreSQL.
SQL входит в Python wheel и Docker image; путь к файлам не зависит от cwd.

Повторная установка того же определения в выбранной разрешённой БД атомарна:

```bash
psql -X -v ON_ERROR_STOP=1 --single-transaction -f analytics/sql/views_v1.sql
```

DSN/пароли передаются через защищённое окружение / `.pgpass`, не через Git или
командную строку. Используется схема connection search_path (в production public).
Перед изменением состава/семантики колонок нужен новый номер контракта и миграция;
`CREATE OR REPLACE` предназначен для повторения текущего определения.

Откат после остановки потребителей: `alembic downgrade 0003_analytical_identity`.
Удаляет только четыре views, без CASCADE; источник и каталог остаются. Зависимые
пользовательские views могут блокировать откат и должны быть сняты явно.

## Grains и безопасные поля

| View | Уникальный grain | Поля |
| --- | --- | --- |
| analytics_receipts_v1 | receipt_id | purchased_at, purchase_date, month, created_at, updated_at, store, status, currency, operation, receipt_total, valid_receipt_total |
| analytics_items_v1 | item_id | receipt_id, category, source_quantity, source_unit, source_unit_price, item_total, valid_item_values, item_arithmetic_delta, item_arithmetic_mismatch |
| analytics_reconciliation_v1 | receipt_id | item_rows, known_item_totals, known_item_amount, receipt_minus_items, reconciliation_status |
| analytics_quality_v1 | month + currency + operation + status | receipt_count, known_receipt_totals, recorded_receipt_amount, item_rows, known_item_totals, known_item_amount, reconcilable_receipts, receipt_minus_items, absolute_receipt_minus_items, mismatched_receipts, receipts_without_items, receipts_with_missing_item_totals, snapshot_at, methodology_version |

Views описывают **все текущие сохранённые наблюдения**. `month`/purchase_date —
Europe/Moscow; исходный timestamp хранит offset. null month остаётся отдельной
группой. Для завершённых месяцев/parsed нужно применять явный фильтр; views сами
не объявляют полноту периода или расходов. Классификация operation — точный
trim/lower из CONTRACT. Валюты в группах всегда раздельны, возврат не меняет знак.

`valid_receipt_total` означает строго > 0. `valid_item_values` — все три значения
total/quantity/unit_price строго > 0; null считается невалидным. Source-поля —
точная копия исходных чисел и единиц, без преобразования/подстановки.
`item_arithmetic_delta = item_total - source_unit_price * source_quantity`;
`item_arithmetic_mismatch` — abs(delta) > 0.01; при неизвестной арифметике null.
Расхождение не исправляется и само по себе не исключает покупку: скидка может
давать другую уплаченную сумму.

`item_rows` считает строки, `known_item_totals` — строки с ненулевым в смысле
SQL NOT NULL total (числовой 0 известен). `known_item_amount` — SUM известных
total; при отсутствии строк null. Разница receipt_total − SUM(item_total)
существует только при хотя бы одной строке и полностью известных суммах.
`reconciliation_status`: no_items → missing_receipt_total → missing_item_totals
→ mismatch (abs > 0.01) → balanced. Неизвестная разница остаётся null.

В quality `receipt_count` включает все чеки группы; `known_receipt_totals`
считает только известные суммы; `recorded_receipt_amount` — SUM известных сумм.
`reconcilable_receipts` — число известных разниц; signed/absolute разницы суммируются
**только** по ним. Остальные счётчики — соответствующие статусы сверки.
snapshot_at — начало транзакции чтения, не immutable снимок источника.

Receipt KPI считайте из receipts или quality. После JOIN receipts → items нельзя
суммировать receipt_total. В item-view этого поля намеренно нет. Category composition
использует item_total. Не распределяйте остаток чека пропорционально товарам.

Views не содержат оригинальных/нормализованных названий, seller, адресов, почтовых
или фискальных полей, parse_error, OAuth и secrets. Store/category/ID/суммы всё равно
приватны. Для публичного demo нужна **отдельная synthetic БД**, не фильтр живой БД.

## Параметризованный расчёт подтверждённой части

```bash
.venv/bin/python -m app.cli analytics marts \
  .local-evaluations/marts-2026-08-09.json \
  --mapping-version "$version" --as-of '2026-10-26T21:00:00+03:00' \
  --base-month 2026-08-01 --current-month 2026-09-01
# Необязательный точный фильтр store применяется ДО порогов и P/Q:
# --store 'synthetic-a'
```

Команда создаёт новый файл 0600 и не перезаписывает существующий. Родительская папка
должна существовать. JSON не публикуется и не выводится в stdout; числа PostgreSQL
сохраняются без промежуточного float. Без mapping-version расчёт остаётся описательным:
confirmed amount = 0, comparison_status = insufficient_data. Не выбирается latest.

`calculate()` открывает отдельную REPEATABLE READ READ ONLY транзакцию, устанавливает
30s timeout, вызывает validate_version и затем `sql/comparison_v1.sql` в той же
транзакции. Stale/удалённый/повреждённый/неизвестный mapping блокирует **весь** расчёт.
Проверяется полный выбранный manifest, даже если store-фильтр исключает часть строк.
Внешний Session/cached validation не принимается. Сырые SQL JOIN каталога без этого
gate не являются поддерживаемым потребителем и не должны становиться BI dataset.

Параметры пары наследуют CONTRACT: два соседних завершённых месяца, первое число,
as_of с UTC offset. `created_at <= as_of`, `purchased_at < as_of`, status=parsed;
месячные границы полуоткрытые по Москве. Магазин фильтруется до группировок.
Это срез **текущего** состояния с отсечкой as_of, не восстановление прошлой версии:
Receipt/ReceiptItem изменяемы; mapping не восстанавливает источник.

Подтверждённое количество = source_quantity × factor из валидированной версии,
unit = kg/l/piece. Вес/объём уже в исходной единице не умножается на содержимое
упаковки повторно. `paid_unit_price = SUM(item_total)/SUM(comparable_quantity)`;
для одной строки та же формула без SUM. Единицы не смешиваются; один product_id
фиксирует вариант/упаковку. source_unit_price не используется как уплаченная цена.

## Выходные наборы расчёта и знаменатели

Каждый набор относится к metadata: methodology_version, mapping_version,
mapping_rules_version/mapping_created_at (null без mapping), as_of, calculated_at,
timezone, base_month/current_month, store, minimum_receipt_observations_per_month=2,
totals_status=descriptive_only. `sql_sha256` фиксирует ожидаемые файлы SQL этой сборки,
а не доказывает отсутствие сторонних изменений установленных views.

| Набор JSON | Grain и значения |
| --- | --- |
| receipt_monthly | month/currency/operation; receipt_count, known_receipt_totals, recorded_receipt_amount, average_known_receipt (знаменатель known_receipt_totals), invalid_receipt_totals |
| items | item_id; поля item-view + month/currency/operation/valid_receipt_total, mapping_status/product_id/quantity_basis/factor/comparison_unit, initial_reason, mapped_quantity, reason, comparable_quantity/paid_unit_price |
| product_monthly | month/product_id/comparison_unit; item_rows, receipt_observations = COUNT(DISTINCT receipt_id), paid_amount, comparable_quantity, paid_unit_price, passes_pair_threshold |
| coverage | ровно две строки month, включая пустые; item_rows, receipts_with_items, A/B/D, два отношения D/A и D/B, comparable_products, comparison_status |
| reasons | month/currency/reason; item_rows, receipt_observations, known_item_totals, recorded_item_amount |
| quality | month/currency; item_rows, invalid_item_values, unknown_arithmetic, arithmetic_mismatches, signed_arithmetic_delta, absolute_arithmetic_delta |
| reconciliation | receipt_id; поля reconciliation-view + month/currency/operation |
| overview | observed_receipts, missing_purchase_dates, future_purchase_dates, unparsed_receipts, last_purchase_at; весь выбранный store, created_at <= as_of, вне ограничения пары |
| freshness | last_completed_sync <= as_of, failed_syncs_last_day по started_at, last_sync_status по started_at/id; глобальный журнал sync, без store-фильтра |

`mapped_quantity` — диагностическое количество подтверждённого mapping при валидных
item values; comparable_quantity/paid_unit_price определены только при initial_reason
IS NULL (положительная RUB purchase, положительный чек и подтверждённая identity).
product_monthly включает такие наблюдения, даже если ключ не прошёл порог пары;
passes_pair_threshold явно отличает их от допущенных к будущей декомпозиции #5.

A = positive_rub_item_amount: все положительные RUB item_total выбранных parsed
чеков, включая неизвестные операции. B = eligible_purchase_item_amount: из A только
purchase с receipt_total > 0, **включая** плохие quantity/unit/identity. D =
confirmed_comparable_amount: допустимые подтверждённые строки ключей с ≥2 разными
чеками в **каждом** месяце. Несколько строк одного чека не повышают число наблюдений.
comparable_products — количество таких ключей в месяце. Отношения
confirmed_coverage_of_positive_rub = D/A и confirmed_coverage_of_eligible_purchases
= D/B; при знаменателе 0 null. Суммы A/B/D пустого периода = 0. Проценты месяцев
не усредняются. Это покрытие наблюдаемых строк, не всех личных расходов.

У строки ровно один reason, в приоритете: operation_unknown/purchase_return/other,
currency_not_rub, invalid_receipt_total, invalid_item_values,
identity_unmatched/unresolved/rejected, not_observed_in_both_periods,
too_few_receipts, comparable. initial_reason — воронка до порога пары; null у
допустимой подтверждённой строки. Причины identity теперь относятся к явным ID
каталога #3, а не кандидатному имени/единице аудита #2. Signed суммы reasons могут
быть отрицательными и не равны положительному знаменателю A по определению.

available_partial означает наличие хотя бы одного ключа, прошедшего порог пары;
иначе insufficient_data. recorded суммы остаются descriptive_only. Ни статус,
ни порог не доказывают статистическую значимость, полноту или причинность.

## Superset и права

Администратор создаёт отдельный LOGIN role без memberships, SUPERUSER/CREATEDB/
CREATEROLE/BYPASSRLS и без прав на исходные таблицы. Credentials задаются вне Git.
Затем выполняет `sql/grants_v1.sql` через psql с `-v bi_role=tratte_bi`: только USAGE
public, SELECT четырёх views и default_transaction_read_only. Нет default grants
будущим объектам; PUBLIC не получает доступ. Проверьте, что существующие PUBLIC/
наследуемые grants не дают чтение receipts, receipt_items, gmail_integrations и
analytics_mapping_versions. Сам read_only default не заменяет запрет DML grants.

Физические Superset datasets #6 могут читать четыре views. Подтверждённая часть
выдаётся отдельным защищённым расчётом/приватным экспортом, поскольку обычный SQL
dataset не умеет вызывать Python validate_version в своей транзакции. Здесь нет
SQL-only обхода проверки или обещания готового дашборда. Подключение результата
к BI в #6 должно сохранить этот gate и metadata; нельзя выдать BI права на raw
mapping/manifest и считать простое соединение валидированным результатом.

## Проверки

```bash
TRATTE_AUDIT_TEST_DSN=postgresql://audit@localhost:55439/audit_utf8 \
  .venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

DSN должен указывать только на одноразовый PostgreSQL, никогда production.
Тесты создают/удаляют временные схемы и NOLOGIN role, поэтому требуют прав владельца
тестовой БД. CI уже запускает такую БД PostgreSQL 16. Проверяются фактический SQL,
grains, Москва, валюты/операции, null/0, сверка, пороги/фильтр, вычисление цены,
stale gate, согласованность при конкурентной записи, приватность колонок, права,
повторная установка и downgrade → upgrade с сохранением источника.
