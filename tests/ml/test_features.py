"""Point-in-time features: no peeking into the future, and each signal fires when it should."""

from __future__ import annotations

import ast
import dataclasses
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from expense_agent.db.synthetic import LONDON, generate
from expense_agent.ml import features as F
from expense_agent.ml.records import ClaimRecord, from_dataset

LIMITS = {("meals", "associate"): 30.0, ("training", "associate"): 750.0}
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=LONDON)  # a Monday


def rec(cid: int, *, amount: float = 15.0, merchant: str = "Cafe", category: str = "meals",
        day: int = 0, sub_hours: float | None = None, hour: int = 12, emp: int = 1,
        travel: bool = False) -> ClaimRecord:  # fmt: skip
    incurred = T0.replace(hour=hour) + timedelta(days=day)
    submitted = incurred + timedelta(hours=sub_hours if sub_hours is not None else 1 + cid / 100)
    return ClaimRecord(
        cid, emp, "associate", category, travel, amount, merchant, incurred, submitted
    )


def feats(records: list[ClaimRecord]) -> dict[int, dict[str, float]]:
    ids, rows = F.build_features(records, LIMITS)
    return {
        cid: dict(zip(F.FEATURE_NAMES, row, strict=True))
        for cid, row in zip(ids, rows, strict=True)
    }


@pytest.fixture(scope="module")
def synthetic() -> tuple[list[ClaimRecord], dict, dict]:
    return from_dataset(generate(42))


# --------------------------------------------------------------------------- no future leakage


def test_features_equal_when_future_claims_are_removed(synthetic: tuple) -> None:
    """For sampled claims: features from the full data == features from data up to that claim."""
    records, limits, _ = synthetic
    full_ids, full_rows = F.build_features(records, limits)
    full = dict(zip(full_ids, full_rows, strict=True))
    for claim in random.Random(0).sample(records, 40):  # noqa: S311
        past_and_now = [r for r in records if r.submitted_at <= claim.submitted_at]
        ids, rows = F.build_features(past_and_now, limits)
        assert dict(zip(ids, rows, strict=True))[claim.claim_id] == full[claim.claim_id]


def test_appending_future_claims_changes_no_past_feature(synthetic: tuple) -> None:
    records, limits, _ = synthetic
    cut = len(records) * 2 // 3
    ids_a, rows_a = F.build_features(records[:cut], limits)
    ids_b, rows_b = F.build_features(records, limits)
    assert ids_b[:cut] == ids_a
    assert rows_b[:cut] == rows_a


def test_claims_with_identical_submission_time_cannot_see_each_other() -> None:
    a = rec(1, amount=15.0, sub_hours=2)
    b = dataclasses.replace(a, claim_id=2)  # exact duplicate submitted at the same instant
    f = feats([a, b])
    assert f[1]["dup_exact_10d"] == 0 and f[2]["dup_exact_10d"] == 0


def test_record_has_no_status_approval_or_label_fields() -> None:
    names = {f.name for f in dataclasses.fields(ClaimRecord)}
    assert not names & {"status", "approval", "approvals", "decision", "label", "anomaly_type",
                        "anomaly_score"}  # fmt: skip


