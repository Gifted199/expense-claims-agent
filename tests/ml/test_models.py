"""Candidate models on synthetic data (skipped where scikit-learn can't load)."""

from __future__ import annotations

import pickle

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from expense_agent.db.synthetic import generate  # noqa: E402
from expense_agent.ml import models  # noqa: E402
from expense_agent.ml.features import FEATURE_NAMES, build_features  # noqa: E402
from expense_agent.ml.metrics import average_precision  # noqa: E402
from expense_agent.ml.records import from_dataset  # noqa: E402
from expense_agent.ml.train import DEFAULT_TEST_START, time_split  # noqa: E402


@pytest.fixture(scope="module")
def split() -> dict:
    records, limits, labels = from_dataset(generate(42))
    ids, rows = build_features(records, limits)
    X = np.asarray(rows)
    submitted = {r.claim_id: r.submitted_at for r in records}
    train, test = time_split(ids, submitted, DEFAULT_TEST_START)
    y = [1 if ids[i] in labels else 0 for i in test]
    return {"X": X, "train": train, "test": test, "y": y}


@pytest.mark.parametrize("candidate", models.candidates(), ids=lambda c: c.name)
def test_candidate_scores_shape_and_beats_random(candidate, split) -> None:  # type: ignore[no-untyped-def]
    X, train, test, y = split["X"], split["train"], split["test"], split["y"]
    scores = candidate.fit(X[train]).score(X[test])
    assert scores.shape == (len(test),)
    assert np.isfinite(scores).all()
    prevalence = sum(y) / len(y)
    assert average_precision(y, scores.tolist()) > 2 * prevalence


def test_scores_are_deterministic(split) -> None:  # type: ignore[no-untyped-def]
    X, train = split["X"], split["train"]
    a = models.IsolationForestModel().fit(X[train]).score(X[:50])
    b = models.IsolationForestModel().fit(X[train]).score(X[:50])
    assert np.array_equal(a, b)


def test_robust_zscore_respects_directions() -> None:
    X = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [1.0, 1.0]])
    m = models.RobustZScore(directions=[+1, -1]).fit(X)
    high_first, low_second = m.score(np.array([[5.0, 1.0], [1.0, -5.0]]))
    assert high_first > 0 and low_second > 0
    assert m.score(np.array([[-5.0, 5.0]]))[0] < 0  # unsuspicious on both


def test_anomaly_model_normalises_to_0_1_and_pickles(split) -> None:  # type: ignore[no-untyped-def]
    X = split["X"]
    model = models.AnomalyModel(
        detector=models.IsolationForestModel().fit(X), feature_names=FEATURE_NAMES, version="v"
    )
    model.calibrate(X)
    scores = model.scores_0_1(X[:100])
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert max(model.scores_0_1(X)) == 1.0
    clone = pickle.loads(pickle.dumps(model))  # noqa: S301 - our own object, in-memory
    assert clone.scores_0_1(X[:5]) == model.scores_0_1(X[:5])


def test_uncalibrated_model_refuses_to_normalise() -> None:
    model = models.AnomalyModel(detector=None, feature_names=FEATURE_NAMES, version="v")
    with pytest.raises(ValueError, match="calibrated"):
        model.normalise(0.5)
