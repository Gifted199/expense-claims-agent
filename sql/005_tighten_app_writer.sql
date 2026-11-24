-- 005: tighten app_writer (decision D10).
-- approvals become append-only: a recorded decision can never be changed afterwards.
-- claims: app_writer may only move a claim through the workflow (status), attach the model
-- score and the receipt. Who claimed, how much, when and where can't be rewritten.

REVOKE UPDATE ON claims, approvals FROM app_writer;
GRANT UPDATE (status, anomaly_score, receipt_s3_key) ON claims TO app_writer;
