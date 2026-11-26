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
- **Decision:** Migration 006 creates `app_ml`, which has SELECT on claims, employees, policy_limits, expense_categories and **claim_labels**, read-only transactions by default, and nothing else. Its password is stored in the Secrets Manager entry named `akinsds1126-expense-db-ml`.
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

### D23: gitleaks: secret names allowlisted by pattern, values never; fingerprints for history only (2026-11-24, Phase 2; revised 2026-11-25, Phase 3)
- **Decision:**
  - `.gitleaks.toml` extends the default rules (`useDefault = true`) with one allowlist: a matched *value* that is exactly a project Secrets Manager entry **name** (`^akinsds1126-expense-[a-z0-9-]+$`, `regexTarget = "secret"`). Names are not credentials.
  - Secret **values** are never allowlisted. Nothing is allowlisted by path, rule or commit.
  - `.gitleaksignore` holds exact fingerprints (`commit:file:rule:line`) **only for historic findings** already in git history, each with a comment.
  - CI proves the config still bites: `scripts/gitleaks_selftest.py` writes a fixed fake AWS access key ID (assembled from parts at run time, so the literal is never in the repo) and requires gitleaks to flag it, 20 times in a row. A random key was tried first and was flaky: gitleaks' generic rule skips values containing a stopword, which a random string sometimes does, and requires a line naming `akinsds1126-expense-azure-openai` to pass. `tests/test_gitleaks_config.py` checks the allowlist shape and regex.
- **Alternatives:**
  - Fingerprints only (the first version of this decision). Fragile: a squash merge creates a new commit, so every merge needed new fingerprints, and every new file naming a secret failed CI again.
  - Rewriting history (needs a force-push, which is forbidden) or disabling the rule or ignoring `docs/` (hides real leaks).
- **Why:** It fixes the cause (names look like keys to the generic rule) without weakening detection of real values, and the self-test fails CI if the allowlist ever becomes too broad.
- **Known limit:** a value that is all lowercase and starts with `akinsds1126-expense-` would also pass. Real Azure keys and generated passwords don't have that shape, and detect-secrets still scans locally.

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
  - Its password is stored in its own Secrets Manager entry, named `akinsds1126-expense-db-ingest`.
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

### D31: Exact section-ID lookup added to the winning vector search, after Run A (2026-11-25, Phase 3)
- **Decision:** Production retrieval is the pre-declared winner, **vector search with text-embedding-3-large** (Run A, D26). On top of it sits a deterministic step, method `vector_id`:
  - If the question contains an `EXP-n` or `EXP-n.m` ID that **exists in the latest handbook**, those sections are returned first.
  - The remaining places are filled from vector search, without duplicates.
  - With no ID in the question, the result is exactly vector search.
- **This was added after seeing Run A's results**, and is marked here as a post-hoc change. Run A's numbers stay in `docs/RAG.md` unchanged, as the pre-declared outcome.
- **Validation:** a new held-out set of 10 questions, `eval_questions_holdout.json`, committed in `dac8f3e` **before** the shortcut was written or run:
  - 5 cite IDs in varied phrasings
  - 5 are plain paraphrases, to show nothing else changes
  - Before and after results are reported on both the original 30 and the new 10.
- **Alternatives:** Switch to hybrid (better on IDs, but worse MRR and it misses the deadline question); leave the gap.
- **Why:** Users and the agent will cite sections by ID ("what does EXP-6.3 say?"). Vector embeddings don't match ID strings reliably. A lookup has no parameters to tune, so it can't overfit. It only acts when a real, current section ID is present.

### D27/D28 amendments after the Phase 3 review (2026-11-25, Phase 3)
- **Real per-step transactions.**
  - **What was wrong:** the ingest job used to open a non-autocommit connection. After its first SELECT, psycopg turned every `with conn.transaction()` into a *savepoint* inside one long transaction. That transaction stayed open across all the Azure calls, and a late failure (for example the S3 upload of the eval results) rolled back everything.
  - **What it does now:** the job connects with `autocommit=True`. Each step commits on its own: registering the documents, each (version, model) chunk replacement, syncing `is_latest`, and the prune. This matches what D27 always claimed.
