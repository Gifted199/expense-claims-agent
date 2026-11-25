# MCP server (Phase 4)

One FastMCP server, reached over streamable HTTP on an internal port (8080). Only the agent API calls it. Decisions: D32–D37 in [DECISIONS.md](DECISIONS.md).

## Who is calling: identity and auth
- Every request carries two headers:
  - `Authorization: Bearer <token>`. The token is a shared internal secret, stored in Secrets Manager (entry `akinsds1126-expense-mcp-internal`).
  - `X-Employee-Id: <id>`: the logged-in employee. The agent API sets it from its own session.
- A request with a missing or wrong token, or a missing or repeated employee header, gets **401** before any tool runs.
- **No tool has an "employee" argument.** The model can't choose whose data it sees, and a claim that isn't yours looks exactly like one that doesn't exist.
- Manager approve/reject is **not** a tool. Only a human manager can do that, in the UI.

## The tools in plain words
| Tool | What it does | Database role |
|---|---|---|
| `search_policy(question)` | Finds the passages of the **latest** handbook that answer a policy question. Each passage has its section ID (for example EXP-6.3) for the answer to cite. If the best match is weak (similarity under 0.35) and no section ID was named, it returns `not_covered: true`, so the agent says the handbook doesn't cover it instead of guessing. | `app_readonly` |
| `score_claim_anomaly(claim_id, or category/amount/merchant/incurred_at)` | Says how unusual a claim looks compared with this person's and everyone's **past** claims. The score runs 0–1, and 0.95 or more is "high". It also gives up to 3 reasons in plain words, for example "same merchant and amount already claimed within 10 days". It works on one of your existing claims or on a claim you're about to submit. | `app_readonly` |
| `extract_receipt(s3_key)` | Reads an uploaded receipt image with Amazon Textract and returns vendor, date, total, currency and a confidence. `needs_review` means "ask the user to check". It only reads the shared demo receipts or your own uploads, and it never writes receipt text to the logs. | none |
| `get_my_claims(status?, date_from?, date_to?)` | Lists your own claims, newest first (at most 50). | `app_readonly` |
| `get_claim_status(claim)` | One of your claims, by number or `CLM-…` reference, with its latest decision and reason. | `app_readonly` |
| `spending_summary(period, group_by)` | Your totals for this month, last month, this quarter, this year or the last 12 months, by category or by month. Rejected claims are left out. It runs fixed queries only. | `app_readonly` |
| `submit_claim(category, amount, merchant, incurred_at, description, receipt_s3_key?, city?, nights?)` | Files a claim and runs the **fixed** checks below. It is approved only if every check passes; otherwise it goes to your manager, with every reason listed. | `app_writer` |

### submit_claim, step by step (D34)
1. **Check the fields.**
   - **A hotel claim without a city or number of nights returns `missing_fields`**, and the agent asks for them. Nothing is guessed (D36).
   - A receipt is required over the category's threshold.
   - Expenses over 90 days old are refused.
2. **Limits:** the per-claim limit (hotels: the nightly rate against the city cap, or the UK-wide hotel limit), then the monthly total.
3. **Duplicate:** same merchant and amount within 10 days.
4. **Split purchase:** same merchant within a day, where the combined amount is over the limit.
5. **Deadline:** more than 30 days old goes to the manager (EXP-2.3).
6. **Anomaly score:** high goes to the manager.
7. **Decision in code:** all passed → `approved`. Otherwise → `pending_manager`. One transaction writes the claim, the system approval (if any) and an audit-log row.

## Running it locally (cloud services only)
```bash
uv run python -m expense_agent.mcp_server --profile aero                     # 127.0.0.1:8080
uv run python -m expense_agent.mcp_server.smoke --profile aero --employee 5  # in another terminal
```
- There's no local database, model or vector store. The server uses RDS, S3, Textract and Azure OpenAI through the AWS profile.
- **The smoke test writes real synthetic claims to RDS.** Their descriptions start with `[smoke]`.

## Synthetic receipts
`uv run python -m expense_agent.ocr.make_receipts --profile aero --upload` draws 5 fictional receipts and uploads them to `s3://akinsds1126-expense-receipts/samples/receipts/` with a `manifest.json` of the expected values. Every image is stamped "SYNTHETIC RECEIPT - DEMO DATA ONLY".

## Cloud smoke test result (2026-11-25, employee 6, senior)
All 16 checks passed against RDS, S3, Textract and Azure OpenAI:
- Calls without the token, or with a wrong token, got **401**. The tool list has exactly the 7 tools.
- `search_policy` put **EXP-6.3** first for the London senior hotel question. "Which ETF should I buy for my pension?" came back `not_covered` (top similarity 0.321).
- `get_my_claims`, `spending_summary`, `get_claim_status` and `score_claim_anomaly` worked on the employee's own claims. Claim 1, which belongs to someone else, was "not found".
- `extract_receipt` on `r03.png` returned vendor "Riverside Lodge Hotel", date 2026-11-20, total 520.00 GBP and confidence 1.0, matching the manifest. Another employee's upload path was refused. The server log held only the key and the confidence.
- `submit_claim`:
  - a hotel claim without city or nights → `missing_fields` (`city`, `nights`)
  - with London and 2 nights → `pending_manager`: "£260.00/night is over the £230.00 cap for London at senior grade (EXP-6.3)", plus a high anomaly score
  - the demo lunch → `pending_manager` because of a high anomaly score
- **Known model behaviour** (Phase 2 model, not a tool bug): `robust_zscore` takes the most extreme feature. Almost every training claim is at a merchant seen before, so **any new merchant scores about 0.99** and goes to a manager. That's safe (a human looks), but strict. Listed in Phase 4 future work.
