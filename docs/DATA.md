# Data: schema and synthetic dataset

**All data is synthetic.** Names are made up, emails use the reserved `example.com` domain, and merchants are fictional.
- Database: RDS PostgreSQL 18 `akinsds1126-expense-db`, database `expense`, region eu-west-2.
- Schema: versioned migrations in [`sql/`](../sql).
- Generator: [`src/expense_agent/db/synthetic.py`](../src/expense_agent/db/synthetic.py), seed 42.

## Tables, in plain words

| Table | What it is | Why it looks like this |
|---|---|---|
| `departments` | The 5 teams (Operations, Sales, Engineering, Marketing, Finance), each with a cost centre | Real expense systems charge spend to a cost centre, and spending summaries group by it |
| `employees` | 40 people with a department, a **grade** (associate, senior, director), a **manager** and a home currency (GBP) | Grade decides which limits apply. The manager link routes over-limit claims to a human. Nobody can be their own manager |
| `expense_categories` | The 9 allowed kinds of spend: meals, taxi, rail, hotel, air, mileage, client entertainment, office supplies, training | A fixed list stops free-text categories drifting. Each has a "receipt required over £X" amount |
| `policy_limits` | For each category and grade: a **per-claim** and a **per-month** limit in GBP | Example: an associate's meal is limited to £30 per claim and £300 per month, a director's to £60 and £600. This is what the deterministic workflow checks |
| `claims` | One row per expense: who, category, amount, **when it was spent** (`incurred_at`), when it was submitted, merchant, description, status, receipt S3 key, anomaly score | Status is `submitted`, `approved`, `pending_manager` or `rejected`. `anomaly_score` is filled by the Phase 2 model |
| `approvals` | Every decision on a claim: decided by the **system** (auto) or a **manager**, approved or rejected, with a reason and timestamps | Append-only for the app. A database trigger **blocks anyone from deciding their own claim** |
| `audit_log` | An append-only trail: who did what to which record, plus JSON details | The app can only INSERT. History can't be rewritten |
| `claim_labels` | **Eval only:** which claims are planted anomalies, the anomaly type, and the related claim (original or first split part) | Kept apart so the model and agent can't see the answers. No app role can read it |
| `schema_migrations` | Which migration files have run, with checksums | Makes migrations idempotent and detects edited history |

**Roles:**
- `app_readonly` (query tools): SELECT on business tables only. It can't read `claim_labels` or `audit_log`, and its transactions are read-only by default.
- `app_writer` (workflow and manager decisions):
  - INSERT on claims, approvals and audit_log
  - UPDATE on only `claims.status`, `anomaly_score` and `receipt_s3_key`
  - Decisions and audit entries can't be edited, and there's no DELETE anywhere (D2, D10).

## How the normal data is made
- **Window:** 1 Nov 2025 to 31 Oct 2026. The dataset's "now" is 1 Nov 2026, 09:00 London time.
- **Volume:** about 3,000 claims.
  - Each person makes a random number of claims per month. The average depends on the department: Sales about 9 a month, Engineering and Finance about 4.5.
  - Seasonality:
    - August is quiet, and so is the Christmas break (22 Dec to 2 Jan)
    - March, September and October are busy
    - client entertainment spikes in December
    - training spikes in January and September
- **Amounts:** skewed (log-normal) around a typical value per category, for example meals about £18, hotels about £120 and training about £400.
  - Each person has a personal spending level.
  - A quarter of hotel stays are pricier London stays.
- **Times:**
  - meals at lunch or dinner
  - office supplies and training in working hours
  - non-travel spend on weekdays only
- **Legitimate edge cases on purpose:** late taxis home after a client dinner, Sunday-night hotels before a Monday meeting, and expensive London hotels. Detection has to tell these apart from real anomalies.
- **Statuses follow the workflow rules:**
  - Over a per-claim or monthly limit means a manager decides (mostly approved, some rejected). Those still in the last 14 days are `pending_manager`.
  - Within the limits means auto-approved by the system. Claims from the last 3 days are still `submitted`.

## Planted anomalies (about 4% of claims)
Concentrated in 6 "repeat offenders" (60% of anomalies), with the rest spread across everyone.

| Type | What it looks like | Why it's realistic |
|---|---|---|
| `duplicate_exact` | The same claim (merchant, amount, date, description) resubmitted 1–10 days later | The most common expense error or fraud: resubmitting by mistake or hoping nobody notices |
| `duplicate_near` | A resubmission with the amount changed by 1p to £2, the date shifted by a day, or the description reworded | Small edits get past exact-match duplicate checks |
| `amount_outlier` | 4–8× this person's usual amount for the category, for example a £140 lunch from someone who usually spends £15. It can still be under the policy limit | Inflated claims. Limits alone miss them, so "unusual for *this person*" matters |
| `odd_time` | Non-travel spend (meals, office supplies, training, entertainment) at the weekend or between 23:00 and 05:00 | Personal spend disguised as business falls outside working patterns. Time alone isn't proof, because legitimate late taxis exist |
| `category_mismatch` | A merchant from another category, for example an electronics shop, golf club or spa claimed as office supplies, or a restaurant claimed as training | Mislabelling is how personal or disallowed spend hides inside an allowed category |
| `split_claim` | One purchase over the per-claim limit, split into 2–3 claims each at 85–99% of the limit. Same merchant, same or next day | A classic way to dodge approval: each part looks fine alone |

Many anomalies were **auto-approved** by the rules. That's the point: rules miss them, and the Phase 2 model has to catch them.

## Commands
```bash
uv run python -m expense_agent.db.migrate --profile aero       # idempotent
uv run python -m expense_agent.db.app_roles --profile aero     # (--create-secret the first time)
uv run python -m expense_agent.db.seed --profile aero          # --reset to reload
uv run python -m expense_agent.db.verify --profile aero        # counts, anomaly rate, security checks
```
