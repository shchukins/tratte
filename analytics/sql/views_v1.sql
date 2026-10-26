-- Owner-installed views. No raw names, mail, fiscal fields, addresses or manifests.
-- Ordinary views reflect current source state, not a historical ledger.
CREATE OR REPLACE VIEW analytics_receipts_v1 AS
SELECT r.id AS receipt_id, r.purchased_at,
       (r.purchased_at AT TIME ZONE 'Europe/Moscow')::date AS purchase_date,
       date_trunc('month', r.purchased_at AT TIME ZONE 'Europe/Moscow')::date AS month,
       r.created_at, r.updated_at, r.store, r.status, r.currency,
       CASE lower(btrim(r.operation_type))
         WHEN 'приход' THEN 'purchase'
         WHEN 'возврат прихода' THEN 'purchase_return'
         ELSE CASE WHEN nullif(btrim(r.operation_type), '') IS NULL
                   THEN 'unknown' ELSE 'other' END END AS operation,
       r.total AS receipt_total,
       coalesce(r.total > 0, false) AS valid_receipt_total
FROM receipts r;

CREATE OR REPLACE VIEW analytics_items_v1 AS
SELECT i.id AS item_id, i.receipt_id, i.category,
       i.quantity AS source_quantity, i.unit AS source_unit,
       i.unit_price AS source_unit_price, i.total AS item_total,
       coalesce(i.total > 0 AND i.quantity > 0 AND i.unit_price > 0, false)
         AS valid_item_values,
       i.total - i.unit_price * i.quantity AS item_arithmetic_delta,
       abs(i.total - i.unit_price * i.quantity) > 0.01 AS item_arithmetic_mismatch
FROM receipt_items i;

CREATE OR REPLACE VIEW analytics_reconciliation_v1 AS
SELECT r.receipt_id, count(i.item_id) AS item_rows,
       count(i.item_total) AS known_item_totals, sum(i.item_total) AS known_item_amount,
       CASE WHEN count(i.item_id) > 0 AND count(i.item_id) = count(i.item_total)
            THEN r.receipt_total - sum(i.item_total) END AS receipt_minus_items,
       CASE WHEN count(i.item_id) = 0 THEN 'no_items'
            WHEN r.receipt_total IS NULL THEN 'missing_receipt_total'
            WHEN count(i.item_id) <> count(i.item_total) THEN 'missing_item_totals'
            WHEN abs(r.receipt_total - sum(i.item_total)) > 0.01 THEN 'mismatch'
            ELSE 'balanced' END AS reconciliation_status
FROM analytics_receipts_v1 r LEFT JOIN analytics_items_v1 i USING (receipt_id)
GROUP BY r.receipt_id, r.receipt_total;

CREATE OR REPLACE VIEW analytics_quality_v1 AS
SELECT r.month, r.currency, r.operation, r.status,
       count(*) AS receipt_count, count(r.receipt_total) AS known_receipt_totals,
       sum(r.receipt_total) AS recorded_receipt_amount,
       sum(q.item_rows) AS item_rows, sum(q.known_item_totals) AS known_item_totals,
       sum(q.known_item_amount) AS known_item_amount,
       count(q.receipt_minus_items) AS reconcilable_receipts,
       sum(q.receipt_minus_items) AS receipt_minus_items,
       sum(abs(q.receipt_minus_items)) AS absolute_receipt_minus_items,
       count(*) FILTER (WHERE q.reconciliation_status = 'mismatch') AS mismatched_receipts,
       count(*) FILTER (WHERE q.reconciliation_status = 'no_items') AS receipts_without_items,
       count(*) FILTER (WHERE q.reconciliation_status = 'missing_item_totals')
         AS receipts_with_missing_item_totals,
       transaction_timestamp() AS snapshot_at,
       'expense_marts_v1'::text AS methodology_version
FROM analytics_receipts_v1 r JOIN analytics_reconciliation_v1 q USING (receipt_id)
GROUP BY r.month, r.currency, r.operation, r.status;

-- No EXECUTE or table-wide/default grants. Installation never grants PUBLIC access.
REVOKE ALL ON analytics_receipts_v1, analytics_items_v1,
              analytics_reconciliation_v1, analytics_quality_v1 FROM PUBLIC;
