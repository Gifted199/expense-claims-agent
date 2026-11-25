"""Serve the anomaly model trained in Phase 2.

* The model is loaded from ``s3://<artifacts>/models/anomaly/latest.json`` (D19), whose
  ``model_key`` must stay under ``models/anomaly/``. It is cached for the life of the process.
* Features come from the **same** ``build_features`` used in training, over every claim submitted
  before the one being scored (point-in-time, D12), so training and serving can't drift.
* Reasons: the per-feature robust z-score against the training data, top 3 above 2, in words.
"""

from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from expense_agent.ml.features import FEATURE_DIRECTIONS, FEATURE_NAMES, build_features
from expense_agent.ml.records import ClaimRecord, Limits

DEFAULT_ARTIFACT_BUCKET = "akinsds1126-expense-artifacts"
MODEL_PREFIX = "models/anomaly/"
POINTER_KEY = f"{MODEL_PREFIX}latest.json"
HIGH_THRESHOLD = 0.95
REASON_MIN_Z = 2.0
NEW_CLAIM_ID = -1

REASON_PHRASES: dict[str, str] = {
    "log_amount_vs_emp_median": "much higher than this person's usual claim in this category",
    "amount_vs_limit": "large compared with the per-claim limit",
    "log_amount_vs_category_median": "much higher than typical claims in this category",
    "is_weekend": "spent at a weekend",
    "is_night": "spent late at night (23:00-05:00)",
    "non_travel_off_hours": "non-travel spending at a weekend or at night",
    "dup_exact_10d": "same merchant and amount already claimed within 10 days",
    "dup_near_10d": "nearly the same amount at the same merchant within 10 days",
    "split_sum_vs_limit": "with other claims at this merchant within a day, the total is high",
    "split_count": "several claims at the same merchant within a day",
    "merchant_seen_before": "a merchant nobody has claimed at before",
    "merchant_category_share": "an unusual category for this merchant",
}


class ModelLoadError(RuntimeError):
    pass


def artifact_bucket() -> str:
    return os.environ.get("EXPENSE_ARTIFACT_BUCKET", DEFAULT_ARTIFACT_BUCKET)


def checked_model_key(pointer: dict[str, Any]) -> str:
    key = str(pointer.get("model_key", ""))
    if not key.startswith(MODEL_PREFIX) or ".." in key or not key.endswith("/model.joblib"):
        raise ModelLoadError("latest.json points outside models/anomaly/")
    return key


def load_model(s3: Any, bucket: str | None = None) -> Any:
    """Read latest.json, then the model it points at. Only our own artefact bucket is trusted."""
    import joblib

    bucket = bucket or artifact_bucket()
    pointer = json.loads(s3.get_object(Bucket=bucket, Key=POINTER_KEY)["Body"].read())
    body = s3.get_object(Bucket=bucket, Key=checked_model_key(pointer))["Body"].read()
    model = joblib.load(io.BytesIO(body))
    if tuple(model.feature_names) != FEATURE_NAMES:
        raise ModelLoadError("model features do not match this code's FEATURE_NAMES")
    return model


@dataclass
class ModelCache:
    """Loads the model once per process (thread-safe)."""

    s3: Any
    bucket: str | None = None
    _model: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self) -> Any:
        with self._lock:
            if self._model is None:
                self._model = load_model(self.s3, self.bucket)
            return self._model


@dataclass(frozen=True)
class AnomalyResult:
    score: float
    label: str
    reasons: list[str]
    model_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "label": self.label,
            "reasons": self.reasons,
            "model_version": self.model_version,
        }


def reasons_for(model: Any, row: Sequence[float], top: int = 3) -> list[str]:
    """Plain-word reasons from per-feature robust z-scores (only for detectors that expose them,
    such as the shipped RobustZScore)."""
    det = model.detector
    median, scale = getattr(det, "median_", None), getattr(det, "scale_", None)
    if median is None or scale is None:
        return []
    zs = [
        ((x - m) / s) * d if s else 0.0
        for x, m, s, d in zip(row, median, scale, FEATURE_DIRECTIONS, strict=True)
    ]
    ranked = sorted(
        ((z, name) for z, name in zip(zs, FEATURE_NAMES, strict=True) if z > REASON_MIN_Z),
        reverse=True,
    )
    return [REASON_PHRASES[name] for _, name in ranked[:top] if name in REASON_PHRASES]


def score_row(model: Any, row: Sequence[float]) -> AnomalyResult:
    import numpy as np

    raw = float(model.raw_scores(np.asarray([row], dtype=float))[0])
    score = float(model.normalise(raw))
    label = "high" if score >= HIGH_THRESHOLD else "normal"
    return AnomalyResult(score, label, reasons_for(model, row), str(model.version))


def features_for_existing(records: Sequence[ClaimRecord], limits: Limits, claim_id: int) -> list:
    ids, rows = build_features(records, limits)
    return rows[ids.index(claim_id)]


def features_for_new(
    records: Sequence[ClaimRecord],
    limits: Limits,
    *,
    employee_id: int,
    grade: str,
    category_code: str,
    is_travel: bool,
    amount: float,
    merchant: str,
    incurred_at: datetime,
    now: datetime,
) -> list[float]:
    """Features for a claim that isn't in the database yet, as if submitted at ``now``."""
    new = ClaimRecord(
        claim_id=NEW_CLAIM_ID,
        employee_id=employee_id,
        grade=grade,
        category_code=category_code,
        is_travel=is_travel,
        amount=amount,
        merchant=merchant,
        incurred_at=incurred_at,
        submitted_at=now,
    )
    past = [r for r in records if r.submitted_at < now]
    ids, rows = build_features([*past, new], limits)
    return rows[ids.index(NEW_CLAIM_ID)]
