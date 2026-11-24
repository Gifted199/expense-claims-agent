"""Ranking metrics for anomaly detection. Pure Python.

- average_precision (PR-AUC): how clean the ranked list is, averaged over every true anomaly.
  Random ranking scores about the anomaly rate; 1.0 is perfect.
- precision@k / recall@k: with a review budget of k claims, the share reviewed that were real
  anomalies, and the share of all real anomalies that were inside the budget.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence

REVIEW_BUDGET = 0.05


def budget_k(n: int, budget: float = REVIEW_BUDGET) -> int:
    return max(1, math.ceil(budget * n))


def average_precision(y_true: Sequence[int], scores: Sequence[float]) -> float:
    """Step-wise AP over distinct score thresholds (same definition as scikit-learn)."""
    positives = sum(y_true)
    if positives == 0:
        return 0.0
    pairs = sorted(zip(scores, y_true, strict=True), key=lambda p: -p[0])
    ap = tp = fp = 0.0
    prev_recall = 0.0
    i = 0
    while i < len(pairs):
        threshold = pairs[i][0]
        while i < len(pairs) and pairs[i][0] == threshold:  # ties form one threshold
            tp += pairs[i][1]
            fp += 1 - pairs[i][1]
            i += 1
        recall = tp / positives
        ap += (recall - prev_recall) * (tp / (tp + fp))
        prev_recall = recall
    return ap


def top_k(scores: Sequence[float], k: int) -> list[int]:
    """Indices of the k highest scores; ties broken by position (deterministic)."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:k]


def precision_recall_at_k(
    y_true: Sequence[int], scores: Sequence[float], k: int
) -> tuple[float, float]:
    picked = top_k(scores, k)
    hits = sum(y_true[i] for i in picked)
    positives = sum(y_true)
    return hits / k, (hits / positives if positives else 0.0)


def recall_at_k_by_type(
    types: Sequence[str | None], scores: Sequence[float], k: int
) -> dict[str, float]:
    """Per anomaly type: share of that type's claims inside the top-k."""
    picked = set(top_k(scores, k))
    totals = Counter(t for t in types if t)
    found = Counter(types[i] for i in picked if types[i])
    return {t: found[t] / n for t, n in sorted(totals.items())}


def bootstrap_ap_interval(
    y_true: Sequence[int],
    scores: Sequence[float],
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap interval for average precision (resampling claims)."""
    n = len(y_true)
    if n == 0 or sum(y_true) == 0:
        return 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311 - statistics, not cryptography
    values: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        ys = [y_true[i] for i in idx]
        if sum(ys) == 0:
            continue
        values.append(average_precision(ys, [scores[i] for i in idx]))
    values.sort()
    lo = values[int((alpha / 2) * (len(values) - 1))]
    hi = values[int((1 - alpha / 2) * (len(values) - 1))]
    return lo, hi


def evaluate(
    scores: Sequence[float], labels: Mapping[int, str], claim_ids: Sequence[int]
) -> dict[str, object]:
    """All metrics for one model on one test set."""
    if not claim_ids:
        raise ValueError("empty test set: no claims to evaluate")
    types = [labels.get(cid) for cid in claim_ids]
    y = [1 if t else 0 for t in types]
    k = budget_k(len(y))
    precision, recall = precision_recall_at_k(y, scores, k)
    lo, hi = bootstrap_ap_interval(y, scores)
    return {
        "n": len(y),
        "anomalies": sum(y),
        "anomaly_rate": sum(y) / len(y) if y else 0.0,
        "k": k,
        "average_precision": average_precision(y, scores),
        "average_precision_ci95": [lo, hi],
        "precision_at_k": precision,
        "recall_at_k": recall,
        "recall_at_k_by_type": recall_at_k_by_type(types, scores, k),
    }
