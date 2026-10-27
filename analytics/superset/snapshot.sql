-- Run as owner in a SEPARATE BI database, never in Superset's metadata database.
CREATE SCHEMA IF NOT EXISTS tratte_bi;
REVOKE ALL ON SCHEMA tratte_bi FROM PUBLIC;
CREATE TABLE IF NOT EXISTS tratte_bi.snapshot_v1 (
    snapshot_id text PRIMARY KEY,
    published_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    document jsonb NOT NULL CHECK (document->>'dashboard_contract'='expense_dashboard_v1')
);
REVOKE ALL ON tratte_bi.snapshot_v1 FROM PUBLIC;
-- Owner explicitly grants USAGE schema and SELECT table to a dedicated BI reader.
-- Reader has no access to the source database or mapping manifests.
