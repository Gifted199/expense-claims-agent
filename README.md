# Expense Claims Assistant

An AI assistant that employees chat with to **submit expense claims, scan receipts, check their spending and ask about the expense policy**. It runs entirely in the cloud: AWS in `eu-west-2`, with Azure OpenAI for the LLM and embeddings. All data is synthetic.

It combines a **deterministic workflow** with **agent decisions**:
- **Deterministic:** the `submit_claim` steps are fixed and always run in the same order: validate, then limits and city hotel caps, then duplicate, split, deadline and anomaly checks, then approve or send to a manager. Approval is decided in code and **never by the LLM**.
- **Agent:** for everything else, the LLM chooses which tools to call, and can call several in one turn.

Design: [docs/SPEC.md](docs/SPEC.md) · Plan: [docs/PLAN.md](docs/PLAN.md) · **Every design choice: [docs/DECISIONS.md](docs/DECISIONS.md)** (D1–D52).

## Architecture
```
 Browser (your IP only)
    │  :8501
┌───▼──────────────── ECS Fargate service akinsds1126-expense-app-svc (one task) ─────────────────┐
│  ui (Streamlit)  ──HTTP──▶  agent-api (FastAPI, 127.0.0.1:8000)  ──MCP──▶  mcp (FastMCP, 127.0.0.1:8080) │
│  no secrets                LangChain create_agent + guardrails          7 tools                │
│                            Azure OpenAI gpt-5.4-mini                    ├ search_policy  (RAG)  │
│                            chat memory (PostgresSaver)                  ├ score_claim_anomaly (ML)│
│                                                                         ├ extract_receipt (OCR) │
│                                                                         ├ get_my_claims / get_claim_status / spending_summary │
│                                                                         └ submit_claim (fixed workflow) │
└──────────────┬─────────────────────────────┬────────────────────────────────┬────────────────┘
               │                             │                                │
     RDS PostgreSQL 18 + pgvector      S3: receipts, model artefacts     Textract AnalyzeExpense
     claims, policy chunks, memory     Secrets Manager (all credentials) Azure OpenAI (LLM + embeddings)

One-off ECS tasks:  trainer (anomaly model → S3)   ingest (handbook → embeddings → pgvector + RAG eval)
CI/CD: GitHub Actions CI (lint, tests, gitleaks) ──▶ deploy job via OIDC ──▶ ECR ──▶ ECS (on push to main)
```

## How each requirement is met
| Requirement | How | Evidence |
|---|---|---|
| **MCP server with tools** | FastMCP over streamable HTTP. 7 tools. The employee comes from a header behind an internal token, **never from a tool argument** | [docs/MCP.md](docs/MCP.md), `tests/mcp_server` |
| **ML tool, trained by us, served from the cloud** | Anomaly detector trained as a **one-off ECS task** on point-in-time features with a time split; 3 candidates compared; model and metrics in S3; served by `score_claim_anomaly` and used in the workflow | [docs/ML.md](docs/ML.md) |
| **RAG over documents, cloud vector store** | Handbook generated from the DB; structure-aware chunks; Azure embeddings in **pgvector on RDS**; vector, keyword and hybrid compared on labelled questions; answers cite section IDs | [docs/RAG.md](docs/RAG.md) |
| **Guardrails** | Deterministic middleware. **Input:** length, prompt injection, investment advice (pure → refused without calling the LLM; mixed → only that part declined). **Output:** investment advice stripped, every £ figure must come from a tool result, policy answers must cite a section. **Workflow:** over-limit or unusual claims always go to a human | [docs/AGENT.md](docs/AGENT.md), D41 |
| **Managed DB, synthetic data** | Amazon RDS PostgreSQL with least-privilege roles per job (readonly, writer, ml, ingest, memory) and numbered migrations | [docs/DATA.md](docs/DATA.md) |
| **Short-term memory** | LangGraph `AsyncPostgresSaver` in its own schema and role; conversations bound to the employee; survives restarts | D40 |
| **Pre-commit hooks** | ruff, ruff-format, detect-secrets, file checks (gitleaks in CI) | `.pre-commit-config.yaml` |
| **Unit tests on every push** | GitHub Actions: pre-commit, gitleaks (full history plus a config self-test), about 400 pytest tests, with no cloud calls (AWS and Azure mocked; scripted fake LLM) | `.github/workflows/ci.yml` |
| **Push to main deploys** | The `deploy` job in `ci.yml` runs after the tests pass, on pushes to main only. It signs in through **GitHub OIDC** (no stored keys), builds three images tagged with the commit SHA, renders the task definition, updates the service and waits for stability | [docs/DEPLOY.md](docs/DEPLOY.md), D51 |
| **Evaluation** | 21 labelled conversations; tool choice, answer checks, guardrails, memory; no LLM judge | [docs/EVALUATION.md](docs/EVALUATION.md) |
| **Cloud only** | ECS Fargate, RDS, S3, Textract, Secrets Manager, ECR, CloudWatch, Azure OpenAI. Nothing runs locally except clients | — |
| **OCR** | Amazon Textract AnalyzeExpense on receipts in S3, then a prefilled claim in the chat | `src/expense_agent/ocr` |
| **Production-grade UI** | Welcome cards, examples, chat with memory, receipt scan into a claim, My claims, Manager queue (managers only, with the reasons) | `src/expense_agent/ui` |

