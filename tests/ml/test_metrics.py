"""Ranking metrics against hand-worked examples (and scikit-learn where available)."""

from __future__ import annotations

import random

import pytest

from expense_agent.ml import metrics as M


def test_average_precision_perfect_and_worst() -> None:
    assert M.average_precision([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0
    # positives ranked last: precision 1/3 at the first hit, 2/4 at the second
    assert M.average_precision([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == pytest.approx(
        0.5 * (1 / 3) + 0.5 * (2 / 4)
    )


def test_average_precision_hand_worked() -> None:
    # ranking: 1, 0, 1, 0 -> AP = 0.5*1 + 0.5*(2/3)
    assert M.average_precision([1, 0, 1, 0], [4, 3, 2, 1]) == pytest.approx(0.5 + 1 / 3)


def test_average_precision_no_positives_is_zero() -> None:
    assert M.average_precision([0, 0], [1, 2]) == 0.0


def test_matches_sklearn_including_ties() -> None:
    sk = pytest.importorskip("sklearn.metrics")
    rng = random.Random(1)  # noqa: S311
    for _ in range(20):
        y = [1 if rng.random() < 0.1 else 0 for _ in range(200)]
        y[0] = 1
        s = [round(rng.random(), 1) for _ in range(200)]  # rounding creates many ties
        assert M.average_precision(y, s) == pytest.approx(sk.average_precision_score(y, s))


def test_budget_k_rounds_up_and_is_at_least_one() -> None:
    assert M.budget_k(700) == 35
    assert M.budget_k(701) == 36
    assert M.budget_k(3) == 1


def test_precision_recall_at_k() -> None:
    y = [1, 0, 1, 0, 0, 1]
    s = [0.9, 0.8, 0.7, 0.1, 0.2, 0.05]
    assert M.precision_recall_at_k(y, s, 3) == pytest.approx((2 / 3, 2 / 3))


def test_top_k_ties_break_by_position() -> None:
    assert M.top_k([1.0, 2.0, 2.0, 0.5], 2) == [1, 2]


def test_recall_by_type() -> None:
    types = ["dup", None, "split", "split", None]
    scores = [0.9, 0.8, 0.7, 0.1, 0.2]
    assert M.recall_at_k_by_type(types, scores, 3) == {"dup": 1.0, "split": 0.5}


def test_bootstrap_interval_contains_point_estimate() -> None:
    rng = random.Random(3)  # noqa: S311
    y = [1 if i % 20 == 0 else 0 for i in range(400)]
    s = [yi * 0.5 + rng.random() for yi in y]
    lo, hi = M.bootstrap_ap_interval(y, s, n_boot=200)
    assert 0 <= lo <= M.average_precision(y, s) <= hi <= 1


def test_evaluate_reports_everything() -> None:
    out = M.evaluate([0.9, 0.1, 0.8, 0.2], {10: "dup"}, [10, 11, 12, 13])
    assert out["n"] == 4 and out["anomalies"] == 1 and out["k"] == 1
    assert out["average_precision"] == 1.0
    assert out["recall_at_k_by_type"] == {"dup": 1.0}


def test_bootstrap_handles_no_rows_or_no_anomalies() -> None:
    assert M.bootstrap_ap_interval([], []) == (0.0, 0.0)
    assert M.bootstrap_ap_interval([0, 0, 0], [0.1, 0.2, 0.3]) == (0.0, 0.0)


def test_evaluate_rejects_empty_test_set() -> None:
    with pytest.raises(ValueError, match="empty test set"):
        M.evaluate([], {}, [])
