-- 010: hotel city and number of nights on claims (decision D36).
-- submit_claim checks a hotel's nightly rate (amount / nights) against the city cap in
-- policy_city_hotel_caps, so both values are stored on the claim for the manager to see.
-- Both are nullable: historic seeded claims have neither, and non-hotel claims never do.
-- "Hotel claims must have both" is enforced in the workflow code, not by a table CHECK:
-- a CHECK (even NOT VALID) would also fire when a manager updates the status of an old
-- hotel claim that has no city.
-- No new grants: app_writer's INSERT and both roles' SELECT on claims are table-wide.

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS city text CHECK (city IS NULL OR length(city) BETWEEN 1 AND 100),
    ADD COLUMN IF NOT EXISTS nights smallint CHECK (nights IS NULL OR nights BETWEEN 1 AND 60);
