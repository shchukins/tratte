"""Deterministic, credential-free Superset 6.1 export. JSON is valid YAML 1.2."""

import argparse
import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).parent


def uid(name):
    return str(uuid5(NAMESPACE_URL, "https://tratte.invalid/expense-dashboard/v1/" + name))


COMMON = """SELECT s.snapshot_id, s.published_at,
 s.document->'metadata'->>'pair_id' AS pair_id,
 s.document->'metadata'->>'as_of' AS as_of,
 coalesce(s.document->'metadata'->>'store','Все магазины') AS scope_store,
 coalesce(s.document->'metadata'->>'category','Все категории') AS scope_category,
 s.document->>'comparison_status' AS comparison_status,
 s.document->>'bridge_status' AS bridge_status,
 s.document->'metadata'->>'mapping_version' AS mapping_version,
 s.document->'sql_sha256' AS sql_sha256,
"""
SOURCE = " FROM tratte_bi.snapshot_v1 s"

DEFINITIONS = {
    "context": ("""s.document->'metadata' AS methodology,
        s.document->'freshness' AS freshness, s.document->'overview' AS source_overview""", None),
    "coverage": ("r.*", "coverage|month date, item_rows bigint, missing_item_totals bigint, "
        "known_item_amount numeric, positive_item_amount numeric, matched_amount numeric, "
        "matched_coverage_of_positive_items numeric, unmatched numeric"),
    "products": ("r.*, abs(r.amount_delta) AS absolute_contribution, "
        "CASE WHEN r.amount_delta>0 THEN 'Рост +' WHEN r.amount_delta<0 THEN 'Снижение −' "
        "ELSE 'Без изменения' END AS direction", "products|product_id text, "
        "category text, comparison_unit text, reason text, s0 numeric, s1 numeric, q0 numeric, "
        "q1 numeric, p0 numeric, p1 numeric, receipts0 bigint, receipts1 bigint, "
        "price_effect numeric, quantity_effect numeric, amount_delta numeric"),
    "details": ("r.*", "details|item_id bigint, receipt_id bigint, product_id text, "
        "purchased_at timestamptz, purchase_date date, month date, store text, category text, "
        "currency text, operation text, source_quantity numeric, source_unit text, "
        "mapped_quantity numeric, comparison_unit text, item_total numeric, "
        "paid_unit_price numeric, reason text"),
    "medians": ("r.*", "medians|product_id text, comparison_unit text, month date, store text, "
        "item_observations bigint, receipt_observations bigint, median_paid_unit_price numeric"),
    "reasons": ("r.*", "reasons|month date, currency text, operation text, reason text, "
        "item_rows bigint, missing_item_totals bigint, known_item_amount numeric"),
    "reconciliation": ("r.*", "reconciliation|receipt_id bigint, month date, currency text, "
        "operation text, item_rows bigint, known_item_totals bigint, known_item_amount numeric, "
        "receipt_minus_items numeric, reconciliation_status text"),
    "scenario": ("r.*", "scenario|status text, previous_price_cost numeric, "
        "actual_same_part_cost numeric, actual_minus_scenario numeric, product_count bigint, "
        "current_coverage_of_positive_items numeric"),
}


