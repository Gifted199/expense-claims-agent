# SPEC — Expense Claims Assistant (Agentic AI on AWS)

Owner: Akinlolu Oyetakin · Due: Wed 25 Nov 2026, 17:00 · Synthetic data only

## 1. What we are building

One AI agent that employees chat with to **submit expense claims**, **upload receipts**, and **ask about spending and expense policy**.
It combines two kinds of behaviour:

- **Deterministic workflow (always the same fixed steps):** submit claim → validate fields → check policy limits → score anomaly → auto-approve OR route to a manager.
- **Agent decisions:** for questions, the LLM decides which tool(s) to call (database, policy RAG, anomaly model, OCR) and can call several in one turn.

## 2. Hard constraints (from the brief)

- Cloud only: no local DB, model, vector store or LLM. Everything runs on AWS, except the LLM and embeddings, which use **Azure OpenAI** (approved by trainer because Bedrock is blocked by an organisation SCP).
- Synthetic data only.
- One agent.
- Push to GitHub; repo link as a Trello comment.

## 3. Requirements traceability (must-haves → design → evidence)

| # | Must-have | Design | Evidence of done |
|---|---|---|---|
| R1 | MCP server exposing tools | FastMCP server (streamable HTTP) in its own container; agent connects with `langchain-mcp-adapters` | Tool list visible from agent; MCP tests pass |
| R2 | ML tool, trained by you, served from cloud | Anomaly detector trained by a one-off ECS task, model + metrics saved to S3, loaded by MCP server; tool `score_claim_anomaly` | Model comparison table; S3 artefact; tool returns score |
| R3 | RAG tool over documents, cloud vector store | Synthetic expense policy handbook → chunks → Azure embeddings → **pgvector on RDS**; tool `search_policy` with citations | Retrieval recall on labelled questions |
| R4 | Input + output guardrails + security guardrails | See §5 | Guardrail tests + eval guardrail pass rate |
| R5 | Managed cloud DB, synthetic data | **Amazon RDS for PostgreSQL** (employees, policy limits, claims, approvals, vectors, chat memory) | Seed script, row counts |
| R6 | Short-term memory | LangGraph **PostgresSaver** checkpointer on RDS, `thread_id` per conversation | Multi-turn eval cases pass after container restart |
| R7 | Pre-commit hooks | ruff, ruff-format, gitleaks secret scan, basic file checks | `pre-commit run --all-files` clean |
| R8 | Unit tests on every push | GitHub Actions `ci.yml` (lint + pytest, no cloud calls) | Green CI badge |
| R9 | Push to main deploys | GitHub Actions `deploy.yml` via **OIDC role** → build → ECR → ECS | Commit on main appears live |
| R10 | Evaluation | Labelled conversation set: tool choice accuracy, answer quality, guardrail pass rate (+ workflow correctness, memory) | `docs/EVALUATION.md` |
| R11 | Cloud only | ECS Fargate, RDS, S3, Textract, Secrets Manager, ECR, CloudWatch; Azure OpenAI for LLM | Architecture diagram |
| R12 | OCR on uploaded documents | **Amazon Textract AnalyzeExpense** on receipts uploaded to S3 | Receipt → prefilled claim |
| R13 | Key guardrail | No investment advice; claims over limit always need a human | Tests + eval cases |
| R14 | User requests: multi-part questions answered | Agent may call several tools per turn; allowed parts answered, blocked parts declined | Multi-intent eval cases |
| R15 | User request: production-grade UI that shows what it offers | Streamlit: welcome + capability cards, chat, receipt upload, My claims, Manager queue | Screenshots |

## 4. Tools exposed by the MCP server

| Tool | Kind | Notes |
|---|---|---|
| `submit_claim` | Deterministic workflow | Fixed steps; returns approved / pending_manager / rejected with reasons |
| `get_my_claims`, `get_claim_status` | DB read | Scoped to the logged-in employee (identity injected by server, never taken from the LLM) |
| `spending_summary` | DB read | Parameterised queries (period, category); no free-form SQL |
| `search_policy` | RAG | Returns passages + section citations |
| `score_claim_anomaly` | ML | Returns score, label, top reasons |
| `extract_receipt` | OCR | Textract AnalyzeExpense on an uploaded S3 object |

Manager approve/reject is **not** an agent tool: only a human manager can do it, in the UI.

## 5. Guardrails

- **Input:** length and file type/size limits, prompt-injection patterns, off-topic and **investment-advice** detection, PII redaction in logs.
- **Output:** no investment advice; every money figure must come from a tool result; policy answers must cite a handbook section; no other employee's data.
- **Workflow / security:** over-limit or anomalous claims → `pending_manager` (code-enforced, never LLM-decided); read-only DB user for query tools; secrets in Secrets Manager; least-privilege IAM roles; internal-only ports for MCP and agent API.
- **Multi-intent:** split decisions per part; answer allowed parts, decline blocked parts.

## 6. Decisions made by testing, not guessing

| Decision | Candidates | Metric |
|---|---|---|
| Anomaly model | Isolation Forest, Local Outlier Factor, robust z-score rule baseline | PR-AUC, precision@k, recall on planted anomalies |
| LLM deployment | Azure OpenAI deployments available (e.g. gpt-5.4-mini vs a larger model) | Tool-choice accuracy, guardrail pass rate, latency, cost |
| Retrieval | Vector only vs hybrid (pgvector + Postgres full-text) | Recall@k on labelled handbook questions |
| Embedding model | text-embedding-3-small vs -large (if deployed) | Recall@k, cost |
