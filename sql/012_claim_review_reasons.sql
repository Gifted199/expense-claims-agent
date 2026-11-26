-- 012: why a claim was sent to a manager (decision D44).
-- submit_claim writes the failed-check reasons at INSERT; the manager queue reads them.
-- Deliberately NO UPDATE grant: app_writer's column-level UPDATE on claims stays limited to
-- (status, anomaly_score, receipt_s3_key) from 005, so the reasons can't be rewritten later.
-- verify.py checks this, and proves it live with an UPDATE that must be denied.

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS review_reasons text[]
        CHECK (review_reasons IS NULL OR cardinality(review_reasons) <= 20);
