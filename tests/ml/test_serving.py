"""Model serving: S3 loading guard, scores, labels, reasons, point-in-time features."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from expense_agent.ml import serving  # noqa: E402
from expense_agent.ml.features import FEATURE_NAMES, build_features  # noqa: E402
from expense_agent.ml.models import AnomalyModel, RobustZScore  # noqa: E402
from expense_agent.ml.records import ClaimRecord  # noqa: E402

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def fitted_model() -> AnomalyModel:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, len(FEATURE_NAMES)))
    model = AnomalyModel(RobustZScore().fit(X), FEATURE_NAMES, "test-version")
    model.calibrate(X)
    return model


def rec(cid: int, amount: float, days: int, merchant: str = "Cafe", emp: int = 1) -> ClaimRecord:
    at = T0 + timedelta(days=days)
    return ClaimRecord(cid, emp, "senior", "meals", False, amount, merchant, at, at)


LIMITS = {("meals", "senior"): 50.0}


def test_normal_row_is_normal_and_extreme_row_is_high_with_reasons() -> None:
    model = fitted_model()
    normal = serving.score_row(model, [0.0] * len(FEATURE_NAMES))
    assert normal.label == "normal" and 0 <= normal.score < serving.HIGH_THRESHOLD
    assert normal.reasons == []
    row = [0.0] * len(FEATURE_NAMES)
    row[FEATURE_NAMES.index("dup_exact_10d")] = 25.0
    row[FEATURE_NAMES.index("hour")] = 99.0  # direction 0: never a reason
    high = serving.score_row(model, row)
    assert high.label == "high" and high.score >= 0.95
    assert high.reasons[0] == serving.REASON_PHRASES["dup_exact_10d"]
    assert len(high.reasons) <= 3
    assert high.to_dict()["model_version"] == "test-version"


def test_reasons_need_a_detector_with_median_and_scale() -> None:
    class Opaque:
        detector = object()

    assert serving.reasons_for(Opaque(), [0.0] * len(FEATURE_NAMES)) == []


def test_every_directional_feature_has_a_phrase() -> None:
    from expense_agent.ml.features import FEATURES

    assert {f.name for f in FEATURES if f.direction} <= set(serving.REASON_PHRASES)


def test_new_claim_features_match_build_features_and_ignore_the_future() -> None:
    history = [rec(1, 10.0, 0), rec(2, 12.0, 1), rec(3, 11.0, 2)]
    future = rec(4, 999.0, 30)
    now = T0 + timedelta(days=5)
    got = serving.features_for_new(
        [*history, future],
        LIMITS,
        employee_id=1,
        grade="senior",
        category_code="meals",
        is_travel=False,
        amount=10.0,
        merchant="Cafe",
        incurred_at=now,
        now=now,
    )
    new = ClaimRecord(-1, 1, "senior", "meals", False, 10.0, "Cafe", now, now)
    ids, rows = build_features([*history, new], LIMITS)
    assert got == rows[ids.index(-1)]
    assert (
        serving.features_for_existing(history, LIMITS, 2) == build_features(history, LIMITS)[1][1]
    )


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


def test_load_model_follows_latest_pointer() -> None:
    import joblib

    buf = io.BytesIO()
    joblib.dump(fitted_model(), buf)
    key = "models/anomaly/v1/model.joblib"
    s3 = FakeS3({serving.POINTER_KEY: json.dumps({"model_key": key}).encode(), key: buf.getvalue()})
    model = serving.ModelCache(s3, "bucket").get()
    assert model.version == "test-version"


@pytest.mark.parametrize(
    "key", ["other/model.joblib", "models/anomaly/../x/model.joblib", "models/anomaly/v1/evil.pkl"]
)
def test_pointer_outside_prefix_is_refused(key: str) -> None:
    s3 = FakeS3({serving.POINTER_KEY: json.dumps({"model_key": key}).encode()})
    with pytest.raises(serving.ModelLoadError):
        serving.load_model(s3, "bucket")
