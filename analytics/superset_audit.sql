-- Run directly against Superset's metadata DB in a READ ONLY transaction.
-- Do not initialize the Superset application: startup can sync configuration.
-- IDs are discovered from the Tratte dataset names, not assumed stable.
WITH datasets AS (
    SELECT id, table_name, database_id, sql IS NOT NULL AS virtual,
           md5(sql) AS sql_md5,
           sql ILIKE '%ВОЗВРАТ%' AS has_return_text_rule,
           sql ILIKE '%ELSE total%' OR sql ILIKE '%ELSE ri.total%' AS passes_other_totals
    FROM tables WHERE table_name IN ('tratte_receipts', 'tratte_items', 'tratte_daily_spend')
), charts AS (
    SELECT id, datasource_id, viz_type FROM slices
    WHERE datasource_type = 'table' AND datasource_id IN (SELECT id FROM datasets)
), boards AS (
    SELECT d.id, d.published,
           (SELECT jsonb_agg((c.value->'meta'->>'chartId')::int ORDER BY
               (c.value->'meta'->>'chartId')::int)
            FROM jsonb_each(d.position_json::jsonb) c
            WHERE c.value->>'type' = 'CHART') AS layout_chart_ids,
           d.json_metadata::jsonb->'cross_filters_enabled' AS cross_filters,
           COALESCE(d.json_metadata::jsonb->'native_filter_configuration', '[]'::jsonb)
               AS native_filters
    FROM dashboards d WHERE d.id IN (
        SELECT dashboard_id FROM dashboard_slices WHERE slice_id IN (SELECT id FROM charts)
    )
)
SELECT jsonb_build_object(
    'observed_at', transaction_timestamp(),
    'datasets', (SELECT jsonb_agg(to_jsonb(d) ORDER BY id) FROM datasets d),
    'charts', (SELECT jsonb_agg(to_jsonb(c) ORDER BY id) FROM charts c),
    'dashboards', (SELECT jsonb_agg(to_jsonb(b) ORDER BY id) FROM boards b)
);
