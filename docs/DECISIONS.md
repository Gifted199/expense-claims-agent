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

### D12: Features are point-in-time: only claims submitted before this one (2026-11-24, Phase 2)
- **Decision:** A claim's features use only the claim itself and claims with an earlier `submitted_at`. Claims submitted at the same instant can't see each other.
- **Alternatives:** Features computed over the whole table, for example "the employee's median" across all 12 months.
- **Why:** At scoring time the future doesn't exist. Whole-table features leak later information, such as a duplicate that hasn't been submitted yet, and inflate the evaluation. Tests check that removing or appending future claims never changes a past claim's features.

### D13: Merchant consistency is learned from claim history, not the generator's merchant list (2026-11-24, Phase 2)
- **Decision:** `merchant_seen_before` and `merchant_category_share` come only from past claims.
- **Alternatives:** Look up each merchant's "true" category in the synthetic generator's map.
- **Why:** That map only exists because the data is synthetic, so using it would be cheating. Real systems learn from history or card merchant-category codes. The cost is a cold start: new merchants look unusual until they build history.

### D14: Time-based split, fixed hyperparameters, winner by PR-AUC (2026-11-24, Phase 2)
- **Decision:** Train on claims submitted Nov 2025 to Jul 2026, test on Aug–Oct 2026.
  - Hyperparameters are set before looking at results: Isolation Forest with 300 trees and seed 42; LOF with 35 neighbours and `novelty=True` after RobustScaler.
  - The winner has the highest average precision. The report also gives a 95% bootstrap interval, precision@k and recall@k at a 5% review budget, and recall per anomaly type.
- **Alternatives:** A random split; tuning on the test months; picking by ROC-AUC.
- **Why:**
  - A random split lets the model learn from the future.
  - Tuning on the test set makes its score optimistic.
  - With about 4% anomalies, ROC-AUC looks good even for weak models. PR-AUC and precision@k reflect what a reviewer actually experiences.

### D15: ML libraries in their own uv group (2026-11-24, Phase 2)
- **Decision:** `scikit-learn` (and with it numpy, scipy and joblib) lives in the `ml` dependency group. It's a default group locally and in CI. The trainer image installs `--group ml` only.
- **Alternatives:** Main dependencies.
- **Why:** The agent and UI images (Phase 6) don't need about 400 MB of numerical libraries.

