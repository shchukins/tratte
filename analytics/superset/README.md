# Superset: изменение расходов — issue #6

Внутренний BI на **Superset 6.1.0**, PostgreSQL 16; отдельного web-приложения,
плагинов визуализации или публичного доступа к production нет. Два dashboard:
`tratte-expenses` (итоги, факторы, вклад, вкладка качества) и
`tratte-expense-detail` (фильтр product_id → наблюдения, медианы и приватные строки).
UUID объектов детерминированы, связи экспортируются по UUID, а не локальным ID.

## Источник и неизменяемый scope

`analytics dashboard-publish` вызывает `calculate(..., dashboard=True)`.
В **одной REPEATABLE READ READ ONLY транзакции** источник проходит validate_version,
SQL витрин #4, факторов #5 и `sql/dashboard_v1.sql`. Нет обхода gate через BI JOIN.
Для category среза строки фильтруются до порогов/P/Q; полный receipt context
показан отдельно и не распределяется на категории. Порог ≥2 разных чека в каждом
месяце относится к сопоставимым ценовым наблюдениям; медиана — отдельное описание.

После успешного расчёта writer публикует JSONB без преобразования numeric через
float в **отдельную BI БД**. Единственная таблица `tratte_bi.snapshot_v1` имеет
неизменяемый ключ SHA256 полного документа; повторная запись ключа ничего не меняет.
Проверка stale/неизвестной версии завершается до записи. Нет автоматического выбора
latest, scheduler и чтения source credentials из Superset.

Каждый виртуальный dataset содержит **литеральный snapshot_id**, поэтому новый
срез не меняет открытые графики и не смешивает версии при разных HTTP запросах.
Срез включает metadata, SQL SHA, mapping version, as_of и calculated_at. Это снимок
текущего изменяемого источника с отсечкой, не восстановление старых чеков.

Месяцы, магазин и категория меняются только публикацией **нового расчёта и пары
объектов**, привязанных к нему. Native time/store/category фильтров нет: они могли бы
отфильтровать готовые факторы без пересчёта. Нельзя добавлять такие фильтры в UI.
Фильтр product_id влияет только на detail dashboard и сохраняет scope итогов.
Сброс фильтра показывает все строки среза, включая unmatched с пустым product_id.

## Установка внутреннего источника

1. В отдельной BI PostgreSQL БД владелец выполняет `snapshot.sql`. Не выполняйте
   его в Superset metadata БД. Никаких изменений рабочих receipts/mapping.
2. Администратор создаёт отдельные LOGIN writer/reader без SUPERUSER, CREATEDB,
   CREATEROLE, BYPASSRLS и memberships. Пароли задаются вне Git/чатов. Writer:
   USAGE schema и SELECT/INSERT таблицы. Reader: USAGE schema и только SELECT:

```sql
GRANT USAGE ON SCHEMA tratte_bi TO tratte_bi_writer, tratte_bi_reader;
GRANT SELECT, INSERT ON tratte_bi.snapshot_v1 TO tratte_bi_writer;
GRANT SELECT ON tratte_bi.snapshot_v1 TO tratte_bi_reader;
ALTER ROLE tratte_bi_reader SET default_transaction_read_only = on;
```

Проверьте отсутствие inherited/PUBLIC доступа, DML и доступа к source DB.
Для source CLI используется уже настроенный источник приложения; writer URL
задаётся защищённым окружением `TRATTE_BI_DATABASE_URL` (SQLAlchemy psycopg URL).
Команда не создаёт схемы, роли, миграции и не применяет mapping:

```bash
.venv/bin/python -m app.cli analytics dashboard-publish \
  --mapping-version "$version" --as-of '2026-10-27T12:00:00+03:00' \
  --base-month 2026-08-01 --current-month 2026-09-01
# Необязательные exact scope: --store '...' --category '...'
```

Команда возвращает только snapshot_id; деньги/строки и DSN не выводятся. Без mapping
получается честный descriptive/insufficient_data срез, без ценовых наблюдений.
Ошибка не удаляет старые снимки. Старые snapshots удаляет владелец только после
снятия использующих их dashboards. Приложение writer не имеет DELETE/UPDATE.

## Экспорт и импорт

`tratte-expenses.zip` — **проверенный экспорт синтетического demo**, database URI
заменён на `postgresql://tratte_bi_reader@CHANGE_ME/tratte_bi`. Архив содержит 1 database, 11 datasets, 15 charts и 2 dashboards.
В архиве нет чековых
данных, credentials, реальных магазинов или названий; только SQL и объекты BI.
Сам архив не загружает данные. Опубликованный demo snapshot нужно создать отдельно.

Для своего snapshot создайте новый архив с обязательной явной привязкой:

```bash
.venv/bin/python -m analytics.superset.bundle \
  --snapshot-id "$snapshot_id" --output /tmp/tratte-dashboard.zip
```

