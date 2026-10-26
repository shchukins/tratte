-- Execute ONLY via analytical_marts.calculate(): mapping is validated in the
-- SAME REPEATABLE READ READ ONLY transaction. No implicit latest mapping.
WITH parameters AS (
    SELECT CAST(:as_of AS timestamptz) AS as_of,
           CAST(:base_month AS date) AS base_month,
           CAST(:current_month AS date) AS current_month,
           CAST(:mapping_version AS text) AS mapping_version,
           CAST(:store AS text) AS store
), months AS (
    SELECT base_month AS month FROM parameters
    UNION ALL SELECT current_month FROM parameters
), observed AS (
    SELECT r.* FROM analytics_receipts_v1 r CROSS JOIN parameters p
    WHERE r.created_at <= p.as_of AND (p.store IS NULL OR r.store = p.store)
), scoped AS (
    SELECT r.* FROM observed r CROSS JOIN parameters p
    WHERE r.status = 'parsed' AND r.purchased_at < p.as_of
      AND r.month IN (p.base_month, p.current_month)
), mapped AS (
    SELECT i.*, r.month, r.currency, r.operation, r.valid_receipt_total,
           m.status AS mapping_status, m.product_id, m.quantity_basis, m.factor,
           prod.comparison_unit,
           CASE
             WHEN r.operation <> 'purchase' THEN 'operation_' || r.operation
             WHEN r.currency IS DISTINCT FROM 'RUB' THEN 'currency_not_rub'
             WHEN NOT r.valid_receipt_total THEN 'invalid_receipt_total'
             WHEN NOT i.valid_item_values THEN 'invalid_item_values'
             WHEN m.status IS DISTINCT FROM 'confirmed'
               THEN 'identity_' || coalesce(m.status, 'unmatched')
             ELSE NULL END AS initial_reason,
           CASE WHEN m.status = 'confirmed' AND i.valid_item_values
                THEN i.source_quantity * m.factor END AS mapped_quantity
    FROM scoped r JOIN analytics_items_v1 i USING (receipt_id)
    CROSS JOIN parameters p
    LEFT JOIN analytics_item_mappings m
      ON m.source_item_id = i.item_id AND m.mapping_version_id = p.mapping_version
    LEFT JOIN analytics_products prod ON prod.id = m.product_id
), product_periods AS (
    SELECT month, product_id, comparison_unit, count(*) AS item_rows,
           count(DISTINCT receipt_id) AS receipt_observations,
           sum(item_total) AS paid_amount, sum(mapped_quantity) AS comparable_quantity,
           sum(item_total) / nullif(sum(mapped_quantity), 0) AS paid_unit_price
    FROM mapped WHERE initial_reason IS NULL
    GROUP BY month, product_id, comparison_unit
), comparable_keys AS (
    SELECT product_id FROM product_periods
    GROUP BY product_id HAVING count(*) = 2 AND min(receipt_observations) >= 2
), classified AS (
    SELECT m.*,
           coalesce(initial_reason,
             CASE WHEN k.product_id IS NOT NULL THEN 'comparable'
                  WHEN (SELECT count(*) FROM product_periods pp
                        WHERE pp.product_id = m.product_id) < 2
                    THEN 'not_observed_in_both_periods'
                  ELSE 'too_few_receipts' END) AS reason
    FROM mapped m LEFT JOIN comparable_keys k USING (product_id)
), coverage_amounts AS (
    SELECT mo.month, count(c.item_id) AS item_rows,
           count(DISTINCT c.receipt_id) AS receipts_with_items,
           coalesce(sum(c.item_total) FILTER (
             WHERE c.currency = 'RUB' AND c.item_total > 0), 0) AS positive_rub_item_amount,
           coalesce(sum(c.item_total) FILTER (
             WHERE c.currency = 'RUB' AND c.item_total > 0
               AND c.operation = 'purchase' AND c.valid_receipt_total), 0)
             AS eligible_purchase_item_amount,
           coalesce(sum(c.item_total) FILTER (WHERE c.reason = 'comparable'), 0)
             AS confirmed_comparable_amount,
           count(DISTINCT c.product_id) FILTER (WHERE c.reason = 'comparable')
             AS comparable_products
    FROM months mo LEFT JOIN classified c USING (month) GROUP BY mo.month
), coverage AS (
    SELECT *, confirmed_comparable_amount / nullif(positive_rub_item_amount, 0)
                AS confirmed_coverage_of_positive_rub,
              confirmed_comparable_amount / nullif(eligible_purchase_item_amount, 0)
                AS confirmed_coverage_of_eligible_purchases,
              CASE WHEN comparable_products > 0 THEN 'available_partial'
                   ELSE 'insufficient_data' END AS comparison_status
    FROM coverage_amounts
), receipt_monthly AS (
    SELECT r.month, r.currency, r.operation, count(*) AS receipt_count,
           count(r.receipt_total) AS known_receipt_totals,
           sum(r.receipt_total) AS recorded_receipt_amount,
           avg(r.receipt_total) AS average_known_receipt,
           count(*) FILTER (WHERE NOT r.valid_receipt_total) AS invalid_receipt_totals
    FROM scoped r GROUP BY r.month, r.currency, r.operation
), reconciliation AS (
    SELECT r.month, r.currency, r.operation, q.*
    FROM scoped r JOIN analytics_reconciliation_v1 q USING (receipt_id)
), reasons AS (
    SELECT month, currency, reason, count(*) AS item_rows,
           count(DISTINCT receipt_id) AS receipt_observations,
           count(item_total) AS known_item_totals, sum(item_total) AS recorded_item_amount
    FROM classified GROUP BY month, currency, reason
), quality AS (
    SELECT month, currency, count(*) AS item_rows,
           count(*) FILTER (WHERE NOT valid_item_values) AS invalid_item_values,
           count(*) FILTER (WHERE item_arithmetic_delta IS NULL) AS unknown_arithmetic,
           count(*) FILTER (WHERE item_arithmetic_mismatch) AS arithmetic_mismatches,
           sum(item_arithmetic_delta) AS signed_arithmetic_delta,
           sum(abs(item_arithmetic_delta)) AS absolute_arithmetic_delta
    FROM classified GROUP BY month, currency
)
SELECT jsonb_build_object(
    'sql_sha256', jsonb_build_object('comparison', CAST(:comparison_sha256 AS text),
                                     'views', CAST(:views_sha256 AS text)),
    'metadata', (SELECT jsonb_build_object(
        'methodology_version', 'expense_marts_v1', 'mapping_version', p.mapping_version,
        'mapping_rules_version', (SELECT rules_version FROM analytics_mapping_versions
            WHERE id = p.mapping_version),
        'mapping_created_at', (SELECT created_at FROM analytics_mapping_versions
            WHERE id = p.mapping_version),
        'as_of', p.as_of, 'calculated_at', transaction_timestamp(),
        'timezone', 'Europe/Moscow', 'base_month', p.base_month,
        'current_month', p.current_month, 'store', p.store,
        'minimum_receipt_observations_per_month', 2,
        'totals_status', 'descriptive_only') FROM parameters p),
    'overview', (SELECT jsonb_build_object(
        'observed_receipts', count(*),
        'missing_purchase_dates', count(*) FILTER (WHERE purchased_at IS NULL),
        'future_purchase_dates', count(*) FILTER (WHERE purchased_at >= p.as_of),
        'unparsed_receipts', count(*) FILTER (WHERE status <> 'parsed'),
        'last_purchase_at', max(purchased_at) FILTER (WHERE purchased_at < p.as_of)
        ) FROM observed CROSS JOIN parameters p),
    'freshness', (SELECT jsonb_build_object(
        'last_completed_sync', (SELECT max(finished_at) FROM sync_runs
            WHERE status = 'completed' AND finished_at <= p.as_of),
        'failed_syncs_last_day', (SELECT count(*) FROM sync_runs WHERE status = 'failed'
            AND started_at >= p.as_of - interval '1 day' AND started_at <= p.as_of),
        'last_sync_status', (SELECT status FROM sync_runs s
            WHERE s.started_at <= p.as_of ORDER BY started_at DESC, id DESC LIMIT 1)
        ) FROM parameters p),
    'receipt_monthly', coalesce((SELECT jsonb_agg(to_jsonb(t)
        ORDER BY month, currency, operation) FROM receipt_monthly t), '[]'::jsonb),
    'items', coalesce((SELECT jsonb_agg(to_jsonb(t) || jsonb_build_object(
        'comparable_quantity', CASE WHEN initial_reason IS NULL THEN mapped_quantity END,
        'paid_unit_price', CASE WHEN initial_reason IS NULL
          THEN item_total / nullif(mapped_quantity, 0) END) ORDER BY item_id)
        FROM classified t), '[]'::jsonb),
    'product_monthly', coalesce((SELECT jsonb_agg(to_jsonb(t) || jsonb_build_object(
        'passes_pair_threshold', product_id IN (SELECT product_id FROM comparable_keys))
        ORDER BY month, product_id) FROM product_periods t), '[]'::jsonb),
    'coverage', (SELECT jsonb_agg(to_jsonb(t) ORDER BY month) FROM coverage t),
    'reasons', coalesce((SELECT jsonb_agg(to_jsonb(t)
        ORDER BY month, currency, reason) FROM reasons t), '[]'::jsonb),
    'quality', coalesce((SELECT jsonb_agg(to_jsonb(t)
        ORDER BY month, currency) FROM quality t), '[]'::jsonb),
    'reconciliation', coalesce((SELECT jsonb_agg(to_jsonb(t)
        ORDER BY receipt_id) FROM reconciliation t), '[]'::jsonb)
)::text
