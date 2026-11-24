"""Training job plumbing: version, time split, artefact layout and pointer order (no cloud)."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

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


# --------------------------------------------------------------------------- decision logic


def _data():  # type: ignore[no-untyped-def]
    import pytest

    pytest.importorskip("sklearn")
    from expense_agent.db.synthetic import generate
    from expense_agent.ml.records import from_dataset

    return from_dataset(generate(42))


_RESULT_CACHE: dict = {}


def _result():  # type: ignore[no-untyped-def]
    if "r" not in _RESULT_CACHE:
        records, limits, labels = _data()
        _RESULT_CACHE["r"] = (
            train.train_and_select(records, limits, labels, date(2026, 8, 1), "V1", "abc1234"),
            records,
            labels,
        )
    return _RESULT_CACHE["r"]


def test_winner_is_highest_pr_auc() -> None:
    result, _, _ = _result()
    best = max(result.results.values(), key=lambda m: m["average_precision"])
    assert result.results[result.winner] is best
    assert set(result.results) == {"robust_zscore", "isolation_forest", "local_outlier_factor"}
    assert result.pointer["winner"] == result.winner == result.meta["winner"]


def test_winner_refit_and_calibrated_on_all_claims() -> None:
    result, records, _ = _result()
    assert len(result.model.reference) == len(records) == len(result.claim_ids)
    assert result.meta["n_train"] + result.meta["n_test"] == result.meta["n_claims"]
    assert result.X.shape == (len(records), len(result.meta["features"]))


def test_artefact_set_is_complete() -> None:
    result, _, _ = _result()
    assert set(result.files) == {
        "model.joblib",
        "metrics.json",
        "comparison.md",
        "comparison.csv",
        "features.json",
        "model_card.md",
    }
    metrics = json.loads(result.files["metrics.json"][0])
    assert metrics["version"] == "V1" and metrics["git_sha"] == "abc1234"


def test_backfill_aligns_scores_with_claim_ids() -> None:
    result, _, _ = _result()
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    n = train.backfill_scores(conn, result)
    params = cur.executemany.call_args.args[1]
    assert n == len(params) == len(result.claim_ids)
    assert [cid for _, cid in params] == result.claim_ids
    raw = result.model.raw_scores(result.X[:3])
    assert [s for s, _ in params[:3]] == [result.model.normalise(float(r)) for r in raw]
    assert all(0.0 <= s <= 1.0 for s, _ in params)


@pytest.mark.parametrize("bad_start", [date(2025, 1, 1), date(2027, 1, 1)])
def test_split_outside_data_window_is_rejected(bad_start: date) -> None:
    records, limits, labels = _data()
    with pytest.raises(train.TrainingError, match="--test-start"):
        train.train_and_select(records, limits, labels, bad_start, "V")


def test_test_window_without_labels_is_rejected() -> None:
    records, limits, _ = _data()
    with pytest.raises(train.TrainingError, match="no labelled anomalies"):
        train.train_and_select(records, limits, {}, date(2026, 8, 1), "V")


def test_run_publishes_and_never_writes_db_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    import contextlib

    import boto3

    from expense_agent.db import connection
    from expense_agent.ml import records as rec_mod

    records, limits, labels = _data()
    roles: list[str] = []

    @contextlib.contextmanager
    def fake_connect(role, profile=None):  # type: ignore[no-untyped-def]
        roles.append(role)
        yield MagicMock()

    s3 = MagicMock()
    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(rec_mod, "load_claims", lambda conn: records)
    monkeypatch.setattr(rec_mod, "load_limits", lambda conn: limits)
    monkeypatch.setattr(rec_mod, "load_labels", lambda conn: labels)
    monkeypatch.setattr(boto3, "client", lambda name: s3)
    monkeypatch.setenv("EXPENSE_ARTIFACT_BUCKET", "bucket")
    monkeypatch.setenv("GIT_SHA", "deadbeef00")

    meta = train.run(
        argparse.Namespace(profile=None, test_start=date(2026, 8, 1), score_claims=False)
    )
    assert roles == ["ml"]  # read-only role only; no writer connection
    keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert keys[-1] == "models/anomaly/latest.json" and len(keys) == 7
    assert meta["version"].endswith("-deadbee")


def test_main_returns_2_on_training_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args):  # type: ignore[no-untyped-def]
        raise train.TrainingError("nope")

    monkeypatch.setattr(train, "run", boom)
    assert train.main(["--profile", ""]) == 2