def dataset_sql(snapshot_id="REPLACE_WITH_SNAPSHOT_ID"):
    result = {}
    for name, (fields, records) in DEFINITIONS.items():
        sql = COMMON + fields + SOURCE
        if records:
            key, columns = records.split("|", 1)
            function = "jsonb_to_record" if key=="scenario" else "jsonb_to_recordset"
            sql += f", {function}(s.document->'{key}') AS r({columns})"
        result[name] = sql
    # KPI scope follows decomposition target. Never sum receipt totals after item joins.
    result["totals"] = """WITH months AS (
      SELECT s.*, c.*, (s.document->'metadata'->>'category') IS NOT NULL AS category_scope
      FROM tratte_bi.snapshot_v1 s, jsonb_to_recordset(s.document->'coverage') AS c(
        month date, known_item_amount numeric, missing_item_totals bigint)),
    totals AS (
      SELECT m.*, CASE WHEN category_scope THEN known_item_amount ELSE coalesce((
        SELECT sum(r.recorded_receipt_amount)
        FROM jsonb_to_recordset(document->'receipt_monthly') AS r(
          month date,currency text,operation text,recorded_receipt_amount numeric)
        WHERE r.month=m.month AND currency='RUB' AND operation='purchase'),0) END AS known_total,
        CASE WHEN category_scope THEN missing_item_totals ELSE coalesce((
          SELECT sum(r.receipt_count-r.known_receipt_totals)
          FROM jsonb_to_recordset(document->'receipt_monthly') AS r(
            month date,currency text,operation text,receipt_count bigint,
            known_receipt_totals bigint)
          WHERE r.month=m.month AND currency='RUB' AND operation='purchase'),0) END AS missing
      FROM months m)
    SELECT snapshot_id, month, known_total, missing,
      CASE WHEN missing=0 THEN known_total END AS total,
      document->'metadata'->>'bridge_target' AS target,
      document->>'comparison_status' AS comparison_status,
      document->>'bridge_status' AS bridge_status,
      (document->>'target_delta')::numeric AS delta,
      CASE WHEN document->>'bridge_status'='available' THEN
        (document->>'target_delta')::numeric / nullif(first_value(known_total)
          OVER(ORDER BY month),0) END AS delta_ratio
    FROM totals"""
    result["bridge"] = """WITH totals AS (""" + result["totals"] + """), parts AS (
      SELECT snapshot_id, 0 AS step, 'База (известная сумма)' AS label,
             known_total AS amount FROM totals WHERE month=(SELECT min(month) FROM totals)
      UNION ALL
      SELECT snapshot_id, CASE component WHEN 'price' THEN 1 WHEN 'quantity' THEN 2
        WHEN 'current_only' THEN 3 WHEN 'base_only' THEN 4 WHEN 'unmatched' THEN 5
        WHEN 'reconciliation' THEN 6 ELSE 7 END,
        CASE component WHEN 'price' THEN 'Цена' WHEN 'quantity' THEN 'Количество'
        WHEN 'current_only' THEN 'Появились в покупках' WHEN 'base_only' THEN 'Исчезли из покупок'
        WHEN 'unmatched' THEN 'Несопоставимые' WHEN 'reconciliation' THEN 'Сверка'
        ELSE 'Округление' END, amount_rounded
      FROM tratte_bi.snapshot_v1 s, jsonb_to_recordset(document->'bridge') AS b(
        component text,amount_rounded numeric)
    ) SELECT *, sum(amount) OVER(ORDER BY step) AS running_known_total,
      CASE WHEN amount>0 THEN 'Рост +' WHEN amount<0 THEN 'Снижение −'
           ELSE 'Без изменения / неизвестно' END AS direction
      FROM parts
      UNION ALL SELECT snapshot_id,8,'Текущий итог (известная сумма)',known_total,known_total,
        'Итог' FROM totals WHERE month=(SELECT max(month) FROM totals)"""
    result["observations"] = result["details"].replace("r.*", """r.*,
        to_char(r.purchased_at AT TIME ZONE 'Europe/Moscow','YYYY-MM-DD HH24:MI:SS')
          || ' · #' || r.item_id AS observation_key""") + " WHERE r.paid_unit_price IS NOT NULL"
    if snapshot_id != "REPLACE_WITH_SNAPSHOT_ID" and not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Snapshot ID must be a SHA256 returned by dashboard-publish")
    binding = "(SELECT * FROM tratte_bi.snapshot_v1 WHERE snapshot_id='" + snapshot_id + "') s"
    return {name: sql.replace("tratte_bi.snapshot_v1 s", binding) for name, sql in result.items()}


