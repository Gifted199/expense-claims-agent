-- 004: application roles and least-privilege grants.
-- Roles are created WITHOUT a password here; passwords are set by
-- `python -m expense_agent.db.app_roles` from the Secrets Manager secret
-- akinsds1126-expense-db-app, so no secret ever lives in SQL or git.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_readonly') THEN
        CREATE ROLE app_readonly LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_writer') THEN
        CREATE ROLE app_writer LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_readonly, app_writer', current_database());
END;
$$;

-- Start from nothing, then grant exactly what each role needs.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_readonly, app_writer;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_readonly, app_writer;
GRANT USAGE ON SCHEMA public TO app_readonly, app_writer;

-- app_readonly: query tools (get_my_claims, spending_summary, ...). SELECT only.
-- No access to claim_labels (eval only), audit_log or schema_migrations.
GRANT SELECT ON departments, employees, expense_categories, policy_limits, claims, approvals
    TO app_readonly;
ALTER ROLE app_readonly SET default_transaction_read_only = on;
ALTER ROLE app_readonly SET statement_timeout = '15s';

-- app_writer: submit_claim workflow + manager decisions in the UI.
-- Reads limits and claims to decide; writes claims/approvals; audit_log is INSERT only
-- (append-only trail). No DELETE anywhere, no access to claim_labels.
GRANT SELECT ON departments, employees, expense_categories, policy_limits, claims, approvals
    TO app_writer;
GRANT INSERT, UPDATE ON claims, approvals TO app_writer;
GRANT INSERT ON audit_log TO app_writer;
GRANT USAGE ON SEQUENCE claims_claim_id_seq, approvals_approval_id_seq, audit_log_audit_id_seq
    TO app_writer;
ALTER ROLE app_writer SET statement_timeout = '30s';