- **Prune can't empty the table.** `prune()` refuses an empty model list, and `--models` must name at least one deployment. Otherwise `NOT (embedding_model = ANY('{}'))` would delete every chunk.
- **No DELETE on documents (009).** Migration 009 revokes `app_ingest`'s DELETE on `policy_documents`. Re-ingest only upserts documents, and a document delete would cascade to all its chunks. DELETE on `policy_chunks` remains, and it is the only DELETE grant in the schema.
- **The committed task definition is the production command.** `infra/ecs/ingest-task-definition.json` now runs `--models text-embedding-3-large --prune-other-models`. Re-running it keeps production on the winner, instead of restoring the -small rows. The evaluation runs are reproduced with a command override (see docs/RAG.md).
- **Known follow-up (not fixed):** for a question that cites several IDs, the exact-ID lookup orders rows by `section_id` text and limits them to k. A section-level ID such as `EXP-10` expands to all its subsections and could crowd out a second cited ID. Ordering also follows text, so `EXP-1.10` sorts before `EXP-1.2`. No current question hits this. A fix would keep the order in which IDs are cited and cap the rows per ID.

### D32: Phase 4 is lean: one review, only bug and security fixes (2026-11-25, Phase 4)
- **Decision:** Following the trainer's guidance for this week (the goal is an agent that calls tools; accuracy is not the agenda), Phase 4 gets one `/review-phase` pass. Only bugs and security issues found there are fixed. Everything else goes in the **Phase 4 future work** list below.
- **Alternatives:** Several review rounds and polishing (more time than the deadline allows); no review at all (risks shipping a security hole into the agent's tools).
- **Why:** The tools must be correct and safe, because the agent and the UI build on them. Polish can wait.

### D33: MCP server: FastMCP (mcp SDK 1.x), streamable HTTP, identity from a token-protected header (2026-11-25, Phase 4)
- **Decision:**
  - The official `mcp` Python SDK's `FastMCP`, pinned `mcp>=1.20,<2` (in 2.x FastMCP was renamed and its API changed). Streamable HTTP, `stateless_http=True`, JSON responses, port 8080 (127.0.0.1 locally; internal-only in ECS).
  - Every request needs `Authorization: Bearer <token>` (Secrets Manager entry named `akinsds1126-expense-mcp-internal`, JSON key `MCP_INTERNAL_TOKEN`, compared with `hmac.compare_digest`) and exactly one `X-Employee-Id`. Otherwise an ASGI middleware answers 401 before any MCP code runs. `/health` is open and returns no data.
  - Tools read the employee from the request headers via FastMCP's hidden `Context`, then load it as `app_readonly` and refuse unknown or inactive employees. **No tool has an identity argument** (a test checks every schema). Manager approve/reject is not a tool.
  - "Not found" and "someone else's claim" return the same message.
- **Alternatives:** Identity as a tool argument (the model could choose whose data to see); OAuth between internal services (heavy for two containers in one task); the standalone `fastmcp` package (an extra third-party dependency); mcp 2.x `MCPServer` (new API, less proven).
- **Why:** The model can't change who it acts for. Only the agent API holds the token and sets the header, and the token never reaches the model.

### D34: submit_claim is a fixed sequence; every check runs; the decision is code (2026-11-25, Phase 4)
- **Decision:**
  - Steps, always in this order: validate → per-claim limit (hotels: nightly rate vs city cap) → monthly limit (same definition as `verify.OVER_LIMIT_AUTO_APPROVED`) → duplicate (same merchant and amount within 10 days) → split (same merchant within ±1 day, combined over the per-claim limit) → deadline → anomaly score.
  - **Every** check runs, so all reasons come back together, each citing its handbook section.
  - Approved only if every check passed (with a `system` approval row); otherwise `pending_manager`. The status is set at insert, in one transaction with the claim, the approval and an `audit_log` row (check results only, no receipt text).
  - The transaction first takes `pg_advisory_xact_lock(employee_id)`, so two claims submitted at the same moment can't both slip under the monthly limit.
  - Anomaly "high" means a score of 0.95 or more. The features come from the same `build_features` as training, over claims submitted before now.
  - **Deadline (a change from the plan):** a claim older than the 30-day deadline is not refused. It goes to the manager, as the handbook says (EXP-2.3 Late claims). Only claims past the 90-day cutoff are refused as invalid. Both numbers come from `rag.handbook.LATEST`.
  - A hotel's per-claim check is its nightly rate (amount ÷ nights) against the city cap, or the UK-wide hotel limit (`policy_limits`) for cities without their own cap, as the handbook table says.
- **Alternatives:** Stop at the first failed check (hides other problems from the manager); let the LLM decide (forbidden); refuse late claims outright (contradicts EXP-2.3).
- **Why:** The same input always gives the same decision, and a human sees every reason.

### D35: Receipts: S3 key scoping, and no OCR text in logs (2026-11-25, Phase 4)
- **Decision:** `extract_receipt` and `submit_claim` accept only `samples/receipts/<file>` (shared synthetic demo receipts) or `uploads/<employee_code>/<file>` (the caller's own uploads; Phase 5 `/upload` will write there). Keys with `..`, `//`, a leading `/` or anything else are refused before Textract is called. Logs hold the key, the missing fields and the confidence, never vendor names, amounts or line items (a test captures the logs).
- **Alternatives:** Any key in the bucket (one employee could read another's receipts through the model).
- **Why:** The model chooses the key, so the server must enforce whose receipts can be read.

### D36: Hotel city and nights on claims (010); missing ones are asked for, never guessed (2026-11-25, Phase 4)
- **Decision:** Migration 010 adds nullable `claims.city` and `claims.nights` (1–60). `submit_claim` takes optional `city` and `nights`. For a hotel claim without either, it returns `{"result": "missing_fields", "missing": [...], "message": ...}` and writes nothing, so the agent asks the user. No default city is ever assumed.
  - "Hotel claims must have both" is enforced in the workflow code, not by a table CHECK. A CHECK (even `NOT VALID`) would also fire when a manager updates the status of an old seeded hotel claim that has no city.
- **Alternatives:** Keep city and nights only in the audit details (the manager couldn't see them); guess the city from the merchant (wrong caps); drop the city-cap check.
- **Why:** The city decides the cap, so a guess could approve a claim that should go to a manager.

### D37: Five synthetic receipts under samples/receipts/ (2026-11-25, Phase 4)
- **Decision:** `python -m expense_agent.ocr.make_receipts --upload` draws 5 receipts with Pillow (lunch, taxi, a 2-night London hotel over the senior cap, office supplies, and a repeat of the lunch for the duplicate demo). All vendors are fictional, each image is stamped "SYNTHETIC RECEIPT - DEMO DATA ONLY", and dates are relative to the day they're generated. They're uploaded with a `manifest.json` of the expected values, which the smoke test checks Textract against.
- **Why:** Synthetic data only (CLAUDE.md), and the demo receipts exercise the duplicate, city-cap and OCR paths.

### Phase 4 review fixes (2026-11-25, Phase 4)
The single `/review-phase 4` pass found these. Per D32, only bugs and security issues were fixed:
- **"Today" was refused before noon.** A date-only `incurred_at` means midday London time, so a claim for today's lunch submitted at 09:00 counted as "in the future". It is now capped at the submission time.
- **No future tolerance.** A 5-minute allowance let a time slightly after "now" through validation, and the database's `incurred_at <= submitted_at` CHECK then failed with an error instead of a clean `invalid` result. The tolerance is now 0.
- **Header parsing:** `X-Employee-Id` must be plain ASCII digits. A Latin-1 superscript digit used to cause a 500 instead of a 401.
- **Auth middleware:** only `lifespan` events skip the token check. Any non-HTTP scope, such as a websocket, is dropped.
- **`score_claim_anomaly` scoping:** it checks ownership before loading the model or any claim history, and it now has unit tests (someone else's claim gives "not found" and reads nothing).
- Lint and format fixes in three test files (CI would have failed).

### Phase 4 future work (not done, by D32)
- Smoke check for "someone else's claim" should also assert `found is False` (the recorded run did show "not found").
- `get_claim_status` with a number above bigint returns a database error instead of "not found"; `score_claim_anomaly` should refuse NaN or infinite amounts. Both end as tool errors and leak no data.
- Phase 6: the MCP task role must be **read-only** on `models/anomaly/*`, because the model file is a pickle that the server loads.
- Database connection pooling (each tool call opens a TLS connection now).
- Model hot-reload when `latest.json` changes (the model is cached until restart).
- Incremental feature history (scoring loads all claims each time; fine at this data size).
- Multi-currency claims with an FX check (everything is GBP now).
- Textract line items, and using the OCR'd date and total to cross-check the submitted claim.
- Per-employee rate limiting on the MCP server.
- MCP task role, image and ECS service (Phase 6), and DNS-rebinding settings for the container host name.
- The multi-ID RAG crowding follow-up from Phase 3.
- Anomaly model strictness: with `robust_zscore`, a merchant nobody has claimed at before scores about 0.99, so every new-merchant claim goes to a manager (seen in the smoke test, docs/MCP.md). Safe but strict; a fix belongs to the model (for example cap the new-merchant feature's z-score or calibrate per feature), not to the tools.

### D38: Phase 5 scope: agent + API + UI together, lean (2026-11-26, Phase 5)
- **Decision:** Phase 7 (the Streamlit UI) is merged into Phase 5. Lean rules from D32 apply (one review; only bug and security fixes). Langfuse tracing and the LLM comparison move to Future work (the LLM comparison to Phase 8 evaluation). Instead, every turn logs one JSON line: employee ID, a hash of the thread, tools called, guardrail flags and latency, with **no message text** and no token.
- **Why:** The goal this week is a working agent that calls tools, end to end, with safety in code.

### D39: One agent, built per request, with MCP tools over streamable HTTP (2026-11-26, Phase 5)
- **Decision:** For each `/chat` request the API loads the 7 MCP tools with `langchain-mcp-adapters`. The connection carries the internal token and the **session's** `X-Employee-Id`. The API then builds `create_agent` (LangChain 1.x) with `AzureChatOpenAI` (`gpt-5.4-mini` from the Azure secret), the system prompt, the guardrail middleware and the Postgres checkpointer, and runs one turn.
- **Alternatives:** One long-lived agent with an identity argument on the tools (the model could choose the identity); one MCP session shared by all users (headers can't differ per user).
- **Why:** The identity is fixed per request, outside the model. Nothing conversational lives in the process, so memory is only what RDS holds, and it survives restarts.

### D40: Chat memory: AsyncPostgresSaver in its own schema, narrow role, own secret (2026-11-26, Phase 5)
- **Decision:**
  - Migration 011 creates schema `agent_memory` and role `app_memory` (`USAGE, CREATE` on that schema only, `search_path = agent_memory`, 15 s statement timeout). The role has no grants on any `public` table, and `verify` checks this.
  - Its password is stored in the Secrets Manager entry named `akinsds1126-expense-db-memory`, set by `app_roles`.
  - The saver's own `setup()` creates its tables there.
  - **The checkpoint key is `e<employee_id>:<uuid>`.** The client's `thread_id` must be a UUID. One employee can't open another's conversation, even with the same `thread_id` (tested).
- **Alternatives:** The saver's tables in `public` with `app_writer` (a memory bug could then touch claims); an in-memory saver (lost on restart, fails R6).

### D41: Guardrails are deterministic middleware; output checks get one retry (2026-11-26, Phase 5)
- **Decision:**
  - **Input** (`before_agent`, **the LLM is not called** when it refuses): empty or over 2,000 characters; prompt-injection patterns (including identity claims such as "I am employee E0003" or "as manager approve"); investment-only messages.
  - **Multi-intent:** messages are split into clauses. If only some clauses ask for investment advice, the turn continues with a note telling the model to decline only that part in one sentence and answer the rest.
  - **Output** (`after_model`, final answers only):
    - sentences that give investment advice are removed and replaced by one decline line; policy text that merely mentions shares or crypto stays
    - every £ figure must appear in a tool result from this turn, or in the user's own message (tool JSON is decoded first, because "£" is escaped there)
    - if the policy search covered the question, the answer must cite one of the returned section IDs
    - on a problem, the bad answer is removed and the model gets **one** retry with feedback; if it fails again, a safe fallback message replaces it
  - Over-limit and anomalous claims go to a human because of `submit_claim`'s code, not the prompt.
- **Alternatives:** An LLM judge (slower and not deterministic; it's in Future work as a second layer); prompt-only rules (not enforceable).
- **Why:** These rules are checkable by code, so they are enforced by code and tested with a scripted model.

### D42: Agent API identity in the demo; manager actions are HTTP, never tools (2026-11-26, Phase 5)
- **Decision:**
  - The UI's demo user switcher sends `X-Demo-Employee-Id`. The API accepts it only with demo auth switched on (`--demo-auth` / `EXPENSE_DEMO_AUTH=true`, default **off**), and only for an active employee. **Production would use Amazon Cognito** (a JWT verified by the API).
  - `GET /manager/queue` shows only the caller's **direct reports'** `pending_manager` claims.
  - `POST /manager/claims/{id}/decision` runs as `app_writer` in one transaction: lock the claim only if it is pending **and** the caller is the claimant's manager; update the status; append a manager approval row; append an audit row. Anything else is 404, including your own claim. The database's self-approval trigger is the backstop.
  - **Phase 6 (your decision):** because the switcher lets anyone pick a user, the UI must not be publicly open. Its security group will allow port 8501 **only from your IP**. The agent API (8000) and MCP server (8080) stay internal-only.
- **Why:** The demo needs user switching, but the trust boundary must be explicit and switched off by default.

### D43: Uploads: checked by content, stored under the caller's prefix, OCR preview without the LLM (2026-11-26, Phase 5)
- **Decision:** `/upload` accepts PNG, JPEG or PDF by **magic bytes** (the filename and declared type are ignored), at most 5 MB (Textract's synchronous limit). The server picks the key `uploads/<employee_code>/<random>.<ext>`. It then calls the MCP `extract_receipt` directly (no LLM) for the preview. "Create a claim from this receipt" sends a prefilled chat message, so the claim still goes through `submit_claim`'s checks.

### D44: claims.review_reasons is written once, at INSERT (2026-11-26, Phase 5)
- **Decision:** Migration 012 adds `claims.review_reasons text[]`. `submit_claim` fills it with the failed-check reasons when it inserts the claim, and the manager queue shows them.
  - `app_writer` **cannot UPDATE it**. Its column-level UPDATE stays `(status, anomaly_score, receipt_s3_key)` from 005, and 012 adds no grant.
  - This is checked in three ways: `verify` has the column-privilege row; `verify` runs a live `UPDATE claims SET review_reasons = …` as `app_writer` inside a rolled-back transaction, which must fail with `InsufficientPrivilege` (it does); and unit tests prove no migration grants UPDATE on it and that `submit_claim` never UPDATEs.
- **Why:** The manager must see why the system held a claim, and that record can't be rewritten afterwards.

### D45: The UI holds no secrets and talks only to the agent API (2026-11-26, Phase 5)
- **Decision:** Streamlit (theme in `.streamlit/config.toml`). It has a welcome screen with capability cards and clickable examples, chat with memory (one UUID thread per conversation), receipt upload with an OCR preview and "create a claim", My claims, and a Manager queue tab for managers only. It knows only `EXPENSE_API_URL`.

### D46: Demo receipts use merchants that already exist in the data (2026-11-26, Phase 5)
- **Decision:** The receipts are now from Green Leaf Kitchen (lunch), CityCab (taxi), Riverside Lodge (London hotel, still over the senior cap) and OfficeHub. That way the approved path in the demo is realistic: the Phase 2 model scores unseen merchants as unusual (see Phase 4 future work).

### Phase 5 notes and future work (not done, by D32/D38)
- **Local ports on this laptop:** Docker Desktop already holds 8000 and 8501, so local runs use **8001** (API) and **8502** (UI). Containers keep 8000 and 8501 (Phase 6).
- **The smoke test isn't repeatable on the same day:** re-submitting the same taxi receipt is, correctly, a duplicate and goes to the manager.
- Future work:
  - Langfuse tracing
  - the LLM comparison (Phase 8)
  - Cognito sign-in
  - streaming replies
  - an LLM-based investment and off-topic classifier as a second layer
  - PII redaction beyond "no message text in logs"
  - a database connection pool for the API's reads
  - the new-merchant strictness of the anomaly model

### Phase 5 review fixes (2026-11-26, Phase 5)
The single `/review-phase 5` pass found these. Per D32/D38 only bugs and security issues were fixed, and each has a test:
- **Ordinary questions were refused as attacks.** "Why did they reject my claim?", "Which train ticket should I buy?", "Show me the instructions for submitting a hotel claim" and similar were blocked. Now:
  - only *commands* to approve or reject are treated as injection ("approve my claim", "you should approve"), not questions about a decision
  - "buy" or "sell" count as investment only next to a financial product
  - "savings" is no longer a product
  - "instructions" must be *your/system* instructions
  - 12 allow-case tests pin this down. Checked live: "Why did they reject my claim CLM-2026-002940?" is answered from `get_claim_status`.
- **A refused message stayed in memory.** The next turn ("please answer my previous question") sent the refused text to the model. The refusal now replaces the stored message (same ID) with "[message withheld by a guardrail]". There's a two-turn test, and it was checked live: the model replied that it can't see the withheld question.
- **Receipt prefill** no longer writes `£None` when OCR misses a field.
- **The S3 upload** runs in a worker thread, so it no longer blocks other API requests.
- **The manager queue** shows employee-typed text (description, merchant, name) as plain text. A markdown image link in a description could otherwise make the manager's browser load an external URL.
- Not fixed (nice-to-have, future work):
  - stricter citation matching (any section of the same chapter passes today)
  - guardrail feedback notes stay in the stored history
  - no history trimming, and two simultaneous requests on one thread can overwrite each other's checkpoint
  - a test of the identity-header builders
  - an empty demo directory crashes the UI
  - Starlette buffers the whole upload before the 5 MB check (the API is internal-only)

### D47: "Latest claim" means most recently submitted; no invented date filters; friendly dates (2026-11-26, Phase 5)
- **Bug (manual testing):** asked "What's the status of my latest claim?", the agent called `get_my_claims` with `date_from=2026-11-01, date_to=2026-11-26`, a range it chose itself. (This was confirmed from the stored conversation, read-only as `app_memory`; the turn log records tool names, not arguments.) It got no rows, because the synthetic data ends in Oct 2026, and said the user had no claims.
- **Decision:**
  - `get_my_claims` now sorts by **`submitted_at` DESC** and says so in its result ("claims[0] is the latest claim").
  - Its description and the system prompt say: no status or date filters unless the user asked for that status or period; "latest" means most recently submitted, and the answer says so; dates are shown as "26 Oct 2026, 14:26" (London time). The tool adds `*_display` fields for that.
  - A filtered call that finds nothing carries a note, "this does not mean no claims".
  - **Deterministic safety net:** `EmptyClaimsRetry` middleware re-runs a filtered `get_my_claims` that returned nothing **once, without filters**, and gives the model both facts. This doesn't depend on the model following the prompt (flag `claims_retry_unfiltered`).
  - The UI's My claims and Manager queue show friendly dates. My claims now has a "Submitted" column, newest first.
  - The exact conversation is a scripted-model test (`tests/agent/test_latest_claim.py`) and the first case of the Phase 8 set (`src/expense_agent/evaluation/conversations.json`).
- **Why:** An empty result from a self-chosen filter must never become "you have no claims".

### D48: One ECS task, three containers, started in order of health (2026-11-26, Phase 6)
- **Decision:**
  - Service `akinsds1126-expense-app-svc` runs one Fargate task (1 vCPU / 3 GB) with three containers: `mcp`, `agent-api` and `ui`.
  - `agent-api` waits for `mcp` to be HEALTHY, and `ui` waits for `agent-api`.
  - **The MCP server (8080) and the agent API (8000) bind to 127.0.0.1**, so only containers in the same task can reach them, even if a security group were misconfigured.
  - Only the UI's 8501 is mapped and allowed by the security group.
  - Every container is essential.
  - Logs go to `/ecs/akinsds1126-expense-app` with the prefixes `mcp/`, `agent/` and `ui/`.
- **Why:** These are the simplest internal-only ports: in awsvpc mode the containers share localhost, with no service discovery or load balancer needed.

### D49: One task role shared by mcp and agent-api (2026-11-26, Phase 6)
- **Decision:** One role, `akinsds1126-expense-app-task-role`, holds the union of what the two backend containers need, scoped per resource ARN:
  - the `db-app`, `db-memory`, `azure-openai` and `mcp-internal` secrets (not `ml`, not `ingest`)
  - GetObject on the demo and upload receipts and on `models/anomaly/*` (**read-only on the pickled model**)
  - PutObject on `uploads/*`
  - `textract:AnalyzeExpense`, which has no resource-level permissions
- **Trade-off:** In ECS, the task role is available to **every** container in the task, including the UI, through the task credentials endpoint. The agent container can also read what only MCP uses, and the other way round. The UI code never uses AWS, holds no secrets and only knows the API URL, and the UI image has no RDS certificate. Separate services with separate roles (plus Service Connect or an internal load balancer) would isolate them, at the cost of more moving parts. That's future work.

### D50: No load balancer: a public IP, and the UI open to your IP only (2026-11-26, Phase 6)
- **Decision:**
  - The task gets a public IP in the default-VPC public subnets (no NAT).
  - Security group `akinsds1126-expense-app-sg` allows inbound 8501 **only from your IP** (82.6.101.132/32), because the demo user switcher lets anyone pick any employee (D42).
  - Outbound is 443 (Azure OpenAI and AWS APIs) and 5432 to the RDS SG only.
  - **The public IP changes on every deploy.** The deploy workflow prints the new URL.
- **Future work:** an ALB with a stable DNS name, HTTPS, and Cognito instead of the switcher.

### D51: Push-to-main deploy with GitHub OIDC, only after CI passes (2026-11-26, Phase 6)
- **Decision:**
  - **Trigger:** `deploy.yml` runs on `workflow_run` of `CI`, only when CI succeeded for a **push** to **main**.
  - **Access:** it assumes `akinsds1126-expense-github-deploy` through the existing OIDC provider. The trust is pinned exactly to `repo:Gifted199/expense-claims-agent:ref:refs/heads/main`. **No AWS keys are stored in GitHub.**
  - **The role can only:**
    - push to the three app ECR repos
    - register task definitions
    - update and describe this one service
    - pass exactly the execution role and the app task role, and only to ECS tasks
    - read the new task's public IP, to print the URL
  - **What it does:** builds the three images tagged with the commit SHA (an existing tag is skipped, since tags are immutable and re-runs must be safe), renders the task definition, deploys, and waits for stability.
  - **On failure:** the service's deployment circuit breaker rolls back a failed deploy.
- **Why:** main must always be the tested code that's running, and the deploy needs no long-lived credentials.
- **Amendment (2026-11-26, after Deploy #1 failed):**
  - **What failed:** the first deploy after PR #7 failed at `configure-aws-credentials` with "Not authorized to perform sts:AssumeRoleWithWebIdentity".
  - **Checked read-only:** the role ARN, the `aud` condition (`sts.amazonaws.com`) and the OIDC provider's audience were all correct. The only condition left is `sub`.
  - **Cause:** a job started by a `workflow_run` event (CI completing) doesn't get the token subject that the role trusts exactly, `repo:Gifted199/expense-claims-agent:ref:refs/heads/main`.
  - **Fix:** the deploy is now a `deploy` job inside `ci.yml`. It has `needs: lint-and-test` and `if: push && ref == refs/heads/main`, so it still runs only after the tests pass, and as a push to main its subject is exactly the trusted one. `deploy.yml` was removed. A run on main is no longer cancelled by a newer push, so a deploy is never cut off midway.
  - **For future mismatches:** the job prints the token's non-secret claims (`sub`, `aud`, `ref`, `event_name`, never the token) before assuming the role.
  - **The trust policy is unchanged:** it was not widened.

### D52: Images: one per container, minimal groups, non-root, health checks, a visible version (2026-11-26, Phase 6)
- **Decision:**
  - All images are `python:3.12-slim-bookworm` with uv, `--no-dev --no-default-groups`, and only the groups each needs: mcp = server, rag, ml; agent = agent; ui = ui.
  - They run as non-root uid 10001.
  - Health checks use `python -m expense_agent.healthcheck`, which only probes `http://127.0.0.1:<port>`, since the images have no curl. The same command is used by Docker and by ECS.
  - The UI sidebar shows "Version `<short sha>`" from the `GIT_SHA` build argument, so a deploy is visible.
- **Bootstrap (once, by hand):** I built and pushed the images from the laptop with the credential helper (D20). You registered the task definition and created the service in the console. After that, every merge to main deploys on its own.
- **Proof (your choice):** the Phase 6 PR merge itself is the proof. `deploy.yml` runs after CI, and the UI shows "Version `<main sha>`". There's no extra demo PR. This is recorded in docs/DEPLOY.md.