# Raw tables retain numeric values and explicit statuses; charts never convert null to zero.
CHARTS = [
    ("context", "Какие расходы изменились? Периоды, дата среза и scope", [
        "pair_id", "as_of", "published_at", "scope_store", "scope_category",
        "comparison_status", "bridge_status"]),
    ("totals", "Расходы ₽ • Δ ₽ • Δ/база (при ненулевой базе)", [
        "month", "target", "total", "known_total", "missing", "delta", "delta_ratio"]),
    ("coverage", "Охват: сопоставимая сумма / положительные RUB purchase строки", [
        "month", "matched_amount", "positive_item_amount", "matched_coverage_of_positive_items",
        "missing_item_totals", "unmatched"]),
    ("bridge", "Мост: база → факторы → сверка → текущий итог, ₽", [
        "step", "label", "amount", "running_known_total", "direction"]),
    ("products", "Вклад товаров, ₽ • рейтинг по абсолютному изменению", [
        "product_id", "amount_delta", "absolute_contribution", "reason"]),
    ("products", "Факторы: S, Q, P=S/Q • разные единицы не суммируются", [
        "product_id", "comparison_unit", "reason", "s0", "s1", "q0", "q1", "p0", "p1",
        "receipts0", "receipts1", "price_effect", "quantity_effect", "amount_delta"]),
    ("scenario", "Та же сопоставимая часть по прежней цене • не обещание экономии", [
        "status", "previous_price_cost", "actual_same_part_cost", "actual_minus_scenario",
        "current_coverage_of_positive_items", "product_count"]),
    ("reasons", "Качество: несопоставимые строки и исключения", [
        "month", "currency", "operation", "reason", "item_rows", "missing_item_totals",
        "known_item_amount"]),
    ("reconciliation", "Сверка чеков (весь store; категория не распределяет чек)", [
        "receipt_id", "month", "currency", "operation", "item_rows", "receipt_minus_items",
        "reconciliation_status"]),
    ("context", "Свежесть, ограничения источника и хеши методики", [
        "freshness", "source_overview", "methodology", "sql_sha256",
        "snapshot_id", "mapping_version"]),
    ("observations", "Наблюдения уплаченной цены, ₽/ед. • выберите product_id", [
        "product_id", "purchase_date", "paid_unit_price", "comparison_unit", "store",
        "receipt_id", "item_id"]),
    ("medians", "Медиана наблюдений по месяцу/магазину • отдельно от P=S/Q", [
        "product_id", "month", "store", "comparison_unit", "median_paid_unit_price",
        "item_observations", "receipt_observations"]),
    ("details", "Приватная детализация и причины исключения", [
        "product_id", "receipt_id", "item_id", "purchased_at", "store", "category",
        "source_quantity", "source_unit", "mapped_quantity", "comparison_unit", "item_total",
        "currency", "operation", "paid_unit_price", "reason", "snapshot_id"]),
    ("products", "Вклад товаров • рост + / снижение −, ₽", [
        "product_id", "amount_delta", "direction"]),
    ("observations", "История уплаченной цены • каждый item_id — наблюдение", [
        "item_id", "paid_unit_price", "product_id", "comparison_unit", "store"]),
]


