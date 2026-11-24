"""One-off training job (runs as an ECS Fargate task; never on the laptop).

    python -m expense_agent.ml.train [--score-claims] [--test-start 2026-08-01]

1. Load claims, limits (and, for evaluation only, labels) as the read-only ``app_ml`` role.
2. Build point-in-time features (D12) and split by submission time (D14).
3. Fit every candidate on the train months, evaluate on the test months.
4. Pick the winner by PR-AUC, refit it on all claims, calibrate scores to [0, 1] (D17).
5. Write artefacts to s3://<bucket>/models/anomaly/<version>/, then latest.json LAST (D19).
6. Optionally (--score-claims) write claims.anomaly_score as ``app_writer``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

ARTIFACT_PREFIX = "models/anomaly"
DEFAULT_TEST_START = date(2026, 8, 1)  # train: first 9 months, test: last 3 (Aug-Oct 2026)
LONDON = ZoneInfo("Europe/London")


def make_version(now: datetime, git_sha: str) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{(git_sha or 'unknown')[:7]}"


def time_split(
    claim_ids: Sequence[int], submitted: dict[int, datetime], test_start: date
) -> tuple[list[int], list[int]]:
    """Indices of train rows (submitted before test_start, London time) and test rows."""
    cut = datetime(test_start.year, test_start.month, test_start.day, tzinfo=LONDON)
    train = [i for i, cid in enumerate(claim_ids) if submitted[cid] < cut]
    test = [i for i, cid in enumerate(claim_ids) if submitted[cid] >= cut]
    return train, test


def comparison_rows(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for name, m in sorted(results.items(), key=lambda kv: -kv[1]["average_precision"]):
        lo, hi = m["average_precision_ci95"]
        row = {
            "model": name,
            "pr_auc": round(m["average_precision"], 3),
            "pr_auc_ci95": f"{lo:.3f}-{hi:.3f}",
            "precision_at_k": round(m["precision_at_k"], 3),
            "recall_at_k": round(m["recall_at_k"], 3),
        }
        row.update({f"recall@k_{t}": round(v, 2) for t, v in m["recall_at_k_by_type"].items()})
        rows.append(row)
    return rows


def comparison_markdown(rows: list[dict[str, Any]], k: int, n: int, anomalies: int) -> str:
    header = list(rows[0])
    lines = [
        f"Test set: {n} claims, {anomalies} labelled anomalies; review budget k = {k} (5%).",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    lines += ["| " + " | ".join(str(r.get(h, "")) for h in header) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def comparison_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def model_card(meta: dict[str, Any], table_md: str) -> str:
    return f"""# Model card: expense claim anomaly detector

- **Version:** {meta["version"]} (git {meta["git_sha"]})
- **Winner:** `{meta["winner"]}`, chosen by PR-AUC on held-out months
- **Purpose:** rank submitted expense claims by how unusual they are, so a human reviews the
  most suspicious first. **Not for automatic rejection.** Over-limit or anomalous claims always
  go to a manager (`pending_manager`), and that decision is made by code and a person, never by
  this model.
- **Data:** synthetic claims only ({meta["n_claims"]} claims, {meta["window"]}).
- **Features:** {len(meta["features"])} point-in-time features (see features.json). They use
  only the claim and the employee's earlier claims. Status, approvals and labels are never
  used (D11, D12).
- **Training:** unsupervised (no labels). Fixed hyperparameters (D14).
  Evaluated on claims submitted from {meta["test_start"]}, after training on earlier months, then
  refitted on all claims.
- **Scores:** 0-1 = the share of reference claims this one is more suspicious than (D17).

