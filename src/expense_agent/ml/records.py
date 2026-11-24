"""Inputs for the anomaly model.

``ClaimRecord`` deliberately has **no** status, approval or label fields (D11): the model may only
see what is known about a claim at the moment it is submitted. Labels are loaded separately by
``load_labels`` and used **only** for evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from expense_agent.db.synthetic import Dataset


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: int
    employee_id: int
    grade: str
    category_code: str
    is_travel: bool
    amount: float
    merchant: str
    incurred_at: datetime
    submitted_at: datetime


Limits = dict[tuple[str, str], float]  # (category_code, grade) -> per-claim limit (GBP)

LOAD_CLAIMS_SQL = """
SELECT c.claim_id, c.employee_id, e.grade, c.category_code, ec.is_travel,
       c.amount, c.merchant, c.incurred_at, c.submitted_at
FROM claims c
JOIN employees e USING (employee_id)
JOIN expense_categories ec USING (category_code)
ORDER BY c.submitted_at, c.claim_id
"""
LOAD_LIMITS_SQL = "SELECT category_code, grade, per_claim_limit FROM policy_limits"
LOAD_LABELS_SQL = "SELECT claim_id, anomaly_type FROM claim_labels"  # evaluation only


def load_claims(conn: Any) -> list[ClaimRecord]:
    return [
        ClaimRecord(
            claim_id=int(r[0]),
            employee_id=int(r[1]),
            grade=str(r[2]),
            category_code=str(r[3]),
            is_travel=bool(r[4]),
            amount=float(r[5]),
            merchant=str(r[6]),
            incurred_at=r[7],
            submitted_at=r[8],
        )
        for r in conn.execute(LOAD_CLAIMS_SQL).fetchall()
    ]


def load_limits(conn: Any) -> Limits:
    return {(str(c), str(g)): float(lim) for c, g, lim in conn.execute(LOAD_LIMITS_SQL).fetchall()}


def load_labels(conn: Any) -> dict[int, str]:
    """EVALUATION ONLY. claim_id -> anomaly_type for planted anomalies."""
    return {int(cid): str(t) for cid, t in conn.execute(LOAD_LABELS_SQL).fetchall()}


def from_dataset(ds: Dataset) -> tuple[list[ClaimRecord], Limits, dict[int, str]]:
    """Build model inputs straight from the synthetic generator (tests; no database)."""
    grade = {e.employee_id: e.grade for e in ds.employees}
    travel = {c.code: c.is_travel for c in ds.categories}
    records = [
        ClaimRecord(
            claim_id=c.claim_id,
            employee_id=c.employee_id,
            grade=grade[c.employee_id],
            category_code=c.category_code,
            is_travel=travel[c.category_code],
            amount=float(c.amount),
            merchant=c.merchant,
            incurred_at=c.incurred_at,
            submitted_at=c.submitted_at,
        )
        for c in sorted(ds.claims, key=lambda c: (c.submitted_at, c.claim_id))
    ]
    limits = {(p.category_code, p.grade): float(p.per_claim_limit) for p in ds.policy_limits}
    labels = {lb.claim_id: lb.anomaly_type for lb in ds.labels}
    return records, limits, labels