Параметр обязан быть 64 hex символами. Без него экспорт содержит fail-closed
`REPLACE_WITH_SNAPSHOT_ID` и не читает ни одной публикации. Архив всегда содержит
только placeholder DSN; не записывайте в него credentials. Для нескольких срезов
используйте отдельные namespace/UUID и slug (текущий bundle предназначен для одной
пары объектов); не импортируйте срезы с одинаковыми UUID как разные dashboards.

В Superset 6.1.0: Dashboards → Import → архив. Database с UUID из `bundle.uid("database")`
должна быть привязана администратором к **reader** в нужной BI DB, а не source или
metadata DB. При первом импорте настройте URI в локальной защищённой копии
`databases/*.yaml` или предварительно создайте binding с этим UUID. Не коммитьте
такую копию, экспорт реальной database connection или сырые логи.

В 6.1 dashboard import сохраняет существующие database/dataset bindings даже при
Overwrite. Для переключения snapshot у существующих объектов импортируйте datasets
через Datasets → Import с Overwrite, затем dashboards; проверьте SQL snapshot_id
во всех 11 datasets. Overwrite dashboard одного недостаточно. После импорта
в основной и detail dashboard проверьте snapshot_id из Quality/детальной таблицы.
Автодеплой Tratte доставляет код команды и SQL; он **не** импортирует Superset
объекты и не публикует личные snapshots автоматически.

Доступ: только существующий внутренний VPN/SSH доступ, обычный Superset login.
Например, администратор пробрасывает loopback порт существующего Superset через
`ssh -L 18086:127.0.0.1:<внутренний-порт> <сервер>` и открывает
`http://localhost:18086/superset/dashboard/tratte-expenses/`.
Назначьте внутренней роли только эти datasets/dashboard; не используйте Public
role или guest embed. Архив импортируется как Draft; после проверки администратор
делает его доступным внутренней роли. Порт production не публикуется для портфолио.

## Отображение и ограничения

- Основной мост использует table fallback: база, цена, количество, появившиеся и
  исчезнувшие покупки, unmatched, сверка, округление, текущий итог. Накопленный итог
  позволяет проверить равенство. Waterfall plugin не устанавливается.
- Горизонтальный bar показывает вклад товаров. Рост + — терракотовый, снижение − —
  бирюзовое, нулевое — серое; знак и подпись сохраняют смысл без цвета.
- У таблиц сортировка/поиск только внутри конкретного набора, не scope расчёта.
  Первые 10 000 строк — лимит UI, не расчёта; большие выгрузки нужно разбивать на
  отдельные заранее рассчитанные scopes. Не считать видимую страницу полным итогом.
- Native cross-filter отключён: можно было бы неверно изменить только часть
  расчёта. Поддерживаемый маршрут: вклад → product_id → ссылка на detail → явный
  native filter. Контроль SQL проверяет каждый связанный dataset.
- Наблюдения цены: каждый item_id, дата, магазин, единица, item_total/mapped_quantity;
  scatter не агрегирует разные строки в одну цену. Медиана по month/store использует
  numeric среднее двух центральных значений, а не цену в факторном тождестве S/Q.
- Нулевой знаменатель → NULL/N/A, не 0%. Известные суммы отделены от полных итогов.
  При пропусках bridge_status=insufficient_data; пустой scope также явно имеет
  comparison_status=insufficient_data. Это не доказательство отсутствия расходов.
- Качество включает исключённые валюты/операции, unmatched, сверку, глобальную
  свежесть импорта, source overview (весь store) и полную metadata методики.
  В category scope сверка относится ко всему store, что подписано в заголовке.

## Повторить синтетическое demo

Нужен новый **пустой одноразовый** PostgreSQL 16; demo отказывается писать в БД,
в которой уже есть таблицы. Никогда не задавайте source/production URL:

```bash
# TRATTE_DEMO_DATABASE_URL — SQLAlchemy postgresql+psycopg://, задайте вне истории shell.
.venv/bin/python -m analytics.superset.demo
# Скопируйте напечатанный snapshot_id и выполните bundle --snapshot-id ...
```

Demo содержит два магазина, два полных месяца, два сопоставимых товара, вход/выход,
несопоставимую позицию и unknown operation. Все записи и названия вымышлены.
Импортируйте архив в отдельную чистую Superset 6.1.0 metadata DB с той же версией,
привяжите database к demo PostgreSQL. После проверки удалите только demo окружение.

Проверки и скриншоты: [VALIDATION.md](VALIDATION.md).

Для повторного экспорта используйте обычный Superset Dashboard Export обоих
дашбордов, затем **в изолированной синтетической среде**:

```bash
# Python окружения Superset 6.1.0 уже содержит PyYAML.
python sanitize_export.py /private/demo-export.zip /private/sanitized.zip
```

Скрипт заменяет connection object целиком и сохраняет UUID; он не обезличивает
произвольный production SQL/названия/фильтры. После него нужно просмотреть все JSON
в ZIP. Не запускайте публикацию реальных BI экспортов под видом synthetic demo.