def _code_without_docstrings(path: Path) -> str:
    """Source with docstrings and comments removed; string literals such as SQL stay."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]  # type: ignore[attr-defined]
    return ast.unparse(tree)


@pytest.mark.parametrize("filename", ["features.py", "models.py"])
def test_feature_and_model_code_never_touch_forbidden_data(filename: str) -> None:
    code = _code_without_docstrings(Path(F.__file__).with_name(filename))
    for forbidden in ("claim_labels", "approvals", "status", "load_labels", "anomaly_type"):
        assert forbidden not in code, forbidden


# --------------------------------------------------------------------------- each signal


def test_exact_duplicate_flags_the_copy_not_the_original() -> None:
    original = rec(1, amount=17.5, day=0)
    copy = rec(2, amount=17.5, day=0, sub_hours=24 * 5)  # resubmitted 5 days later
    f = feats([original, copy])
    assert f[1]["dup_exact_10d"] == 0  # the copy is in the original's future
    assert f[2]["dup_exact_10d"] == 1
    assert f[2]["dup_near_10d"] == 0


def test_near_duplicate_within_two_pounds() -> None:
    f = feats([rec(1, amount=17.50), rec(2, amount=18.90, day=1, sub_hours=48)])
    assert f[2]["dup_near_10d"] == 1 and f[2]["dup_exact_10d"] == 0


def test_same_merchant_outside_ten_days_is_not_a_duplicate() -> None:
    f = feats([rec(1, amount=17.5), rec(2, amount=17.5, day=11)])
    assert f[2]["dup_exact_10d"] == 0


def test_split_claim_second_part_pushes_sum_over_limit() -> None:
    first = rec(1, amount=27.0, merchant="Bistro")
    second = rec(2, amount=28.0, merchant="Bistro", hour=13)
    f = feats([first, second])
    assert f[1]["split_sum_vs_limit"] == pytest.approx(27 / 30)
    assert f[2]["split_sum_vs_limit"] == pytest.approx(55 / 30)
    assert f[2]["split_count"] == 2


def test_split_ignores_other_categories_and_far_days() -> None:
    a = rec(1, amount=27.0, merchant="Bistro")
    other_cat = rec(2, amount=500.0, merchant="Bistro", category="training")
    far = rec(3, amount=28.0, merchant="Bistro", day=3)
    f = feats([a, other_cat, far])
    assert f[3]["split_count"] == 1


def test_amount_vs_personal_median_needs_three_past_claims() -> None:
    history = [rec(i, amount=10.0, day=i) for i in range(1, 4)]
    big = rec(9, amount=80.0, day=5)
    f = feats([*history, big])
    assert f[9]["emp_category_history_n"] == 3
    assert f[9]["log_amount_vs_emp_median"] == pytest.approx(2.0794, abs=1e-3)  # log(8)
    assert f[9]["amount_vs_limit"] == pytest.approx(80 / 30)


def test_cold_start_uses_category_median_then_zero() -> None:
    other_person = rec(1, amount=20.0, emp=2)
    newcomer = rec(2, amount=40.0, emp=1, day=1)
    f = feats([other_person, newcomer])
    assert f[1]["log_amount_vs_emp_median"] == 0.0  # no history at all
    assert f[2]["log_amount_vs_emp_median"] == pytest.approx(0.6931, abs=1e-3)  # log(2) vs cat


def test_unseen_merchant_and_category_share() -> None:
    a = rec(1, merchant="Bistro", category="meals")
    b = rec(2, merchant="Bistro", category="meals", day=1)
    odd = rec(3, merchant="Bistro", category="training", amount=200.0, day=2)
    spa = rec(4, merchant="Glow Day Spa", category="training", amount=200.0, day=3)
    f = feats([a, b, odd, spa])
    assert f[1]["merchant_seen_before"] == 0
    assert f[2]["merchant_category_share"] == 1.0
    assert f[3]["merchant_category_share"] == 0.0 and f[3]["merchant_seen_before"] == 1
    assert f[4]["merchant_seen_before"] == 0


def test_time_flags_and_travel_exception() -> None:
    saturday = rec(1, day=5)  # 2026-03-07
    late_lunch = rec(2, hour=23)
    late_taxi = rec(3, hour=23, category="meals", travel=True)
    f = feats([saturday, late_lunch, late_taxi])
    assert f[1]["is_weekend"] == 1 and f[1]["non_travel_off_hours"] == 1
    assert f[2]["is_night"] == 1 and f[2]["non_travel_off_hours"] == 1
    assert f[3]["is_night"] == 1 and f[3]["non_travel_off_hours"] == 0


def test_feature_metadata_is_complete() -> None:
    assert len(F.FEATURE_NAMES) == len(set(F.FEATURE_NAMES)) == len(F.FEATURE_DIRECTIONS)
    assert all(f.description for f in F.FEATURES)
    _, rows = F.build_features([rec(1)], LIMITS)
    assert len(rows[0]) == len(F.FEATURE_NAMES)
