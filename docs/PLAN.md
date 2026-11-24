# PLAN — Expense Claims Assistant

Deadline: **Wed 25 Nov 2026, 17:00**. Region **eu-west-2**. Prefix **akinsds1126-expense-**.
Rule: finish and verify a phase before starting the next. Each phase: plan mode → review plan → implement with tests → review diff → commit.
Cut line if behind: UI polish, LLM-judge scoring, extra comparisons. Never cut a must-have.

## Day 1 — Tue 24 Nov

### Phase 0 — Foundation (≈1h)
- Repo `expense-claims-agent` on GitHub; uv project; folders `src/expense_agent/{db,ml,rag,mcp_server,agent,guardrails,ocr,workflow}`, `ui/`, `tests/`, `infra/`, `docs/`.
- `CLAUDE.md` (rules, commands, constraints), `docs/SPEC.md`, `docs/PLAN.md`.
- `.claude/settings.json` (allow tests/lint; ask before aws/git push; deny destructive commands), Claude Code hooks (ruff after edits), `/review-phase` command, reviewer subagent.
- `.pre-commit-config.yaml` (ruff, ruff-format, gitleaks, file checks); `.github/workflows/ci.yml` (lint + pytest).
- AWS console: create GitHub **OIDC identity provider** (tests early that IAM allows it).
- **Done when:** `pre-commit run --all-files` and `pytest` pass locally; CI green on GitHub; OIDC provider exists.

### Phase 1 — Database + synthetic data (≈1.5h)
- AWS console: RDS PostgreSQL (db.t4g.micro, single-AZ, encrypted, password in Secrets Manager, SG allows My IP + ECS SG only).
- Schema: departments, employees (with manager), policy_limits (category × grade), claims, approvals, audit_log; `pgvector` extension; read-only DB role for query tools.
- Generator: ~3,000 claims over 12 months with **planted anomalies** (duplicates, far above personal norm, weekend/odd hours, category mismatch, split claims just under limit) and a `is_planted_anomaly` label for evaluation only.
- **Done when:** tables seeded; row counts and anomaly rate printed; tests for generator rules pass.

### Phase 2 — ML anomaly model, trained in the cloud (≈1.5h)
- Features per claim (amount vs personal median, vs category limit, day/hour, duplicate signals, merchant/category consistency).
- Compare Isolation Forest vs LOF vs robust z-score baseline → pick on PR-AUC / precision@k.
- Trainer container runs as a **one-off ECS task**: reads RDS, trains, writes `model.joblib`, `metrics.json`, `model_card.md` to S3 under a version folder.
- **Done when:** comparison table in `docs/ML.md`; versioned artefacts in S3; task logs in CloudWatch.

### Phase 3 — RAG policy handbook (≈1h)
- Synthetic expense policy handbook (Markdown, sections with IDs) → S3.
- Structure-aware chunking → Azure embeddings → pgvector; hybrid search (vector + Postgres full-text, fused).
- Labelled handbook questions; compare vector vs hybrid; pick.
- **Done when:** recall table in `docs/RAG.md`; `search_policy` returns cited passages.

### Phase 4 — MCP server, tools, OCR, workflow (≈1.5h)
- FastMCP server with the tools in SPEC §4; Textract AnalyzeExpense for receipts in S3; deterministic `submit_claim` workflow.
- Server-side identity injection; parameterised SQL only.
- **Done when:** unit tests per tool (mocked AWS) pass; local MCP client lists and calls every tool against cloud RDS/S3/Textract.

### Phase 5 — Agent, memory, guardrails (≈2h)
- LangChain `create_agent` + MCP tools + Azure OpenAI; system prompt; PostgresSaver memory with `thread_id`.
- Guardrails (SPEC §5) incl. multi-intent handling; Langfuse tracing.
- FastAPI agent API (`/chat`, `/health`, `/upload`).
- Quick LLM comparison on a small tool-choice set → pick deployment.
- **Done when:** multi-turn + multi-intent + guardrail smoke tests pass.

### Phase 6 — Containers, ECS, push-to-main deploy (≈2h)
- Three images: `ui`, `agent-api`, `mcp-server` (+ `trainer`). ECR repos (immutable tags = git SHA).
- One ECS task, three containers; only UI port public.
- `deploy.yml`: on push to main → assume OIDC role → build → push → new task def revision → update service → wait stable.
- **Done when:** a commit to main is live on ECS without manual steps.

## Day 2 — Wed 25 Nov

### Phase 7 — Production-grade Streamlit UI (≈2.5h)
- Welcome with capability cards and examples; chat with memory; receipt upload with OCR preview → prefilled claim; My claims; Manager queue (approve/reject); demo user switcher (production: Cognito).
- **Done when:** every flow works on the deployed URL.

### Phase 8 — Evaluation (≈2.5h)
- 30–40 labelled conversations: single + multi-tool, multi-intent, memory, workflow, OCR, guardrail attacks.
- Metrics: tool-choice accuracy, answer quality (exact checks + rubric), guardrail pass rate, workflow correctness, memory recall.
- Fix the top failures; re-run; record before/after.
- **Done when:** `docs/EVALUATION.md` with tables.

### Phase 9 — Docs and hardening (≈1.5h)
- README (architecture diagram, how it works, how to run, deploy, cost, teardown), security review, final CI/CD run.

### Phase 10 — Submit (by 16:30)
- Final push to main → live check → repo link as Trello comment.
