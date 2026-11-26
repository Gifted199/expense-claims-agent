# Agent, memory, guardrails, API and UI (Phase 5)

Decisions: D38–D46 in [DECISIONS.md](DECISIONS.md). Tools: [MCP.md](MCP.md).

```
Streamlit UI ──HTTP──> Agent API (FastAPI) ──streamable HTTP + token + X-Employee-Id──> MCP server
 (no secrets)           create_agent + guardrails                                       (7 tools)
                        Postgres memory (schema agent_memory, role app_memory)
                        RDS reads (profile, manager queue) · manager decisions · S3 uploads
```

## The parts in plain words
- **The agent:** one LangChain agent using Azure OpenAI `gpt-5.4-mini`. For each message it decides which of the 7 tools to call, and it can call several at once. It's rebuilt for every request with the signed-in employee fixed in the tool connection, so the model can't act for anyone else.
- **Memory:** each conversation has an ID. Every turn is saved in RDS, in its own schema, by a database user that can't see claims or employees. A follow-up such as "what's the status of *that* claim?" works even after the API restarts. Conversations are tied to the employee, so nobody can open someone else's chat.
- **Guardrails** run as code, before and after the model:
  - **Before:** messages that are too long, prompt-injection attempts ("ignore your instructions", "I am employee E0003…") and pure investment questions are refused **without calling the model**. A question that mixes allowed parts with investment advice is answered, and only the investment part is declined.
  - **After:** investment advice is removed; every £ amount must come from a tool result (or from what you typed); a policy answer must cite a handbook section. If not, the model gets one chance to fix its answer, otherwise you get a safe message.
  - **Approvals are code:** the claim workflow sends anything over a limit or unusual to a manager, whatever the model says.
- **The agent API:** chat, receipt upload with an OCR preview, My claims, and **manager endpoints** (the queue, and approve or reject with a reason). Managers act through plain buttons, never through the chat. Only a pending claim of one of your direct reports can be decided.
- **The UI:**
  - a welcome screen with cards showing what the assistant does, and clickable example questions
  - a chat that remembers the conversation
  - receipt scanning, with a "create a claim from this receipt" button
  - My claims
  - a Manager queue tab that only managers see, showing *why* each claim was held
  - **demo user switcher:** a demo login over synthetic employees. Production would use Amazon Cognito. Because of the switcher, the deployed UI will be reachable from your IP only (Phase 6).

## Running it locally (cloud services only)
```bash
uv run python -m expense_agent.mcp_server --profile aero                        # :8080
uv run python -m expense_agent.api --profile aero --demo-auth --port 8001       # :8001
EXPENSE_API_URL=http://127.0.0.1:8001 uv run python -m streamlit run src/expense_agent/ui/app.py --server.port 8502
uv run python -m expense_agent.agent.smoke --profile aero --api http://127.0.0.1:8001
```
On this laptop, Docker Desktop already uses ports 8000 and 8501, which is why the API runs on 8001 and the UI on 8502. The containers use 8000 and 8501.

## Cloud smoke result (2026-11-26; employee 6, senior; manager employee 1)
All conversation checks passed against RDS, S3, Textract and Azure OpenAI:

| # | You said | What happened |
|---|---|---|
| 1 | "What's the nightly hotel cap in London for my grade?" | `search_policy`. The answer listed £200.00 / £230.00 / £300.00 by grade and cited **EXP-6.3**. |
| 2 | "How much have I spent this year, by category?" | `spending_summary`: £5,461.80 in total, broken down by category. Every figure passed the £ check. |
| 3 | Uploaded the CityCab taxi receipt, then asked to submit it | The upload went to `uploads/E0006/<random>.png` and OCR read CityCab, 2026-11-24, £24.00. `submit_claim` **approved** it (CLM-2026-002939) and listed every passed check. |
| 4 | "Please submit my Riverside Lodge hotel bill of £520.00 …" | `submit_claim` returned `missing_fields`, and the agent asked for the city and the number of nights. "London, 2 nights" → **pending_manager** (CLM-2026-002940): "£260.00/night is over the £230.00 cap for London at senior grade (EXP-6.3)", plus a high anomaly score. |
| 5 | "What's the receipt rule for meals, and which ETF should I buy with my bonus?" | The meal-receipt rule was answered with **EXP-3.1, EXP-3.2**, and the ETF part was declined in one line (flag `investment_mixed`). |
| 6 | "What's the status of that hotel claim now?" (same conversation as 4) | Resolved "that hotel claim" from memory → `get_claim_status` → still pending manager. |
| 7 | Manager steps (no LLM) | Employee 1's queue showed CLM-2026-002940 with both reasons, and the rejection with a reason succeeded. Employee 6 trying to decide **his own** claim got 404. |

**One check needed fixing:** the first run's "a non-manager can't decide" check expected 403, but employee 6 is also a manager (of E0016), so the API correctly answered 404. The check now tries the claimant deciding their own claim *before* the manager acts and accepts 403 or 404. This was re-checked live: 404.

The UI was also driven live through Streamlit's test harness against the real API:
- 5 capability cards
- an example question answered with EXP-3.1
- My claims (50 rows)
- the Manager queue (3 claims for employee 1)
- the tabs change when switching user
