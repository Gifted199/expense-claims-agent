# Decision log

Short record of design decisions: **what** we chose, **what else** we considered, and **why**.
Add an entry whenever a design decision is made.

---

### D1: Publicly accessible RDS, locked to one IP (2026-11-24, Phase 1)
- **Decision:** The RDS instance `akinsds1126-expense-db` is publicly accessible, but its security group allows only the developer's IP on port 5432. The ECS security group will be added in Phase 6.
- **Alternatives:** A private DB inside the VPC, reached through a bastion host, SSM port forwarding or a VPN.
- **Why:** The laptop is only a client during a two-day build, and a bastion or VPN costs time we don't have. The exposure is limited by the IP allow-list, `verify-full` TLS (D6) and `rds.force_ssl=1`. **In production the DB would be private inside the VPC**, reachable only from the ECS tasks.

### D2: audit_log is append-only (2026-11-24, Phase 1)
- **Decision:** `app_writer` has `INSERT` on `audit_log` only: no `UPDATE`, no `DELETE`. `app_readonly` can't read it at all.
- **Alternatives:** `INSERT` and `UPDATE`; a trigger-enforced immutable table.
- **Why:** An audit trail the application can rewrite isn't evidence. Grants are the simplest strong control. A trigger can be added later if the master role ever needs guarding too.

### D3: psycopg 3 is the only Postgres driver (2026-11-24, Phase 1)
- **Decision:** `psycopg[binary]` 3.x.
- **Alternatives:** psycopg2, asyncpg, pg8000.
- **Why:** LangGraph's `PostgresSaver` (Phase 5 memory) needs psycopg 3 anyway, so one driver means one connection pattern. It also gives pipelined `executemany` and safe SQL composition through `psycopg.sql` (`Identifier` and `Literal`). The binary wheel loads fine under Windows Application Control.

### D4: Generator uses only the standard library (2026-11-24, Phase 1)
- **Decision:** The synthetic data comes from a seeded `random.Random`, with no numpy or Faker.
- **Alternatives:** numpy distributions, Faker for names.
- **Why:** The output is exactly reproducible from the seed, with a fixed window and no `now()`. There are no extra dependencies and nothing new for Windows Application Control to block. The tests check that the same seed gives identical data.

### D5: Anomaly labels live in a separate eval-only table (2026-11-24, Phase 1)
- **Decision:** Planted-anomaly labels go in `claim_labels`, not in `claims`. No app role has any privilege on it.
- **Alternatives:** An `is_anomaly` or `anomaly_type` column on `claims`.
- **Why:** The ML features, the agent and the query tools can't "see the answers", so the Phase 2 and Phase 8 evaluations stay honest. `verify.py` checks that both app roles are denied.

### D6: RDS CA bundle committed for verify-full TLS (2026-11-24, Phase 1)
- **Decision:** `certs/rds-global-bundle.pem` (AWS's public RDS CA bundle) is in git, and every connection uses `sslmode=verify-full`.
- **Alternatives:** `sslmode=require`; downloading the bundle at runtime.
- **Why:** `require` encrypts the connection but doesn't check that the server is really our RDS instance, so it's open to a man-in-the-middle. The bundle is a public certificate, not a secret. Committing it makes connections reproducible offline and inside containers.

### D7: PostgreSQL 18.3, not 17 (2026-11-24, Phase 1)
- **Decision:** Keep the instance as created, on engine 18.3. The brief assumed 17.
- **Alternatives:** Recreate on 17.x.
- **Why:** Nothing we use depends on 17. pgvector 0.8.1, `CREATE OR REPLACE TRIGGER` and identity columns are all available on 18, so recreating would cost time for no gain.

### D8: App-role passwords live only in Secrets Manager (2026-11-24, Phase 1)
- **Decision:** Migrations create `app_readonly` and `app_writer` with no password. `expense_agent.db.app_roles` generates random passwords, stores them in `akinsds1126-expense-db-app` **first**, then applies them with `ALTER ROLE` (quoted safely with `psycopg.sql.Literal`). A re-run reuses the stored passwords.
- **Alternatives:** Passwords in SQL or env files; IAM database authentication.
- **Why:** No secret ever touches git or disk. IAM DB auth is worth considering for production, but it adds token-refresh plumbing to every client. `app_readonly` also defaults to read-only transactions with a 15-second statement timeout, as defence in depth.

### D9: Anomaly scores stay NULL until Phase 2; seeded statuses follow the rules (2026-11-24, Phase 1)
- **Decision:** Seeded claims have `anomaly_score = NULL`. Statuses come from the deterministic rules:
  - an over-limit claim goes to a manager and is never auto-approved
  - a within-limit claim is auto-approved
  - recent claims are still pending
- **Alternatives:** Precomputed fake scores; random statuses.
- **Why:** Scores must come from the trained model, not be invented. Rule-based statuses mean many planted anomalies were "approved", which is exactly the gap the ML model has to close.

### D10: app_writer can't rewrite history: approvals are INSERT-only, claims have column-level UPDATE (2026-11-24, Phase 1)
- **Decision:** Migration `005_tighten_app_writer.sql` revokes table-wide UPDATE on `claims` and `approvals`. `app_writer` may only `UPDATE (status, anomaly_score, receipt_s3_key)` on claims, and can only INSERT into approvals.
- **Alternatives:** Table-wide `INSERT, UPDATE` (the original plan); triggers that block changes to particular columns.
- **Why:** The Phase 1 review found that table-wide UPDATE would let the app role change a claim's employee, amount or date after a decision, or flip a recorded approval. A decision record that can be edited isn't evidence, which is the same reasoning as D2. Column grants are enforced by Postgres itself, and `verify.py` checks them.

### D11: Phase 2 must not use status or approvals as model features (2026-11-24, Phase 1)
- **Decision:** The anomaly model's features come only from the claim's own facts and the employee's history: amount, category, merchant, times, duplicates and personal norms. **`status`, `approvals` and `claim_labels` are excluded.**
- **Alternatives:** Using manager rejections as a feature.
- **Why:** In the synthetic data, managers reject planted anomalies more often (35% against 12%), so status and approvals partly encode the label. Using them would leak the answers and inflate the evaluation scores. In production, the score is also needed *before* any decision exists.
