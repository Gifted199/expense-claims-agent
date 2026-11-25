-- 009: app_ingest never deletes policy documents (decision D28 amended).
-- Re-ingest replaces chunks (DELETE on policy_chunks stays) and upserts documents; deleting a
-- document would cascade to all of its chunks, so that power is removed.

REVOKE DELETE ON policy_documents FROM app_ingest;
