"""Training job plumbing: version, time split, artefact layout and pointer order (no cloud)."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

from expense_agent.db.synthetic import LONDON
from expense_agent.ml import train


def test_version_is_utc_timestamp_plus_short_sha() -> None:
    v = train.make_version(datetime(2026, 11, 24, 15, 4, 5, tzinfo=UTC), "abcdef1234567")
    assert v == "20261124T150405Z-abcdef1"
    assert re.fullmatch(r"\d{8}T\d{6}Z-unknown", train.make_version(datetime.now(UTC), ""))


def test_time_split_has_no_overlap_and_respects_boundary() -> None:
    start = datetime(2026, 7, 31, 23, 59, tzinfo=LONDON)
    submitted = {i: start + timedelta(minutes=i) for i in range(5)}  # 23:59, 00:00, ...
    train_idx, test_idx = train.time_split(list(range(5)), submitted, date(2026, 8, 1))
    assert train_idx == [0] and test_idx == [1, 2, 3, 4]
    assert not set(train_idx) & set(test_idx)


def test_publish_writes_everything_under_version_then_latest_last() -> None:
    s3 = MagicMock()
    files = {"model.joblib": (b"m", "application/octet-stream"), "metrics.json": (b"{}", "x")}
    keys = train.publish(s3, "bucket", "V1", files, {"version": "V1"})
    written = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert written == keys
    assert written[-1] == "models/anomaly/latest.json"
    assert all(k.startswith("models/anomaly/V1/") for k in written[:-1])
    pointer = json.loads(s3.put_object.call_args_list[-1].kwargs["Body"])
    assert pointer == {"version": "V1"}
    assert {c.kwargs["Bucket"] for c in s3.put_object.call_args_list} == {"bucket"}


def test_comparison_table_sorted_by_pr_auc() -> None:
    def result(ap: float) -> dict:
        return {
            "average_precision": ap,
            "average_precision_ci95": [ap - 0.1, ap + 0.1],
            "precision_at_k": 0.5,
            "recall_at_k": 0.4,
            "recall_at_k_by_type": {"odd_time": 1.0},
            "k": 10,
            "n": 200,
            "anomalies": 8,
        }

    rows = train.comparison_rows({"a": result(0.3), "b": result(0.6)})
    assert [r["model"] for r in rows] == ["b", "a"]
    md = train.comparison_markdown(rows, 10, 200, 8)
    assert "| b | 0.6 |" in md and "recall@k_odd_time" in md
    assert train.comparison_csv(rows).splitlines()[0].startswith("model,pr_auc")


def test_model_card_states_human_in_the_loop() -> None:
    meta = {
        "version": "V1",
        "git_sha": "abc",
        "winner": "isolation_forest",
        "n_claims": 10,
        "window": "x",
        "features": ["a"],
        "test_start": "2026-08-01",
    }
    card = train.model_card(meta, "table")
    assert "Not for automatic rejection" in card and "pending_manager" in card
