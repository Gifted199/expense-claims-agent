-- 003: EVAL-ONLY anomaly labels.
-- Kept out of `claims` so the ML model, the agent and the app roles can never see the answers.
-- No app role is granted any privilege on this table (see 004).

CREATE TABLE IF NOT EXISTS claim_labels (
    claim_id          bigint PRIMARY KEY REFERENCES claims (claim_id) ON DELETE CASCADE,
    anomaly_type      text NOT NULL CHECK (anomaly_type IN (
                          'duplicate_exact', 'duplicate_near', 'amount_outlier',
                          'odd_time', 'category_mismatch', 'split_claim')),
    related_claim_id  bigint REFERENCES claims (claim_id),
    notes             text NOT NULL DEFAULT '',
    CONSTRAINT claim_labels_not_self_related
        CHECK (related_claim_id IS NULL OR related_claim_id <> claim_id)
);

CREATE INDEX IF NOT EXISTS claim_labels_type_idx ON claim_labels (anomaly_type);

COMMENT ON TABLE claim_labels IS
    'EVAL ONLY: planted anomaly labels for synthetic data. Never read by the app, model features or agent.';