## Results
- **ML** ([docs/ML.md](docs/ML.md)): `robust_zscore` won with **PR-AUC 0.614** (95% CI 0.43–0.78) on held-out months, against a random baseline of 0.033. It is statistically tied with Isolation Forest and won on the pre-declared rule. Its scores are explainable per feature.
- **RAG** ([docs/RAG.md](docs/RAG.md)): vector search with `text-embedding-3-large` scored **Recall@5 0.92, MRR 0.90** on 30 questions. An exact section-ID lookup, added later and validated on 10 held-out questions, brought this to **Recall@5 1.00, MRR 0.98**. Out-of-scope questions score below a 0.35 similarity threshold.
- **Agent** ([docs/EVALUATION.md](docs/EVALUATION.md)): **19/21 conversations pass**. Guardrails 25/25, memory 4/4, tool choice 20/21. The open failure ("where do I find my claim reference?") and the caveats are documented there.

## Security choices
- **Identity never comes from the LLM.** The API takes the employee from its session and sends it to the MCP server in a header, behind a shared token held in Secrets Manager. No tool has an identity argument. Someone else's claim looks exactly like one that doesn't exist.
- **Approvals are code.** Over-limit, duplicate, split, late or unusual claims always go to `pending_manager`. Managers approve or reject in the UI, only for their direct reports, and never through an agent tool. The database blocks self-approval.
- **Least privilege:** one database role per job. The query tools are read-only. `review_reasons` can't be rewritten once written (checked live). Each IAM role is scoped by ARN. The deploy role trusts only `main` of this repo.
- **Network:** only the UI port is exposed, and only to the owner's IP, because the demo user switcher is a demo login; production would use Cognito. The agent API and MCP server listen on localhost. Databases use TLS with full certificate verification.
- **No secrets in code or git.** Secrets Manager holds everything. detect-secrets runs locally and gitleaks runs in CI. Parameterised SQL only. Uploads are checked by file content and size. No message text appears in the logs.

## Run and deploy
```bash
uv sync                                           # Python 3.12, uv
uv run python -m pytest                           # unit tests (no cloud calls)
uv run python -m pre_commit run --all-files

# Local run against the cloud services (AWS profile "aero"); see docs/AGENT.md
uv run python -m expense_agent.mcp_server --profile aero
uv run python -m expense_agent.api --profile aero --demo-auth --port 8001
EXPENSE_API_URL=http://127.0.0.1:8001 uv run python -m streamlit run src/expense_agent/ui/app.py --server.port 8502
uv run python -m expense_agent.evaluation.run --api http://127.0.0.1:8001
```
**Deploy:** merge a pull request to `main`. CI runs the tests, then its `deploy` job builds, pushes and deploys, and prints the new URL (`http://<public-ip>:8501`). The one-time bootstrap and console steps are in [docs/DEPLOY.md](docs/DEPLOY.md). The data setup (migrations, roles, seed, model training, RAG ingest) is in [docs/DATA.md](docs/DATA.md), [docs/ML.md](docs/ML.md) and [docs/RAG.md](docs/RAG.md).

## Limitations and future work
- **Single synthetic dataset and one LLM deployment.** The evaluation set is small and was written by the developer (see the caveats).
- **"Where do I find my claim reference?"** still ends in the safe fallback message (EVALUATION c021).
- **The anomaly model is strict with new merchants:** every claim at a merchant nobody has used before goes to a manager.
- **Hosting:** no load balancer, so the public IP changes on each deploy. One task role is shared by the containers (D49). The login is a demo switcher; production would use Cognito with an ALB and HTTPS.
- **Not done yet:** Langfuse tracing, an LLM comparison, streaming replies, chat history trimming, and connection pooling for the API's reads. Full lists are in DECISIONS.md under "future work".

## Cost and teardown
**Approximate running cost, eu-west-2:**
- **ECS app task** (1 vCPU / 3 GB, always on): about **$45/month**, plus about $4/month for its public IPv4 address.
- **RDS** `db.t4g.micro`, 20 GB, single-AZ: about **$15–20/month**.
- S3, ECR, Secrets Manager (7 entries, about $3/month) and CloudWatch: a few dollars.
- **Per use:** Textract AnalyzeExpense is about $0.01 per page. Azure OpenAI charges per token: `gpt-5.4-mini` for chat, `text-embedding-3-large` for the policy search.
- The trainer and ingest jobs only cost while they run.

**Teardown** (in this order):
1. ECS: set the service's desired count to 0, then delete `akinsds1126-expense-app-svc`, and deregister the task definitions.
2. Delete the ECR repos `akinsds1126-expense-{mcp,agent,ui,trainer,ingest}`.
3. Delete the RDS instance `akinsds1126-expense-db`. Take a final snapshot if you want to keep the data.
4. Empty and delete the S3 buckets `akinsds1126-expense-receipts` and `akinsds1126-expense-artifacts`.
5. Delete the Secrets Manager entries `akinsds1126-expense-*`.
6. Delete the IAM roles `akinsds1126-expense-*` (the app task, trainer, ingest and GitHub deploy roles; the execution role if it isn't shared).
7. Delete the security groups `akinsds1126-expense-*` and the log groups `/ecs/akinsds1126-expense-*`.
8. Delete the Azure OpenAI deployments if they aren't used elsewhere.