## Evaluation (held-out months)
{table_md}
## Limitations
- The test set is small (few anomalies), so the PR-AUC intervals are wide.
- The first part of a split claim looks normal: only later parts reveal the split.
- New merchants look unusual until they build history (cold start).
- Synthetic data: real-world performance must be re-measured on real, labelled data.
"""


def run(args: argparse.Namespace) -> dict[str, Any]:  # pragma: no cover - cloud entrypoint
    import boto3
    import joblib
    import numpy as np

    from expense_agent.db.connection import connect
    from expense_agent.ml.features import FEATURE_NAMES, FEATURES, build_features
    from expense_agent.ml.metrics import evaluate
    from expense_agent.ml.models import AnomalyModel, candidates
    from expense_agent.ml.records import load_claims, load_labels, load_limits

    bucket = os.environ["EXPENSE_ARTIFACT_BUCKET"]
    git_sha = os.environ.get("GIT_SHA", "unknown")
    version = make_version(datetime.now(UTC), git_sha)
    print(f"version {version}", flush=True)

    with connect("ml", args.profile) as conn:
        records = load_claims(conn)
        limits = load_limits(conn)
        labels = load_labels(conn)  # evaluation only, never a feature
    print(f"loaded {len(records)} claims, {len(labels)} eval labels", flush=True)

    claim_ids, rows = build_features(records, limits)
    X = np.asarray(rows, dtype=float)
    submitted = {r.claim_id: r.submitted_at for r in records}
    train_idx, test_idx = time_split(claim_ids, submitted, args.test_start)
    test_ids = [claim_ids[i] for i in test_idx]
    print(f"train {len(train_idx)} / test {len(test_idx)} claims", flush=True)

    results: dict[str, dict[str, Any]] = {}
    for cand in candidates():
        cand.fit(X[train_idx])
        results[cand.name] = evaluate(cand.score(X[test_idx]).tolist(), labels, test_ids)
        m = results[cand.name]
        print(
            f"{cand.name}: PR-AUC {m['average_precision']:.3f} "
            f"P@k {m['precision_at_k']:.2f} R@k {m['recall_at_k']:.2f}",
            flush=True,
        )
    winner = max(results, key=lambda name: results[name]["average_precision"])
    print(f"winner: {winner}", flush=True)

    detector = next(c for c in candidates() if c.name == winner).fit(X)
    model = AnomalyModel(detector=detector, feature_names=FEATURE_NAMES, version=version)
    model.calibrate(X)

    any_result = next(iter(results.values()))
    table_rows = comparison_rows(results)
    table_md = comparison_markdown(
        table_rows, any_result["k"], any_result["n"], any_result["anomalies"]
    )
    meta = {
        "version": version,
        "git_sha": git_sha,
        "winner": winner,
        "selection_metric": "average_precision",
        "test_start": args.test_start.isoformat(),
        "n_claims": len(records),
        "n_train": len(train_idx),
        "n_test": len(test_idx),
        "window": f"{min(submitted.values()):%Y-%m-%d} to {max(submitted.values()):%Y-%m-%d}",
        "features": list(FEATURE_NAMES),
        "created_at": datetime.now(UTC).isoformat(),
    }
    model_buf = io.BytesIO()
    joblib.dump(model, model_buf)
    files: dict[str, tuple[bytes, str]] = {
        "model.joblib": (model_buf.getvalue(), "application/octet-stream"),
        "metrics.json": (
            json.dumps({**meta, "results": results}, indent=2).encode(),
            "application/json",
        ),
        "comparison.md": (table_md.encode(), "text/markdown"),
        "comparison.csv": (comparison_csv(table_rows).encode(), "text/csv"),
        "features.json": (
            json.dumps(
                [{"name": f.name, "description": f.description} for f in FEATURES], indent=2
            ).encode(),
            "application/json",
        ),
        "model_card.md": (model_card(meta, table_md).encode(), "text/markdown"),
    }
    pointer = {
        "version": version,
        "prefix": f"{ARTIFACT_PREFIX}/{version}/",
        "model_key": f"{ARTIFACT_PREFIX}/{version}/model.joblib",
        "winner": winner,
        "git_sha": git_sha,
        "created_at": meta["created_at"],
        "average_precision": results[winner]["average_precision"],
    }
    publish(boto3.client("s3"), bucket, version, files, pointer)
    print(f"published s3://{bucket}/{ARTIFACT_PREFIX}/{version}/ and latest.json", flush=True)

    if args.score_claims:
        scores = model.scores_0_1(X)
        with connect("writer", args.profile) as wconn, wconn.cursor() as cur:
            cur.executemany(
                "UPDATE claims SET anomaly_score = %s WHERE claim_id = %s",
                list(zip(scores, claim_ids, strict=True)),
            )
        print(f"wrote anomaly_score for {len(scores)} claims", flush=True)
    return meta


def publish(
    s3: Any, bucket: str, version: str, files: dict[str, tuple[bytes, str]], pointer: dict
) -> list[str]:
    """Upload every artefact under the version prefix, then the latest.json pointer LAST (D19)."""
    keys = []
    for name, (body, content_type) in files.items():
        key = f"{ARTIFACT_PREFIX}/{version}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        keys.append(key)
    latest = f"{ARTIFACT_PREFIX}/latest.json"
    s3.put_object(
        Bucket=bucket,
        Key=latest,
        Body=json.dumps(pointer, indent=2).encode(),
        ContentType="application/json",
    )
    keys.append(latest)
    return keys


def main(argv: Sequence[str] | None = None) -> int:
    from expense_agent.db.connection import add_profile_argument

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument("--score-claims", action="store_true", help="write claims.anomaly_score")
    parser.add_argument("--test-start", type=date.fromisoformat, default=DEFAULT_TEST_START)
    args = parser.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