### D16: Dedicated read-only `app_ml` role with its own secret (2026-11-24, Phase 2)
- **Decision:** Migration 006 creates `app_ml`, which has SELECT on claims, employees, policy_limits, expense_categories and **claim_labels**, read-only transactions by default, and nothing else. Its password lives in a separate secret, `akinsds1126-expense-db-ml`.
- **Alternatives:** Reuse `app_readonly` (which can't see labels) or master.
- **Why:**
  - Evaluation needs the labels. Only `records.load_labels` reads them, and a test checks that the feature and model code never mention labels, status or approvals.
  - A separate secret means a task allowed only the ML secret can't read the app passwords. The trainer also gets the app secret, but only because of the optional `--score-claims` write. **Superseded by D21:** the trainer no longer has the app secret. Narrowed further by D22.

### D17: Scores are calibrated to 0–1 by percentile against the training data (2026-11-24, Phase 2)
- **Decision:** The shipped model stores the sorted raw scores of the claims it was fitted on. A score is the share of those claims that are at most as suspicious as the new one, ties included (see the D17 amendment below).
- **Alternatives:** Raw model scores (which differ in scale between models); min-max scaling.
- **Why:** `claims.anomaly_score` is constrained to 0–1, and "more unusual than 97% of claims" is easy to explain. The reference ships inside `model.joblib`, so Phase 4 serving gives identical numbers.

### D18: The one-off task runs in default-VPC public subnets with a public IP (2026-11-24, Phase 2)
- **Decision:** Fargate task, `assignPublicIp=ENABLED`, a security group with no inbound and outbound 443/5432 only, and RDS reached through an SG-to-SG rule.
- **Alternatives:** Private subnets with a NAT gateway (about $35 a month); VPC endpoints for ECR, S3, Secrets Manager and Logs (about $7 a month each).
- **Why:** It's cheap and fast for a two-day build, and the task accepts no inbound traffic. **In production:** private subnets with VPC endpoints.

### D19: latest.json is a pointer written last (2026-11-24, Phase 2)
- **Decision:** Every artefact is written under `models/anomaly/<UTC timestamp>-<git sha>/` first, and `models/anomaly/latest.json` is updated last.
- **Alternatives:** Overwrite a fixed `models/anomaly/model.joblib`.
- **Why:** Readers never see a half-written version, every version stays auditable (bucket versioning is on too), and rolling back means pointing at an older folder.

### D20: Push images to ECR with Docker's ecr-login credential helper (2026-11-24, Phase 2)
- **Decision:** Set `AWS_PROFILE=aero` and run `docker push` directly. Docker is configured with `credHelpers` for `666127452756.dkr.ecr.eu-west-2.amazonaws.com` = `ecr-login`.
- **Alternatives:** `aws ecr get-login-password | docker login …`
- **Why:** That pipe fails on this laptop ("The stub received bad data"). The credential helper fetches short-lived tokens itself, and no password passes through the shell.

### D21: The trainer doesn't write scores; `--score-claims` is an admin-only backfill (2026-11-24, Phase 2)
- **Decision:**
  - The trainer's task role can read **only** `akinsds1126-expense-db-ml` and write under `s3://akinsds1126-expense-artifacts/models/anomaly/*`.
  - Task definition revision 2 has no command: it trains, evaluates and publishes, and never writes to the database.
  - The one-off backfill from run 1 (`20261124T170142Z-431e8b4`, revision 1 with `--score-claims`) already scored all existing claims. Phase 4 scores new claims at submit time.
  - The `--score-claims` code path remains as an **admin-only, one-off backfill**. If it's ever needed again, it **must** run with a dedicated narrow role, for example `app_scorer` with only `UPDATE (anomaly_score) ON claims` and its own secret, **never** with the `-db-app` secret.
- **Alternatives:** A separate `app_scorer` role now; keeping `-db-app` access (the original plan).
- **Why:** The Phase 2 review found that the `-db-app` secret holds the `app_writer` password, and `app_writer` can change `claims.status`. A compromised trainer could therefore push claims through approval. The trainer only needs to read data and publish files. This supersedes the `-db-app` part of D16.

### D22: app_ml reads only `employee_id` and `grade` from employees (2026-11-24, Phase 2)
- **Decision:** Migration 007 replaces table-wide SELECT on `employees` with `SELECT (employee_id, grade)`.
- **Alternatives:** Full table SELECT (migration 006).
- **Why:** The model needs only the grade, to look up limits. Names and emails are personal data, synthetic or not, and the trainer has no reason to see them. `verify.py` checks both the allowed and the denied columns.

### D17 amendment: score wording and the floor (2026-11-24, Phase 2)
- A score is the share of reference claims that are **at most as suspicious** as this one, **ties included** (`bisect_right`). It doesn't mean "more suspicious than".
- About 27% of ordinary claims tie at the lowest raw score, so the lowest stored score is about 0.27, not 0.
- **Phase 4 thresholds must sit near the top**, for example 0.95 or more for "high".
- The existing scores and the shipped model use this definition, so it stays unchanged.

### D23: gitleaks false positives are ignored by exact fingerprint only (2026-11-24, Phase 2)
- **Decision:** A confirmed false positive goes into `.gitleaksignore` as one exact fingerprint (`commit:file:rule:line`) with a comment explaining why. No path, rule or wildcard ignores.
  - The first entry is the D16 line in commit `431e8b4`, where the `generic-api-key` rule matched the sentence giving the *name* of the ML database's Secrets Manager entry. A name, not a credential.
- **Alternatives:**
  - Rewriting history to remove the line. That needs a force-push, which the project's deny rules forbid, and it buys nothing because the value isn't a secret.
  - Disabling the rule, or ignoring `docs/`, which would hide real leaks.
- **Why:** A fingerprint silences only that one finding in that one commit, so the full-history scan still catches any real secret anywhere. Every ignore is visible and reviewable in git.

### D24: The handbook is generated from the database; two versions kept, only the latest searched (2026-11-25, Phase 3)
- **Decision:** `rag/handbook.py` renders the Markdown handbook from `policy_limits`, `expense_categories` (receipt thresholds) and the new `policy_city_hotel_caps`.
  - **No money figure is typed into the prose.** A test checks that every £ amount comes from those tables.
  - The mileage rate is referred to by name only ("the approved mileage allowance rate set by Finance"), because no table holds it.
  - Two versions are kept: 2025.1 (previous: 60-day deadline, softer split wording, no city table) and 2026.1 (latest).
  - Both are uploaded to `s3://akinsds1126-expense-artifacts/policy/<version>/`. Only the version flagged `is_latest` is ever searched.
  - The city caps are reference data seeded in migration 008, not by `seed --reset`, which would wipe the Phase 2 anomaly scores.
- **Alternatives:** A hand-written handbook; city guidance in prose only.
- **Why:** If the handbook and the workflow read different numbers, the assistant could cite a limit that the system doesn't enforce. Generating both from one table makes that impossible. Keeping the old version supports audit ("what did the policy say in March?"), and the `is_latest` filter keeps it out of answers.

### D25: Structure-aware chunking: one subsection per chunk (2026-11-25, Phase 3)
- **Decision:** One chunk per `### EXP-n.m` subsection, plus one per section intro.
  - A chunk never crosses a subsection.
  - Its text starts with the heading path, for example "EXP-6 Travel and hotels > EXP-6.3 Hotels by city".
  - Sections over about 1,800 characters split at paragraph or table-row boundaries, with the table header repeated in each part.
- **Alternatives:** Fixed-size windows with overlap; whole sections.
- **Why:**
  - Every chunk maps to exactly one citation.
  - Tables stay readable.
  - The heading path gives the embedding context ("hotels", "limits") that the body alone might lack.
  - The handbook's subsections are short (45 chunks, all under 600 characters), so there's no need for windows.

### D26: Retrieval methods and a selection rule fixed before the run (2026-11-25, Phase 3)
- **Decision:** Three methods, all filtered to `is_latest` and one embedding model:
  - **vector:** cosine distance via HNSW, with `hnsw.iterative_scan = strict_order` so filtered queries still return k rows.
  - **keyword:** the question's words joined with OR into `to_tsquery('english')`, ranked by `ts_rank_cd`. An exact `EXP-n(.m)` in the question is looked up directly and ranked first.
  - **hybrid:** Reciprocal Rank Fusion (score = Σ 1/(60 + rank)) of the top 20 from vector and from keyword.
  - **Selection rule, written down before the run:** highest Recall@5 (within one question is a tie), then MRR (within 0.01 is a tie), then cheaper (keyword < small < large), then simpler (keyword < vector < hybrid).
- **Alternatives:** `websearch_to_tsquery`, which ANDs every word, so natural questions rarely match; a learned re-ranker; picking the method after looking at the results.
- **Why:**
  - RRF needs no score calibration between the two very different rankers.
  - The exact-ID lookup is reliable where Postgres's parser splits "EXP-4.2" unpredictably.
  - Fixing the rule first stops us choosing whatever looks best afterwards.

### D27: Embedding models compared side by side; production keeps the winner (2026-11-25, Phase 3)
- **Decision:**
  - `policy_chunks` has one row per (version, section, chunk, `embedding_model`), so text-embedding-3-small and -large are stored side by side for the comparison.
  - Every embedding request passes `dimensions=1536`, so -large (natively 3072) fits `vector(1536)` and the single HNSW index.
  - **Questions are always embedded with the same model as the chunks they are compared against.**
  - After the evaluation, a second run with `--prune-other-models` leaves only the winner's rows, and `AZURE_OPENAI_EMBED_DEPLOYMENT` is set to the winner.
- **Alternatives:** Separate eval tables; a `vector(3072)` column for -large (HNSW in pgvector supports at most 2,000 dimensions).
- **Why:**
  - One schema serves both the evaluation and production.
  - Mixing models between questions and chunks gives meaningless similarities.
  - The shortened -large vectors are an officially supported trade-off.

### D28: A dedicated ingest role, secret, task role and security group (2026-11-25, Phase 3)
- **Decision:**
  - The `app_ingest` role can only read the three tables the handbook is built from, and write `policy_documents` and `policy_chunks`. Its DELETE is the only DELETE grant in the schema, and it exists because re-ingest replaces chunks.
  - Its password lives in its own secret, `akinsds1126-expense-db-ingest`.
  - The ECS task role can read only that secret and the Azure OpenAI secret, and can write only `policy/*` and `eval/rag/*` in S3.
  - The task has its own outbound-only security group.
  - `app_readonly` gets SELECT on the RAG tables for `search_policy`.
- **Alternatives:** Reuse `app_writer`, or the trainer's role and security group.
- **Why:** Each job can touch only what it needs. A compromised ingest task can't read claims or employees or change any approval.

### D29: Azure OpenAI endpoint normalised; the resource must accept public calls (2026-11-25, Phase 3)
- **Decision:** `normalise_endpoint` keeps only `https://<host>/`, even if a full Target URI was stored, and rejects anything that isn't https. The API key comes only from Secrets Manager and is masked in `repr`. The SDK retries 429 and 5xx errors with backoff (`max_retries=5`). The Fargate task reaches Azure over its public IP.
- **Alternatives:** Trust the stored value as-is; private networking to Azure.
- **Why:** A full Target URI is an easy copy-paste mistake that produces confusing 404s. Private connectivity between AWS and Azure is out of scope for a two-day build, but the key never leaves Secrets Manager or appears in logs.

### D30: detect-secrets runs via `python -m` (2026-11-25, Phase 3)
- **Decision:** The pre-commit detect-secrets hook is a `repo: local` hook running `uv run python -m detect_secrets.pre_commit_hook --baseline .secrets.baseline`, with `detect-secrets==1.5.0` pinned in the dev group. This is the same version the remote hook used.
- **Alternatives:** Keep the remote hook and skip it locally; replace the tool.
- **Why:** Windows Application Control started blocking the remote hook's `.exe` launcher, the same problem the Phase 0 file checks had. It's the same scanner, the same version and the same baseline, launched through Python. CI on Linux runs it too, together with the full-history gitleaks scan.
