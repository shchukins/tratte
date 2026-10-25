-- PostgreSQL 16 / psql. Aggregate output only; run inside a READ ONLY transaction.
-- Required variables: as_of (with offset), timezone, base_month, current_month.
-- Names are used only inside candidate grouping, never returned as identifiers.
WITH parameters AS (
    SELECT :'as_of'::timestamptz AS as_of, :'timezone'::text AS timezone,
           :'base_month'::date AS base_month, :'current_month'::date AS current_month,
           2 AS min_receipts
),
observed_receipts AS (
    SELECT r.*, date_trunc('month', r.purchased_at AT TIME ZONE p.timezone)::date AS month,
           CASE lower(btrim(r.operation_type))
               WHEN 'приход' THEN 'purchase'
               WHEN 'возврат прихода' THEN 'purchase_return'
               ELSE CASE WHEN nullif(btrim(r.operation_type), '') IS NULL
                         THEN 'unknown' ELSE 'other' END
           END AS operation_class
    FROM receipts r CROSS JOIN parameters p
    WHERE r.created_at <= p.as_of
),
observed_items AS (
    SELECT i.*, r.month, r.operation_class, r.currency, r.total AS receipt_total,
           CASE lower(btrim(i.unit))
               WHEN 'шт' THEN 'piece' WHEN 'шт.' THEN 'piece'
               WHEN 'шт. или ед.' THEN 'piece' WHEN 'кг' THEN 'kg'
               ELSE 'unsupported' END AS audit_unit,
           CASE
               WHEN r.operation_class <> 'purchase' THEN 'operation_' || r.operation_class
               WHEN r.currency IS DISTINCT FROM 'RUB' THEN 'currency_not_rub'
               WHEN r.total IS NULL OR r.total <= 0 THEN 'invalid_receipt_total'
               WHEN i.total IS NULL OR i.total <= 0 OR i.quantity IS NULL
                    OR i.quantity <= 0 OR i.unit_price IS NULL OR i.unit_price <= 0
                    THEN 'invalid_item_values'
               WHEN nullif(btrim(i.normalized_name), '') IS NULL THEN 'missing_name'
               WHEN lower(btrim(i.unit)) NOT IN ('шт', 'шт.', 'шт. или ед.', 'кг')
                    OR i.unit IS NULL THEN 'unsupported_unit'
               ELSE NULL
           END AS exclusion_reason
    FROM receipt_items i JOIN observed_receipts r ON r.id = i.receipt_id
    CROSS JOIN parameters p
    WHERE r.status = 'parsed' AND r.purchased_at < p.as_of
),
months AS (
    SELECT base_month AS month FROM parameters
    UNION SELECT current_month FROM parameters
),
pair_items AS (
    SELECT i.* FROM observed_items i JOIN months m USING (month)
),
candidate_periods AS (
    SELECT normalized_name, audit_unit, month, count(DISTINCT receipt_id) AS receipts
    FROM pair_items WHERE exclusion_reason IS NULL
    GROUP BY normalized_name, audit_unit, month
),
candidates AS (
    SELECT normalized_name, audit_unit, count(*) AS periods, min(receipts) AS min_receipts
    FROM candidate_periods GROUP BY normalized_name, audit_unit
),
classified AS (
    SELECT i.*, COALESCE(i.exclusion_reason,
           CASE WHEN c.periods < 2 THEN 'not_observed_in_both_periods'
                WHEN c.min_receipts < p.min_receipts THEN 'too_few_receipts'
                ELSE 'identity_unverified' END) AS reason,
           c.periods = 2 AS repeated_name_in_pair,
           c.periods = 2 AND c.min_receipts >= p.min_receipts AS repeated_candidate
    FROM pair_items i CROSS JOIN parameters p
    LEFT JOIN candidates c USING (normalized_name, audit_unit)
),
reason_groups AS (
    SELECT month, currency, reason, count(*) AS item_rows, sum(total) AS item_amount
    FROM classified GROUP BY month, currency, reason
),
pair_summary AS (
    SELECT m.month,
           count(i.id) AS item_rows,
           count(i.id) FILTER (WHERE i.currency = 'RUB' AND i.total > 0) AS positive_rub_rows,
           COALESCE(sum(i.total) FILTER (WHERE i.currency = 'RUB' AND i.total > 0), 0)
               AS positive_rub_item_amount,
           COALESCE(sum(i.total) FILTER (WHERE i.currency = 'RUB' AND i.total > 0
               AND i.operation_class = 'purchase' AND i.receipt_total > 0), 0)
               AS eligible_purchase_item_amount,
           COALESCE(sum(i.total) FILTER (WHERE i.currency = 'RUB' AND i.total > 0
               AND i.operation_class = 'unknown'), 0) AS unknown_operation_item_amount,
           COALESCE(sum(i.total) FILTER (WHERE i.exclusion_reason IS NULL
               AND i.repeated_name_in_pair), 0) AS repeated_name_candidate_amount,
           COALESCE(sum(i.total) FILTER (WHERE i.exclusion_reason IS NULL
               AND i.repeated_candidate), 0) AS repeated_candidate_amount,
           count(DISTINCT (i.normalized_name, i.audit_unit)) FILTER (
               WHERE i.exclusion_reason IS NULL AND i.repeated_candidate)
               AS repeated_candidate_keys
    FROM months m LEFT JOIN classified i USING (month) GROUP BY m.month
),
receipt_item_totals AS (
    SELECT receipt_id, count(*) AS item_rows, sum(total) AS item_amount,
           count(*) FILTER (WHERE total IS NULL) AS null_item_totals
    FROM receipt_items GROUP BY receipt_id
),
receipt_months AS (
    SELECT r.month, r.currency, count(*) AS receipts, sum(r.total) AS receipt_amount,
           count(*) FILTER (WHERE r.operation_class = 'purchase') AS purchase_receipts,
           count(*) FILTER (WHERE r.operation_class = 'unknown') AS unknown_operation_receipts,
           count(*) FILTER (WHERE r.total IS NULL OR r.total <= 0) AS invalid_receipt_totals,
           count(*) FILTER (WHERE r.total IS NULL) AS null_receipt_totals,
           count(*) FILTER (WHERE r.total = 0) AS zero_receipt_totals,
           count(*) FILTER (WHERE r.total < 0) AS negative_receipt_totals,
           count(*) FILTER (WHERE s.receipt_id IS NULL OR s.null_item_totals > 0
               OR r.total IS NULL) AS unreconcilable_receipts,
           count(*) FILTER (WHERE s.null_item_totals = 0
               AND abs(r.total - s.item_amount) > 0.01) AS mismatched_receipts,
           sum(r.total - s.item_amount) FILTER (WHERE s.null_item_totals = 0)
               AS receipt_minus_items,
           sum(abs(r.total - s.item_amount)) FILTER (WHERE s.null_item_totals = 0)
               AS absolute_receipt_minus_items
    FROM observed_receipts r CROSS JOIN parameters p
    LEFT JOIN receipt_item_totals s ON s.receipt_id = r.id
    WHERE r.status = 'parsed' AND r.purchased_at < p.as_of
    GROUP BY r.month, r.currency
),
receipt_statuses AS (
    SELECT status, count(*) AS receipts FROM observed_receipts GROUP BY status
),
operations AS (
    SELECT operation_class, count(*) AS receipts FROM observed_receipts
    WHERE status = 'parsed' GROUP BY operation_class
),
unit_groups AS (
    SELECT audit_unit, count(*) AS item_rows,
           count(*) FILTER (WHERE total = 0) AS zero_totals,
           count(*) FILTER (WHERE total < 0) AS negative_totals,
           count(*) FILTER (WHERE quantity = 0) AS zero_quantities,
           count(*) FILTER (WHERE quantity < 0) AS negative_quantities,
           count(*) FILTER (WHERE unit_price = 0) AS zero_prices,
           count(*) FILTER (WHERE unit_price < 0) AS negative_prices,
           count(*) FILTER (WHERE total <= 0 OR quantity <= 0 OR unit_price <= 0
               OR total IS NULL OR quantity IS NULL OR unit_price IS NULL) AS invalid_values,
           count(*) FILTER (WHERE abs(total - unit_price * quantity) > 0.01)
               AS arithmetic_mismatches
    FROM observed_items GROUP BY audit_unit
),
repeat_groups AS (
    SELECT normalized_name, audit_unit, count(*) AS item_rows,
           count(DISTINCT month) AS months, count(DISTINCT receipt_id) AS receipts
    FROM observed_items GROUP BY normalized_name, audit_unit
),
duplicate_groups AS (
    SELECT receipt_id, original_name, quantity, unit, unit_price, total, count(*) AS n
    FROM observed_items GROUP BY receipt_id, original_name, quantity, unit, unit_price, total
    HAVING count(*) > 1
),
sync_summary AS (
    SELECT max(finished_at) FILTER (WHERE status = 'completed' AND finished_at <= p.as_of)
               AS last_completed_at,
           max(started_at) AS latest_started_at,
           (array_agg(status ORDER BY started_at DESC))[1] AS latest_status,
           count(*) FILTER (WHERE status = 'failed') AS failed_runs,
           count(*) FILTER (WHERE status = 'failed'
               AND started_at >= p.as_of - interval '24 hours') AS failed_runs_last_24h,
           count(*) FILTER (WHERE finished_at IS NULL) AS unfinished_runs
    FROM sync_runs CROSS JOIN parameters p WHERE started_at <= p.as_of
)
SELECT jsonb_build_object(
    'contract_version', 'expense_comparison_audit_v1',
    'parameters', (SELECT to_jsonb(p) FROM parameters p),
    'database_snapshot_at', transaction_timestamp(),
    'overview', (SELECT jsonb_build_object(
        'parsed_receipts', count(*) FILTER (WHERE status = 'parsed'),
        'first_purchase', min(purchased_at) FILTER (WHERE status = 'parsed'),
        'last_purchase', max(purchased_at) FILTER (WHERE status = 'parsed'),
        'missing_purchase_date', count(*) FILTER (
            WHERE status = 'parsed' AND purchased_at IS NULL),
        'future_purchase_date', count(*) FILTER (
            WHERE status = 'parsed' AND purchased_at >= p.as_of),
        'latest_receipt_created_at', max(created_at),
        'latest_receipt_updated_at', max(updated_at),
        'product_aliases', (SELECT count(*) FROM product_aliases),
        'duplicate_item_groups', (SELECT count(*) FROM duplicate_groups),
        'duplicate_item_extra_rows', (SELECT COALESCE(sum(n-1), 0) FROM duplicate_groups)
    ) FROM observed_receipts CROSS JOIN parameters p),
    'receipt_statuses', (SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY status), '[]')
        FROM receipt_statuses s),
    'operations', (SELECT COALESCE(jsonb_agg(to_jsonb(o) ORDER BY operation_class), '[]')
        FROM operations o),
    'monthly_receipts', (SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY month, currency), '[]')
        FROM receipt_months m),
    'units', (SELECT COALESCE(jsonb_agg(to_jsonb(u) ORDER BY audit_unit), '[]') FROM unit_groups u),
    'repeats', (SELECT jsonb_build_object(
        'name_unit_keys', count(*), 'item_rows', COALESCE(sum(item_rows), 0),
        'keys_in_two_months', count(*) FILTER (WHERE months >= 2),
        'keys_in_three_months', count(*) FILTER (WHERE months >= 3)
    ) FROM repeat_groups),
    'pair_coverage', (SELECT jsonb_agg(to_jsonb(s) || jsonb_build_object(
        'candidate_coverage_of_positive_rub', repeated_candidate_amount
            / nullif(positive_rub_item_amount, 0),
        'candidate_coverage_of_eligible_purchases', repeated_candidate_amount
            / nullif(eligible_purchase_item_amount, 0),
        'confirmed_comparable_amount', 0,
        'confirmed_coverage_of_positive_rub', 0 / nullif(positive_rub_item_amount, 0),
        'confirmed_coverage_of_eligible_purchases', 0 / nullif(eligible_purchase_item_amount, 0)
    ) ORDER BY month) FROM pair_summary s),
    'pair_reasons', (SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY month, currency, reason), '[]')
        FROM reason_groups r),
    'sync', (SELECT to_jsonb(s) FROM sync_summary s)
);
