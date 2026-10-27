-- Run after mapping validation, marts and factors in the SAME source snapshot.
WITH source AS (SELECT CAST(:marts AS jsonb) m, CAST(:factors AS jsonb) f),
items AS (
    SELECT i.* FROM source, jsonb_to_recordset(m->'items') AS i(
        item_id bigint, receipt_id bigint, month date, category text, currency text,
        operation text, source_quantity numeric, source_unit text, item_total numeric,
        product_id text, comparison_unit text, mapped_quantity numeric, initial_reason text
    ) WHERE CAST(:category AS text) IS NULL OR i.category=CAST(:category AS text)
), products AS (
    SELECT p.* FROM source, jsonb_to_recordset(f->'products') AS p(product_id text, reason text)
), details AS (
    SELECT i.*, r.purchased_at, r.purchase_date, r.store,
           coalesce(i.initial_reason,p.reason) AS reason,
           CASE WHEN i.initial_reason IS NULL AND p.reason='comparable'
                THEN i.item_total/nullif(i.mapped_quantity,0) END AS paid_unit_price
    FROM items i JOIN analytics_receipts_v1 r USING(receipt_id)
    LEFT JOIN products p USING(product_id)
), medians AS (
    SELECT product_id, comparison_unit, month, store, count(*) AS item_observations,
           count(DISTINCT receipt_id) AS receipt_observations,
           -- Continuous median using numeric arithmetic, distinct from P=SUM(S)/SUM(Q).
           (array_agg(paid_unit_price ORDER BY paid_unit_price))[(count(*)::int+1)/2]
             /2 + (array_agg(paid_unit_price ORDER BY paid_unit_price))[(count(*)::int+2)/2]
             /2 AS median_paid_unit_price
    FROM details WHERE paid_unit_price IS NOT NULL
    GROUP BY product_id,comparison_unit,month,store
)
SELECT (f || jsonb_build_object(
    'dashboard_contract','expense_dashboard_v1',
    'details',coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY item_id) FROM details d),'[]'),
    'medians',coalesce((SELECT jsonb_agg(to_jsonb(d)
        ORDER BY product_id,month,store) FROM medians d),'[]'),
    'overview',m->'overview',
    'reconciliation',m->'reconciliation',
    'sql_sha256',(f->'sql_sha256') || jsonb_build_object('dashboard',CAST(:dashboard_sha AS text))
))::text FROM source
