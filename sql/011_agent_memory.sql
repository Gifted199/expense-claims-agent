-- 011: short-term chat memory for the agent (decision D40).
-- LangGraph's PostgresSaver keeps its checkpoint tables in their OWN schema, owned by a narrow
-- role, app_memory, whose password lives in its own Secrets Manager entry (set by
-- expense_agent.db.app_roles, never in SQL). app_memory gets nothing on the business tables
-- in "public": it can't read claims, employees, labels or policy data.

CREATE SCHEMA IF NOT EXISTS agent_memory;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_memory') THEN
        CREATE ROLE app_memory LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_memory', current_database());
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_memory;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_memory;
REVOKE ALL ON SCHEMA agent_memory FROM PUBLIC;
-- The saver's setup() creates and migrates its own tables here, so it needs CREATE.
GRANT USAGE, CREATE ON SCHEMA agent_memory TO app_memory;
ALTER ROLE app_memory SET search_path = agent_memory;
ALTER ROLE app_memory SET statement_timeout = '15s';
