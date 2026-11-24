# ML: anomaly detector for expense claims

**Winner: `robust_zscore` (rule baseline). PR-AUC 0.614 (95% CI 0.43–0.78) on held-out months.**

| | |
|---|---|
| Model version | `20261124T170142Z-431e8b4` (git `431e8b4`) |
| Artefacts | `s3://akinsds1126-expense-artifacts/models/anomaly/20261124T170142Z-431e8b4/` (`model.joblib`, `metrics.json`, `comparison.md/.csv`, `features.json`, `model_card.md`) |
| Pointer | `s3://akinsds1126-expense-artifacts/models/anomaly/latest.json` |
| Trained where | One-off ECS Fargate task `akinsds1126-expense-trainer:1` on `akinsds1126-expense-cluster`, 24 Nov 2026 17:01 UTC, exit code 0, about 30 seconds. Logs are in CloudWatch `/ecs/akinsds1126-expense-trainer` |
| Scores written | `claims.anomaly_score` for all 2,936 claims (0–1). This was a **one-off backfill** by run 1 (task definition revision 1 with `--score-claims`). From revision 2 onwards the trainer never writes to the database (D21) |

Purpose: put the most unusual claims in front of a human reviewer first.
- **It never rejects or approves anything by itself.** Over-limit or anomalous claims always go to a manager, and that rule is enforced in code.
- Used by the `score_claim_anomaly` MCP tool in Phase 4.

## Results (held-out months)
Train: 2,189 claims submitted Nov 2025 to Jul 2026, including 92 planted anomalies that the models never saw as labels.
Test: **747 claims submitted Aug–Oct 2026, with 25 labelled anomalies.** Review budget k = 38 (5%).

| Model | PR-AUC | PR-AUC 95% CI | Precision@k | Recall@k |
|---|---|---|---|---|
| **robust_zscore** | **0.614** | 0.426–0.784 | 0.395 | 0.60 |
| isolation_forest | 0.575 | 0.370–0.748 | 0.395 | 0.60 |
| local_outlier_factor | 0.375 | 0.193–0.571 | 0.289 | 0.44 |

Random ordering would score PR-AUC of about 0.033 (the test anomaly rate, 25/747).

**Recall@k by anomaly type** (the number in brackets is how many test claims of that type there were):

| Model | amount_outlier (5) | category_mismatch (3) | duplicate_exact (2) | duplicate_near (3) | odd_time (1) | split_claim (11) |
|---|---|---|---|---|---|---|
| robust_zscore | 0.80 | 1.00 | 1.00 | 1.00 | 1.00 | 0.18 |
| isolation_forest | 0.80 | 0.67 | 1.00 | 1.00 | 1.00 | 0.27 |
| local_outlier_factor | 0.80 | 0.33 | 1.00 | 0.33 | 1.00 | 0.18 |

## What this means, in plain words
- **All three models are far better than random.** The baseline and Isolation Forest put 60% of the test anomalies into the top 5% of claims, and about 2 in 5 of the claims a reviewer would look at are real anomalies.
- **The baseline and Isolation Forest are statistically tied.** Their intervals overlap almost completely. The baseline won on the pre-agreed rule (highest PR-AUC, D14), and it has a bonus: every score can be explained as "this feature was N typical spreads above normal".
- **LOF is clearly weaker.** Distances in a 14-feature space blur the few features that matter.
- **Split claims are the weak spot** (18–27% recall).
  - The first part of a split looks like an ordinary claim, because nothing earlier exists to compare it with.
  - Only the later parts reveal the split.
  - That accounts for most of the misses.
- **The per-type numbers rest on very few claims**, for example 1 odd-time claim and 2 exact duplicates, so read them as indications, not measurements.

## Features (point-in-time, D12)
Each claim is described using only the claim itself and the claims **submitted before it**, which is what the system really knows at submission time. **Status, approvals and anomaly labels are never used (D11).**

| Feature | Plain words |
|---|---|
| `log_amount_vs_emp_median` | How much bigger this is than the person's own past typical claim in this category (the category's past median if they have fewer than 3 past claims) |
| `emp_category_history_n` | How many past claims they have in this category, i.e. how much to trust the ratio above |
| `amount_vs_limit` | Amount ÷ the per-claim limit for their grade |
| `log_amount_vs_category_median` | Amount compared with everyone's past claims in this category |
| `hour`, `is_weekend`, `is_night` | When the money was spent (night is 23:00–05:00) |
| `non_travel_off_hours` | Non-travel spend at a weekend or at night. Legitimate late taxis and Sunday hotels are excluded |
| `dup_exact_10d` | Earlier claims by the same person at the same merchant for the same amount, within 10 days |
| `dup_near_10d` | The same, within £2 but not equal |
| `split_sum_vs_limit` | This claim plus the person's earlier same-merchant, same-category claims within ±1 day, ÷ the per-claim limit |
| `split_count` | How many claims are in that same-merchant, same-category group within ±1 day |
| `merchant_seen_before` | Has anyone claimed at this merchant before? |
| `merchant_category_share` | Of the past claims at this merchant, the share filed under this category. It's learned from history, not from the generator (D13) |