def position(title, groups, markdown):
    p = {"DASHBOARD_VERSION_KEY": "v2",
         "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
         "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
         "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": [], "parents": ["ROOT_ID"]}}
    for index, indices in enumerate(groups):
        row = f"ROW-{index}"
        p["GRID_ID"]["children"].append(row)
        p[row] = {"id": row, "type": "ROW", "parents": ["ROOT_ID", "GRID_ID"],
                  "children": [], "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for i in indices:
            node = f"CHART-{i}"
            p[row]["children"].append(node)
            p[node] = {"id": node, "type": "CHART", "children": [],
                       "parents": ["ROOT_ID", "GRID_ID", row],
                       "meta": {"chartId": i+1, "uuid": uid(f"chart/{i}"),
                                "sliceName": CHARTS[i][1], "width": 12//len(indices),
                                "height": 60 if i in (3, 5, 12) else 20 if i==0 else 35}}
    node = "MARKDOWN-guide"
    p["GRID_ID"]["children"].insert(0, node)
    p[node] = {"id": node, "type": "MARKDOWN", "children": [], "parents": ["ROOT_ID", "GRID_ID"],
               "meta": {"code": markdown, "width": 12, "height": 35}}
    if len(groups) > 5:
        tabs = "TABS-content"
        tab_ids = ["TAB-analysis", "TAB-quality"]
        p[tabs] = {"id": tabs, "type": "TABS", "children": tab_ids,
                   "parents": ["ROOT_ID", "GRID_ID"], "meta": {}}
        p["GRID_ID"]["children"] = [node, tabs]
        for tab, text, rows in ((tab_ids[0], "Изменение расходов", list(range(7))),
                                (tab_ids[1], "Качество и свежесть", list(range(7,len(groups))))):
            p[tab] = {"id": tab, "type": "TAB", "children": [f"ROW-{r}" for r in rows],
                      "parents": ["ROOT_ID", "GRID_ID", tabs], "meta": {"text": text}}
            for r in rows:
                row = f"ROW-{r}"
                p[row]["parents"] = ["ROOT_ID", "GRID_ID", tabs, tab]
                for chart in p[row]["children"]:
                    p[chart]["parents"] = ["ROOT_ID", "GRID_ID", tabs, tab, row]
    return p


GUIDE = """## Почему изменились наблюдаемые расходы?
Покупки RUB · два полных соседних месяца · Москва. Период, магазин и категория
указаны в срезе ниже; все итоги и факторы рассчитаны в этих границах.

**Недостаточно данных** (`insufficient_data`) означает, что сравнение недоступно.
Пустая выборка не доказывает отсутствие расходов. «Известная сумма» учитывает только
сохранённые значения; итог и изменение пусты при пропусках. Охват относится к
наблюдаемым положительным RUB purchase строкам, а не ко всем личным расходам.

**Изменение → фактор → товар → наблюдение:** найдите вклад и product_id,
[откройте детализацию](/superset/dashboard/tratte-expense-detail/) и выберите товар.
Мост — таблица с накопленным итогом, все суммы ₽. Эффекты описывают покупки;
это не официальная инфляция и не причинная оценка магазина. Цена может включать акции.
"""


LABELS = {
    "month": "Месяц", "pair_id": "Сравниваемые месяцы", "as_of": "Данные на",
    "published_at": "Опубликовано", "scope_store": "Магазин", "scope_category": "Категория",
    "comparison_status": "Доступность сравнения", "bridge_status": "Полнота моста",
    "total": "Итог ₽", "known_total": "Известная сумма ₽", "missing": "Пропуски итогов",
    "delta": "Изменение ₽", "delta_ratio": "Изменение к базе %", "target": "Уровень итога",
    "matched_amount": "Сопоставимые покупки ₽", "positive_item_amount": "Знаменатель ₽",
    "matched_coverage_of_positive_items": "Охват %", "missing_item_totals": "Пропуски сумм строк",
    "unmatched": "Несопоставимая сумма ₽", "step": "Шаг", "label": "Компонент",
    "amount": "Вклад ₽", "running_known_total": "Накопленный итог ₽", "direction": "Направление",
    "product_id": "Товар (product_id)", "amount_delta": "Изменение ₽",
    "absolute_contribution": "Абсолютный вклад ₽", "reason": "Причина / допуск",
    "comparison_unit": "Единица", "s0": "Затраты база ₽", "s1": "Затраты текущие ₽",
    "q0": "Количество база", "q1": "Количество текущее", "p0": "Цена база ₽/ед.",
    "p1": "Цена текущая ₽/ед.", "receipts0": "Чеков база", "receipts1": "Чеков текущих",
    "price_effect": "Эффект цены ₽", "quantity_effect": "Эффект количества ₽",
    "purchase_date": "Дата покупки", "purchased_at": "Время покупки", "store": "Магазин",
    "category": "Категория", "paid_unit_price": "Уплачено ₽/ед.",
    "median_paid_unit_price": "Медиана ₽/ед.", "item_observations": "Наблюдений строк",
    "receipt_observations": "Разных чеков", "source_quantity": "Исходное количество",
    "source_unit": "Исходная единица", "mapped_quantity": "Сопоставимое количество",
    "item_total": "Сумма строки ₽", "currency": "Валюта", "operation": "Операция",
    "status": "Доступность сценария", "previous_price_cost": "Та же часть по прежней цене ₽",
    "actual_same_part_cost": "Фактически та же часть ₽", "actual_minus_scenario": "Разница ₽",
    "current_coverage_of_positive_items": "Охват текущих покупок %", "product_count": "Товаров",
}


def column_type(c):
    if c in {"purchase_date", "month"}:
        return "DATE"
    if c in {"purchased_at", "published_at"}:
        return "TIMESTAMP WITH TIME ZONE"
    if c in {"step", "item_id", "receipt_id", "missing", "item_rows", "missing_item_totals",
             "item_observations", "receipt_observations", "receipts0", "receipts1"}:
        return "BIGINT"
    if c in {"s0", "s1", "q0", "q1", "p0", "p1", "price_effect", "quantity_effect",
             "source_quantity", "mapped_quantity", "item_total", "paid_unit_price",
             "median_paid_unit_price", "amount", "running_known_total", "amount_delta",
             "absolute_contribution", "total", "known_total", "delta", "delta_ratio",
             "matched_amount", "positive_item_amount", "matched_coverage_of_positive_items",
             "unmatched", "known_item_amount", "receipt_minus_items", "previous_price_cost",
             "actual_same_part_cost", "actual_minus_scenario",
             "current_coverage_of_positive_items"}:
        return "NUMERIC"
    return "TEXT"


def configs(snapshot_id="REPLACE_WITH_SNAPSHOT_ID"):
    sqls = dataset_sql(snapshot_id)
    docs = {"metadata.yaml": {"version": "1.0.0", "type": "Dashboard",
                             "timestamp": "2026-10-27T00:00:00+00:00"}}
    docs["databases/tratte_bi.yaml"] = {
        "database_name": "Tratte BI snapshot", "uuid": uid("database"), "version": "1.0.0",
        "sqlalchemy_uri": "postgresql://tratte_bi_reader@CHANGE_ME/tratte_bi",
        "extra": {}, "allow_dml": False, "allow_ctas": False, "allow_cvas": False,
        "expose_in_sqllab": False, "allow_file_upload": False,
    }
    for name, sql in sqls.items():
        cols = set()
        for ds, _, names in CHARTS:
            if ds == name:
                cols.update(names)
        cols.add("snapshot_id")
        docs[f"datasets/tratte_bi/{name}.yaml"] = {
            "table_name": f"tratte_{name}_v1", "uuid": uid("dataset/"+name),
            "database_uuid": uid("database"), "version": "1.0.0", "sql": sql,
            "schema": "tratte_bi", "main_dttm_col": None,
            "columns": [{"column_name": c, "type": column_type(c),
                         "is_dttm": c in ("purchase_date", "month"),
                         "filterable": True, "groupby": True,
                         "verbose_name": LABELS.get(c, c)} for c in sorted(cols)],
            "metrics": [], "cache_timeout": 0,
        }
    for i, (ds, title, cols) in enumerate(CHARTS):
        params = {"viz_type": "table", "query_mode": "raw", "all_columns": cols,
                  "row_limit": 10000, "page_length": 10, "include_search": True,
                  "table_timestamp_format": "%Y-%m-%d", "allow_render_html": False,
                  "show_cell_bars": True, "color_pn": False,
                  "order_by_cols": [json.dumps(["absolute_contribution", False])] if i==4 else [],
                  "column_config": {c: {"d3NumberFormat": "+,.2f" if c in
                     ("delta", "amount", "amount_delta", "price_effect", "quantity_effect")
                     else ".1%" if "ratio" in c or "coverage_of" in c else ",.2f"}
                     for c in cols if c in {"amount", "running_known_total", "total", "known_total",
                       "delta", "delta_ratio", "amount_delta", "price_effect", "quantity_effect",
                       "paid_unit_price", "median_paid_unit_price", "matched_amount",
                       "positive_item_amount", "matched_coverage_of_positive_items",
                       "previous_price_cost",
                       "actual_same_part_cost", "actual_minus_scenario",
                       "current_coverage_of_positive_items"}}}
        viz = "table"
        if i in (13, 14):
            viz = "echarts_timeseries_bar" if i==13 else "echarts_timeseries_scatter"
            value = "amount_delta" if i==13 else "paid_unit_price"
            params = {"viz_type": viz, "x_axis": "product_id" if i==13 else "observation_key",
                      "metrics": [{"expressionType": "SIMPLE", "aggregate": "MAX",
                                   "column": {"column_name": value}, "label": value}],
                      "groupby": (["direction"] if i==13
                                  else []),
                      "row_limit": 10000, "orientation": "horizontal" if i==13 else "vertical",
                      "show_legend": i==13, "legendType": "scroll", "show_value": True,
                      "x_axis_title": "Товар" if i==13 else "Дата наблюдения • item_id",
                      "y_axis_title": "Вклад, ₽" if i==13 else "Уплаченная цена, ₽/ед.",
                      "color_scheme": "supersetColors", "y_axis_format": ",.2f",
                      "truncateXAxis": False, "truncateYAxis": False,
                      "series_colors": {"Рост +": "#b45d48", "Снижение −": "#287f80"},
                      "markerSize": 8}
        docs[f"charts/{i:02}.yaml"] = {
            "slice_name": title, "viz_type": viz, "uuid": uid(f"chart/{i}"),
            "dataset_uuid": uid("dataset/"+ds), "version": "1.0.0", "params": params,
            "cache_timeout": 0,
        }
    for slug, title, groups, guide in [
        ("tratte-expenses", "Почему изменились расходы?", [[0], [1,2], [3], [13], [4], [5], [6],
          [7], [8], [9]], GUIDE),
        ("tratte-expense-detail", "Товар → наблюдение • приватная детализация",
          [[0], [14], [10], [11], [12]], "## Выберите товар в фильтре product_id\n"
          "Наблюдения — item_total / mapped_quantity только сопоставимых товаров.\n"
          "Медиана — описательная, не цена в факторном тождестве. "
          "Число чеков и строк показано отдельно.\n"
          "Детализация сохраняет исключённые строки; пустая цена означает unavailable.\n"
          "[Назад к факторам](/superset/dashboard/tratte-expenses/). "
          "Сверьте snapshot_id и scope с главным дашбордом.")]:
        meta = {"cross_filters_enabled": False, "color_scheme": "supersetColors",
                "label_colors": {"Рост +": "#b45d48", "Снижение −": "#287f80",
                                 "Без изменения": "#888888", "paid_unit_price": "#287f80"}}
        if slug.endswith("detail"):
            meta["native_filter_configuration"] = [{
                "id": "NATIVE_FILTER-product", "name": "Товар (product_id)",
                "filterType": "filter_select", "targets": [{"datasetUuid": uid("dataset/details"),
                    "column": {"name": "product_id"}}],
                "defaultDataMask": {"extraFormData": {}, "filterState": {}},
                "controlValues": {"multiSelect": False, "enableEmptyFilter": False,
                                  "searchAllOptions": True},
                "cascadeParentIds": [], "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
            }]
        docs[f"dashboards/{slug}.yaml"] = {
            "dashboard_title": title, "slug": slug, "uuid": uid("dashboard/"+slug),
            "version": "1.0.0", "published": False,
            "position": position(title, groups, guide), "metadata": meta,
            "css": ".dashboard {background:#faf9f6;} .dashboard-component-chart-holder "
                   "{border-radius:4px;} h2 {color:#333;} a {color:#287f80;}",
        }
    return docs


def build(output: Path, snapshot_id="REPLACE_WITH_SNAPSHOT_ID"):
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, obj in configs(snapshot_id).items():
            archive.writestr("tratte/"+path, json.dumps(obj, ensure_ascii=False, indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", default="REPLACE_WITH_SNAPSHOT_ID")
    parser.add_argument("--output", type=Path, default=ROOT / "tratte-expenses.zip")
    args = parser.parse_args()
    build(args.output, args.snapshot_id)
