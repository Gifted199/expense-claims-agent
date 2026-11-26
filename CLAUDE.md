# CLAUDE.md — Expense Claims Assistant

## Purpose
One AI agent that employees chat with to **submit expense claims**, **upload receipts**, and **ask about spending and expense policy**.
It mixes a **deterministic workflow** (submit → validate → policy limits → anomaly score → auto-approve or route to manager) with **agent decisions** (the LLM picks which MCP tools to call: DB, policy RAG, anomaly model, OCR).
Full detail: [docs/SPEC.md](docs/SPEC.md). Phase plan and "done when" criteria: [docs/PLAN.md](docs/PLAN.md).

## Hard constraints
- **Cloud only:** no local DB, model, vector store or LLM. Everything runs on AWS (ECS Fargate, RDS PostgreSQL + pgvector, S3, Textract, Secrets Manager, ECR, CloudWatch).
- **LLM and embeddings:** Azure OpenAI only. Bedrock is blocked by an organisation SCP.
- **AWS region:** `eu-west-2`.
- **Resource names** start with `akinsds1126-expense-`.
- **Synthetic data only.** Never use real people, receipts or company data.
- **One agent.**
- **Deadline:** Wed 25 Nov 2026, 17:00.

## Commands
On the dev PC, Windows Application Control blocks the `.exe` launchers uv generates (`pytest.exe` and similar). Always run tools as Python modules:
```bash
uv sync                                       # install deps (incl. dev group)
uv run python -m pytest                       # unit tests (no cloud calls)
uv run python -m ruff format .                # format
uv run python -m ruff check --fix .           # lint
uv run python -m pre_commit run --all-files   # all pre-commit hooks
uv run python -m detect_secrets scan --baseline .secrets.baseline  # refresh secrets baseline
```

The pre-commit-hooks file checks are defined as `repo: local` hooks that run with `uv run python -m pre_commit_hooks.<module>`, because Windows Application Control blocks pip's `.exe` launchers.

Database (RDS, eu-west-2). Credentials are always read from Secrets Manager, never hardcoded. See [docs/DATA.md](docs/DATA.md).
```bash
uv run python -m expense_agent.db.migrate --profile aero     # apply sql/NNN_*.sql (idempotent)
uv run python -m expense_agent.db.app_roles --profile aero   # set app-role passwords from secret
uv run python -m expense_agent.db.seed --profile aero        # load synthetic data (--reset to reload)
uv run python -m expense_agent.db.verify --profile aero      # counts, anomaly rate, privilege checks
```
- Schema changes go in a **new** `sql/NNN_name.sql`. Never edit an applied migration (checksums are enforced).
- Code uses `expense_agent.db.connection.connect(role)`: `readonly` for query tools, `writer` for the workflow, `master` only for migrations and seeding.
- `claim_labels` is eval-only. App code, model features and the agent must never read it.

ML trainer image (runs in the cloud as a one-off ECS task; never train on the laptop). See [docs/ML.md](docs/ML.md).
```bash
docker build --platform linux/amd64 --build-arg GIT_SHA=$(git rev-parse HEAD)   -t 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-trainer:$(git rev-parse HEAD)   -f docker/trainer.Dockerfile .
AWS_PROFILE=aero docker push 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-trainer:<sha>
```
- **Never** use `aws ecr get-login-password | docker login`. It fails on this laptop, and Docker's `ecr-login` credential helper handles auth (D20).
- Model features must be point-in-time and must never use status, approvals or claim_labels (D11, D12).

MCP server (Phase 4). Local runs are clients of the cloud services only. See [docs/MCP.md](docs/MCP.md).
```bash
uv run python -m expense_agent.mcp_server --profile aero                     # serve on 127.0.0.1:8080
uv run python -m expense_agent.mcp_server.smoke --profile aero --employee 5  # list + call every tool
uv run python -m expense_agent.ocr.make_receipts --profile aero --upload     # synthetic receipts
```
- Identity comes only from the `X-Employee-Id` header behind the internal token (D33). Never add an identity argument to a tool.

Slash command: `/review-phase <N>` reviews the current diff against Phase N's "Done when" in PLAN.md.

## Secret scanning
gitleaks runs in CI because Windows Application Control blocks unsigned binaries locally. detect-secrets covers local commits, using `.secrets.baseline`.

## Decision log
Whenever we make a design decision, add an entry to [docs/DECISIONS.md](docs/DECISIONS.md): decision, alternatives, why.

## Git workflow (mandatory)
- One branch per phase: `phase-<N>-<slug>` (e.g. `phase-0-foundation`).
- Commit on that branch → push → open a pull request to `main` → **merge only when CI is green**.
- **Never commit or push directly to `main`.** `main` auto-deploys, so it must only receive tested code.
- Each phase: plan → implement with tests → `/review-phase` → commit → PR.

## Coding rules
- **Typed:** type hints on all functions and public attributes.
- **Tested:** every feature ships with unit tests. CI makes no cloud calls, so mock AWS and Azure.
- **No secrets in code or git.** Use Secrets Manager in the cloud and environment variables or an uncommitted `.env` locally.
- **Parameterised SQL only.** Never build SQL with f-strings, `%` or concatenation, and never execute free-form SQL from the LLM.
- **Identity never comes from the LLM.** The employee ID is injected server-side from the session. Tools ignore any identity the model supplies.
- **Approval decisions are code, not LLM.** Over-limit or anomalous claims always go to `pending_manager`. Only a human manager approves or rejects, in the UI.
- Least-privilege IAM, a read-only DB role for query tools, and internal-only ports for the MCP server and agent API.

## Communication
**Explain every change in plain words to the user:** what changed, and why.
