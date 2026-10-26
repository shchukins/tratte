-- psql -X -v ON_ERROR_STOP=1 -v bi_role=tratte_bi -f analytics/sql/grants_v1.sql
-- The administrator first creates a dedicated LOGIN role without membership,
-- SUPERUSER, CREATEDB, CREATEROLE, BYPASSRLS or source/default grants.
-- Run in the schema containing the marts (production: public).
BEGIN;
GRANT USAGE ON SCHEMA public TO :"bi_role";
GRANT SELECT ON analytics_receipts_v1, analytics_items_v1,
                analytics_reconciliation_v1, analytics_quality_v1 TO :"bi_role";
ALTER ROLE :"bi_role" SET default_transaction_read_only = on;
COMMIT;
