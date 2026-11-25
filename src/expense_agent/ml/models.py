"""Candidate anomaly detectors behind one interface: ``fit(X)`` then ``score(X)``, where a
higher score means more suspicious. All are unsupervised: labels are never passed in.
Hyperparameters are fixed up front, not tuned on the test set (D14).
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import RobustScaler

from expense_agent.ml.features import FEATURE_DIRECTIONS

SEED = 42


class Candidate(Protocol):
    name: str

    def fit(self, X: np.ndarray) -> Candidate: ...

    def score(self, X: np.ndarray) -> np.ndarray: ...


class RobustZScore:
    """Rule baseline: for each feature, how many typical spreads (MAD) it sits on the suspicious
    side of the training median; the claim's score is the largest of these."""

    name = "robust_zscore"

    def __init__(self, directions: Sequence[int] = FEATURE_DIRECTIONS) -> None:
        self.directions = np.asarray(directions, dtype=float)

    def fit(self, X: np.ndarray) -> RobustZScore:
        self.median_ = np.median(X, axis=0)
        mad = 1.4826 * np.median(np.abs(X - self.median_), axis=0)
        std = X.std(axis=0)
        # Binary or mostly-constant features have MAD 0: fall back to std, then to 1.
        self.scale_ = np.where(mad > 0, mad, np.where(std > 0, std, 1.0))
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        z = (X - self.median_) / self.scale_ * self.directions  # direction 0 drops a feature
        return np.asarray(z.max(axis=1))


class IsolationForestModel:
    name = "isolation_forest"

    def __init__(self) -> None:
        self.model = IsolationForest(
            n_estimators=300, max_samples="auto", contamination="auto", random_state=SEED
        )

    def fit(self, X: np.ndarray) -> IsolationForestModel:
        self.model.fit(X)
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(-self.model.score_samples(X))


class LocalOutlierFactorModel:
    name = "local_outlier_factor"

    def __init__(self) -> None:
        self.model: Pipeline = make_pipeline(
            RobustScaler(), LocalOutlierFactor(n_neighbors=35, novelty=True)
        )

    def fit(self, X: np.ndarray) -> LocalOutlierFactorModel:
        self.model.fit(X)
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(-self.model.score_samples(X))


def candidates() -> list[Candidate]:
    return [RobustZScore(), IsolationForestModel(), LocalOutlierFactorModel()]


@dataclass
class AnomalyModel:
    """What ships to S3: the winning detector plus everything needed to serve it identically.

    ``reference`` holds sorted raw scores from the data it was fitted on; ``normalise`` turns a raw
    score into the share of those claims that are at most as suspicious, ties included, so a
    value in [0, 1] (D17). Many ordinary claims tie at the lowest raw score, so the floor is > 0.
    """

    detector: Any
    feature_names: tuple[str, ...]
    version: str
    reference: list[float] = field(default_factory=list)

    def calibrate(self, X: np.ndarray) -> None:
        self.reference = sorted(float(s) for s in self.detector.score(X))

    def raw_scores(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(self.detector.score(X))

    def normalise(self, raw: float) -> float:
        if not self.reference:
            raise ValueError("model is not calibrated")
        return bisect.bisect_right(self.reference, raw) / len(self.reference)

    def scores_0_1(self, X: np.ndarray) -> list[float]:
        return [self.normalise(float(s)) for s in self.raw_scores(X)]
