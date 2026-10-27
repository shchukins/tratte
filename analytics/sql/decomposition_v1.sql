-- Input is the validated marts document from the SAME read-only snapshot.
WITH document AS (SELECT CAST(:document AS jsonb) AS d),
params AS (
    SELECT (d->'metadata'->>'base_month')::date AS base_month,
           (d->'metadata'->>'current_month')::date AS current_month,
           CAST(:category AS text) AS category_filter FROM document
), months AS (
    SELECT base_month AS month FROM params UNION ALL SELECT current_month FROM params
), items AS (
    SELECT i.* FROM document, jsonb_to_recordset(d->'items') AS i(
        item_id bigint, receipt_id bigint, month date, category text, currency text,
        operation text, valid_receipt_total boolean, item_total numeric,
        initial_reason text, product_id text, comparison_unit text, mapped_quantity numeric
    ), params p WHERE p.category_filter IS NULL OR i.category = p.category_filter
), eligible AS (
    SELECT * FROM items WHERE initial_reason IS NULL
), products AS (
    SELECT product_id, min(category) AS category, min(comparison_unit) AS comparison_unit,
           -- Unknown or conflicting category cannot receive an invented attribution.
           count(DISTINCT category) + CASE WHEN bool_or(category IS NULL) THEN 1 ELSE 0 END
             AS categories,
           count(*) FILTER (WHERE month = p.base_month) AS rows0,
           count(*) FILTER (WHERE month = p.current_month) AS rows1,
           count(DISTINCT receipt_id) FILTER (WHERE month = p.base_month) AS receipts0,
           count(DISTINCT receipt_id) FILTER (WHERE month = p.current_month) AS receipts1,
           coalesce(sum(item_total) FILTER (WHERE month = p.base_month), 0) AS s0,
           coalesce(sum(item_total) FILTER (WHERE month = p.current_month), 0) AS s1,
           sum(mapped_quantity) FILTER (WHERE month = p.base_month) AS q0,
           sum(mapped_quantity) FILTER (WHERE month = p.current_month) AS q1
    FROM eligible CROSS JOIN params p GROUP BY product_id
), classified AS (
    SELECT *, CASE WHEN categories > 1 THEN 'category_conflict'
                   WHEN rows0 = 0 THEN 'current_only'
                   WHEN rows1 = 0 THEN 'base_only'
                   WHEN receipts0 >= 2 AND receipts1 >= 2 THEN 'comparable'
                   ELSE 'too_few_receipts' END AS reason,
           s0 / nullif(q0, 0) AS p0, s1 / nullif(q1, 0) AS p1
    FROM products
), factors AS (
    SELECT *, CASE WHEN reason = 'comparable' THEN (p1-p0)*(q1+q0)/2 END AS price_effect,
           CASE WHEN reason = 'comparable' THEN (q1-q0)*(p1+p0)/2 END AS quantity_effect,
           CASE WHEN reason = 'comparable' THEN q1*p0 END AS previous_price_scenario,
           s1-s0 AS amount_delta
    FROM classified
), assigned AS (
    SELECT i.*, coalesce(i.initial_reason, f.reason) AS reason
    FROM items i LEFT JOIN classified f USING (product_id)
), coverage AS (
    SELECT m.month, count(i.item_id) AS item_rows,
           count(i.item_id) FILTER (WHERE i.item_total IS NULL) AS missing_item_totals,
           coalesce(sum(i.item_total), 0) AS known_item_amount,
           coalesce(sum(i.item_total) FILTER (WHERE i.item_total > 0), 0) AS positive_item_amount,
           coalesce(sum(i.item_total) FILTER (WHERE reason = 'comparable'), 0) AS matched_amount,
           coalesce(sum(i.item_total) FILTER (WHERE reason = 'current_only'), 0) AS current_only,
           coalesce(sum(i.item_total) FILTER (WHERE reason = 'base_only'), 0) AS base_only,
           coalesce(sum(i.item_total) FILTER (
               WHERE reason NOT IN ('comparable', 'current_only', 'base_only')), 0) AS unmatched
    FROM months m LEFT JOIN assigned i ON i.month=m.month
      AND i.currency='RUB' AND i.operation='purchase'
    GROUP BY m.month
), receipts AS (
    SELECT m.month, coalesce(sum(r.recorded_receipt_amount), 0) AS known_receipt_amount,
           coalesce(sum(r.receipt_count-r.known_receipt_totals), 0) AS missing_receipt_totals
    FROM months m CROSS JOIN document
    LEFT JOIN LATERAL jsonb_to_recordset(d->'receipt_monthly') AS r(
        month date, currency text, operation text, recorded_receipt_amount numeric,
        receipt_count bigint, known_receipt_totals bigint
    ) ON r.month=m.month AND r.currency='RUB' AND r.operation='purchase'
    GROUP BY m.month
), components AS (
    SELECT 'price' AS component, coalesce(sum(price_effect), 0) AS amount FROM factors
    UNION ALL SELECT 'quantity', coalesce(sum(quantity_effect), 0) FROM factors
    UNION ALL SELECT 'current_only', sum(current_only) FROM coverage
    UNION ALL SELECT 'base_only', -sum(base_only) FROM coverage
    UNION ALL SELECT 'unmatched', sum(CASE WHEN month=p.current_month THEN unmatched
                                                        ELSE -unmatched END)
        FROM coverage CROSS JOIN params p
    UNION ALL SELECT 'reconciliation', CASE WHEN p.category_filter IS NULL THEN
        sum(CASE WHEN c.month=p.current_month THEN r.known_receipt_amount-c.known_item_amount
                                             ELSE c.known_item_amount-r.known_receipt_amount END)
        ELSE NULL END
        FROM coverage c JOIN receipts r USING(month) CROSS JOIN params p GROUP BY p.category_filter
), target AS (
    SELECT sum(CASE WHEN c.month=p.current_month THEN
                    CASE WHEN p.category_filter IS NULL THEN r.known_receipt_amount
                         ELSE c.known_item_amount END
                    ELSE -CASE WHEN p.category_filter IS NULL THEN r.known_receipt_amount
                               ELSE c.known_item_amount END END) AS delta,
           sum(c.missing_item_totals) + CASE WHEN p.category_filter IS NULL
               THEN sum(r.missing_receipt_totals) ELSE 0 END AS missing_values
    FROM coverage c JOIN receipts r USING(month) CROSS JOIN params p GROUP BY p.category_filter
), rounded AS (
    SELECT component, amount, round(amount, 2) AS amount_rounded FROM components
), category_factors AS (
    SELECT category, sum(price_effect) AS price_effect, sum(quantity_effect) AS quantity_effect,
           sum(amount_delta) FILTER (WHERE reason='comparable') AS matched_delta,
           sum(amount_delta) FILTER (WHERE reason='current_only') AS current_only_delta,
           sum(amount_delta) FILTER (WHERE reason='base_only') AS base_only_delta
    FROM factors GROUP BY category
), category_amounts AS (
    SELECT i.category, sum(CASE WHEN i.month=p.current_month THEN i.item_total
                               ELSE -i.item_total END) AS known_item_delta,
           count(*) FILTER (WHERE i.item_total IS NULL) AS missing_item_totals
    FROM assigned i CROSS JOIN params p
    WHERE i.currency='RUB' AND i.operation='purchase' GROUP BY i.category
), categories AS (
    SELECT a.*, coalesce(f.price_effect, 0) AS price_effect,
           coalesce(f.quantity_effect, 0) AS quantity_effect,
           coalesce(f.current_only_delta, 0) AS current_only_delta,
           coalesce(f.base_only_delta, 0) AS base_only_delta,
           a.known_item_delta-coalesce(f.matched_delta, 0)
             -coalesce(f.current_only_delta, 0)-coalesce(f.base_only_delta, 0) AS unmatched_delta
    FROM category_amounts a LEFT JOIN category_factors f
      ON a.category IS NOT DISTINCT FROM f.category
), reasons AS (
    SELECT month, currency, operation, reason, count(*) AS item_rows,
           count(*) FILTER (WHERE item_total IS NULL) AS missing_item_totals,
           sum(item_total) AS known_item_amount
    FROM assigned GROUP BY month, currency, operation, reason
)
SELECT jsonb_build_object(
    'metadata', (SELECT d->'metadata' || jsonb_build_object(
        'methodology_version', 'expense_decomposition_v1',
        'input_methodology_version', d->'metadata'->>'methodology_version',
        'pair_id', (SELECT base_month::text || '/' || current_month::text FROM params),
        'category', CAST(:category AS text), 'currency', 'RUB', 'operation', 'purchase',
        'bridge_target', CASE WHEN CAST(:category AS text) IS NULL
                             THEN 'known_receipt_totals' ELSE 'known_category_item_totals' END,
        'rounding', 'numeric round half away from zero; explicit rounding component',
        'receipt_scope', 'store; category never allocates receipt totals'
    ) FROM document),
    'sql_sha256', (SELECT d->'sql_sha256' || jsonb_build_object(
        'decomposition', CAST(:decomposition_sha256 AS text)) FROM document),
    'comparison_status', CASE WHEN EXISTS(SELECT 1 FROM factors WHERE reason='comparable')
                              THEN 'available_partial' ELSE 'insufficient_data' END,
    'bridge_status', (SELECT CASE WHEN missing_values=0 THEN 'available'
                                 ELSE 'insufficient_data' END FROM target),
    'known_target_delta', (SELECT delta FROM target),
    'target_delta', (SELECT CASE WHEN missing_values=0 THEN delta END FROM target),
    'bridge', (SELECT jsonb_agg(to_jsonb(r) ORDER BY component) FROM (
        SELECT * FROM rounded UNION ALL
        SELECT 'rounding', NULL, round(delta, 2)-(SELECT sum(amount_rounded) FROM rounded)
        FROM target
    ) r),
    'products', coalesce((SELECT jsonb_agg(to_jsonb(f) ORDER BY product_id) FROM factors f),
                         '[]'::jsonb),
    'categories', coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY category) FROM categories c),
                           '[]'::jsonb),
    'coverage', (SELECT jsonb_agg(to_jsonb(c) || jsonb_build_object(
        'matched_coverage_of_positive_items', matched_amount/nullif(positive_item_amount, 0)
    ) ORDER BY month) FROM coverage c),
    'scenario', (SELECT jsonb_build_object(
        'status', CASE WHEN count(*)>0 THEN 'available_partial' ELSE 'insufficient_data' END,
        'previous_price_cost', sum(previous_price_scenario),
        'actual_same_part_cost', sum(s1),
        'actual_minus_scenario', sum(s1-previous_price_scenario),
        'product_count', count(*),
        'current_coverage_of_positive_items', (SELECT matched_amount/nullif(positive_item_amount, 0)
            FROM coverage CROSS JOIN params p WHERE month=p.current_month)
    ) FROM factors WHERE reason='comparable'),
    'reasons', coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY month,currency,operation,reason)
                         FROM reasons r), '[]'::jsonb),
    'receipt_monthly', (SELECT d->'receipt_monthly' FROM document),
    'freshness', (SELECT d->'freshness' FROM document)
)::text
