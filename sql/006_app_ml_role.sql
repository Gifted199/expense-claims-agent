-- 006: read-only role for the one-off ML training job (decision D16).
-- Reads claims and reference data to build features, and claim_labels ONLY to evaluate
-- the models. It can't write anything. Its password lives in the separate secret
-- akinsds1126-expense-db-ml, set by `python -m expense_agent.db.app_roles`.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ml') THEN
        CREATE ROLE app_ml LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_ml', current_database());
END;
$$;

GRANT USAGE ON SCHEMA public TO app_ml;
GRANT SELECT ON claims, employees, policy_limits, expense_categories, claim_labels TO app_ml;
ALTER ROLE app_ml SET default_transaction_read_only = on;
ALTER ROLE app_ml SET statement_timeout = '60s';
