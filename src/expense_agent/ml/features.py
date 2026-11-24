"""Point-in-time features for claim anomaly scoring (D12).

A claim's features use only the claim itself plus claims submitted **strictly before** it,
which is exactly what the system knows at the moment the claim is submitted. Claims are processed
in submission order; history is updated only after every claim with the same timestamp has
been featurised, so nothing from the present or future can leak in.

Never used as features (D11): claim status, approvals and anomaly labels.
Pure Python, with no numpy, so the rules are easy to test and to read.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from expense_agent.ml.records import ClaimRecord, Limits

LONDON = ZoneInfo("Europe/London")
MIN_PERSONAL_HISTORY = 3
DUPLICATE_WINDOW = timedelta(days=10)
NEAR_DUPLICATE_GBP = 2.0
SPLIT_WINDOW_DAYS = 1


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    description: str
    direction: int  # +1: higher is more suspicious, -1: lower is more suspicious, 0: neither


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "log_amount_vs_emp_median",
        "How much bigger this claim is than this person's own past typical claim in the "
        "category (log ratio). Falls back to the category's past median below 3 past claims.",
        +1,
    ),
    FeatureSpec(
        "emp_category_history_n",
        "How many past claims this person has in this category (how much to trust the ratio).",
        0,
    ),
    FeatureSpec(
        "amount_vs_limit", "Amount divided by the per-claim limit for their grade and category.", +1
    ),
    FeatureSpec(
        "log_amount_vs_category_median",
        "Amount compared with everyone's past claims in this category (log ratio).",
        +1,
    ),
    FeatureSpec("hour", "Local hour of day the money was spent (0-23).", 0),
    FeatureSpec("is_weekend", "1 if spent on a Saturday or Sunday.", +1),
    FeatureSpec("is_night", "1 if spent between 23:00 and 05:00.", +1),
    FeatureSpec(
        "non_travel_off_hours",
        "1 if a non-travel category (meals, office, training, entertainment) was spent at a "
        "weekend or at night. Legitimate travel (late taxis, Sunday hotels) is excluded.",
        +1,
    ),
    FeatureSpec(
        "dup_exact_10d",
        "Earlier claims by this person at the same merchant for the same amount, spent within "
        "10 days.",
        +1,
    ),
    FeatureSpec(
        "dup_near_10d",
        "Earlier claims by this person at the same merchant within GBP 2 of this amount (but not "
        "equal), spent within 10 days.",
        +1,
    ),
    FeatureSpec(
        "split_sum_vs_limit",
        "This claim plus the person's earlier claims at the same merchant and category spent "
        "within a day of it, divided by the per-claim limit.",
        +1,
    ),
    FeatureSpec(
        "split_count", "How many claims are in that same-merchant, same-day group (incl. this).", +1
    ),
    FeatureSpec("merchant_seen_before", "1 if anyone has claimed at this merchant before.", -1),
    FeatureSpec(
        "merchant_category_share",
        "Of past claims at this merchant (anyone), the share filed under this claim's category. "
        "0 if the merchant is new.",
        -1,
    ),
)
FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)
FEATURE_DIRECTIONS: tuple[int, ...] = tuple(f.direction for f in FEATURES)


def _median(sorted_values: Sequence[float]) -> float:
    n = len(sorted_values)
    mid = n // 2
    return sorted_values[mid] if n % 2 else (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _log_ratio(amount: float, reference: float) -> float:
    return math.log(amount / reference) if amount > 0 and reference > 0 else 0.0


class _History:
    """Everything known about claims submitted so far. Only ever grows."""

    def __init__(self) -> None:
        self.emp_cat_amounts: dict[tuple[int, str], list[float]] = defaultdict(list)  # sorted
        self.cat_amounts: dict[str, list[float]] = defaultdict(list)  # sorted
        self.emp_merchant: dict[tuple[int, str], list[ClaimRecord]] = defaultdict(list)
        self.merchant_categories: dict[str, Counter[str]] = defaultdict(Counter)

    def add(self, c: ClaimRecord) -> None:
        bisect.insort(self.emp_cat_amounts[(c.employee_id, c.category_code)], c.amount)
        bisect.insort(self.cat_amounts[c.category_code], c.amount)
        self.emp_merchant[(c.employee_id, c.merchant)].append(c)
        self.merchant_categories[c.merchant][c.category_code] += 1


def _features_for(c: ClaimRecord, h: _History, limits: Limits) -> list[float]:
    limit = limits[(c.category_code, c.grade)]
    personal = h.emp_cat_amounts.get((c.employee_id, c.category_code), [])
    category = h.cat_amounts.get(c.category_code, [])
    cat_median = _median(category) if category else 0.0
    if len(personal) >= MIN_PERSONAL_HISTORY:
        vs_emp = _log_ratio(c.amount, _median(personal))
    else:
        vs_emp = _log_ratio(c.amount, cat_median)

    local = c.incurred_at.astimezone(LONDON)
    is_weekend = local.weekday() >= 5
    is_night = local.hour >= 23 or local.hour < 5

    dup_exact = dup_near = 0
    split_total, split_n = c.amount, 1
    for p in h.emp_merchant.get((c.employee_id, c.merchant), []):
        if abs(p.incurred_at - c.incurred_at) <= DUPLICATE_WINDOW:
            diff = abs(p.amount - c.amount)
            if diff < 0.005:
                dup_exact += 1
            elif diff <= NEAR_DUPLICATE_GBP:
                dup_near += 1
        days_apart = abs((p.incurred_at.astimezone(LONDON).date() - local.date()).days)
        if p.category_code == c.category_code and days_apart <= SPLIT_WINDOW_DAYS:
            split_total += p.amount
            split_n += 1

    seen = h.merchant_categories.get(c.merchant)
    total_seen = sum(seen.values()) if seen else 0
    share = seen[c.category_code] / total_seen if seen and total_seen else 0.0

    return [
        vs_emp,
        float(len(personal)),
        c.amount / limit,
        _log_ratio(c.amount, cat_median),
        float(local.hour),
        float(is_weekend),
        float(is_night),
        float((not c.is_travel) and (is_weekend or is_night)),
        float(dup_exact),
        float(dup_near),
        split_total / limit,
        float(split_n),
        float(total_seen > 0),
        share,
    ]


def build_features(
    records: Iterable[ClaimRecord], limits: Limits
) -> tuple[list[int], list[list[float]]]:
    """Return (claim_ids, feature rows) in submission order, point-in-time correct."""
    ordered = sorted(records, key=lambda r: (r.submitted_at, r.claim_id))
    history = _History()
    ids: list[int] = []
    rows: list[list[float]] = []
    for _, same_time in groupby(ordered, key=lambda r: r.submitted_at):
        batch = list(same_time)
        for c in batch:  # featurise the whole batch first: equal timestamps can't see each other
            ids.append(c.claim_id)
            rows.append(_features_for(c, history, limits))
        for c in batch:
            history.add(c)
    return ids, rows