## Metrics, in plain words
- **PR-AUC (average precision):** a reviewer works down the list from most to least suspicious. At each real anomaly they reach, ask "what share of what I've reviewed so far was real?" and average those answers. 1.0 is perfect, and random scores about the anomaly rate. **It picks the winner.**
- **95% CI:** the range the PR-AUC would plausibly fall in on another sample of similar months (1,000 bootstrap resamples). It's wide because there are only 25 test anomalies.
- **Precision@k:** of the top 5% of claims a reviewer can check, the share that were real anomalies.
- **Recall@k:** of all the real anomalies, the share that made it into that top 5%.
- **Recall@k by type:** the same, for each kind of anomaly.

## The candidates
1. **Robust z-score (rule baseline):** for each feature, how many "typical spreads" (median absolute deviation) the claim sits on the suspicious side of normal. The score is the largest of these.
2. **Isolation Forest:** random trees. Unusual claims get cut off from the rest in only a few splits.
3. **Local Outlier Factor (novelty=True):** is the claim in a much emptier neighbourhood than its nearest neighbours?

All three are **unsupervised**: they are fitted without labels. Hyperparameters were fixed before evaluation (D14).

## How scores are stored (D17)
After choosing the winner, it's refitted on all claims. A score is the **share of those claims that are at most as suspicious as this one, ties included**, so 0.97 means "at least as unusual as 97% of claims".
- The reference distribution ships inside `model.joblib`, so serving gives identical numbers.
- **The floor is about 0.27, not 0.** About 27% of ordinary claims tie at the lowest raw score (the baseline's "nothing unusual" value), and tied claims share a percentile.
- **Phase 4 thresholds must sit near the top**, for example 0.95 or more for "high".

## Who can do what (least privilege)
- **The trainer task** (`akinsds1126-expense-trainer-task-role`) has only two permissions:
  - read the `akinsds1126-expense-db-ml` secret
  - `s3:PutObject` under `models/anomaly/*`
- **No database writes** (D21). `--score-claims` is an **admin-only one-off backfill**. If it's ever needed again, it must use a dedicated narrow role, such as `app_scorer` with `UPDATE (anomaly_score)` only.
- **`app_ml`** can read claims, policy limits, categories, and claim labels (for evaluation only). From employees it can read only `employee_id` and `grade` (D22). Every session is read-only.

## Reproduce (cloud only)
```bash
# build and push (credential helper, no docker login; D20)
docker build --platform linux/amd64 --build-arg GIT_SHA=$(git rev-parse HEAD) \
  -t 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-trainer:$(git rev-parse HEAD) \
  -f docker/trainer.Dockerfile .
AWS_PROFILE=aero docker push 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-trainer:<sha>
# register a new task definition revision with that image (infra/ecs/trainer-task-definition.json, no command), then:
MSYS_NO_PATHCONV=1 aws ecs run-task --profile aero --region eu-west-2 \
  --cluster akinsds1126-expense-cluster --launch-type FARGATE \
  --task-definition akinsds1126-expense-trainer:2 --count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-084b8b27abab667a0,subnet-0fb022fdd3005325f,subnet-0175d22599862cd70],securityGroups=[sg-0858c78f998d4945d],assignPublicIp=ENABLED}"
```

## Limitations and next steps
- **Synthetic data:** real performance must be re-measured on real, labelled claims.
- **Small test set:** the PR-AUC intervals are wide.
- **Splits:** the first part is undetectable at submission time. A follow-up could **re-score earlier claims** when a later part arrives, for example a nightly rescoring job.
- **Cold start:** a new merchant looks unusual until it has history.
- **Limits and grades are read as they are today**, not as they were when the claim was submitted. That's fine for static synthetic data; real data would need effective-dated limits.
- **Hardening deferred:** pin the base image by digest; try `readonlyRootFilesystem: true`, after checking that joblib and scikit-learn don't need a writable temp directory.
- **The trained model is synthetic-specific.** Retrain via the ECS task whenever the data changes. Every version is kept in S3, so rollback is a pointer change.
