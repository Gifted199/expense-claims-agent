"""Fixed, parameterised SQL for the agent API: profile, demo directory and the manager queue.

Manager decisions run as app_writer in one transaction: lock the claim only if it is pending
AND the caller is the claimant's manager, update its status (the only column-level UPDATE that
matters here), append an approval row and an audit row. The database also refuses self-approval
(trigger from 002). Manager actions are never agent tools (D42).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

PROFILE_SQL = """
SELECT e.employee_id, e.employee_code, e.full_name, e.grade, e.is_active,
       EXISTS (SELECT 1 FROM employees r WHERE r.manager_id = e.employee_id AND r.is_active)
FROM employees e
WHERE e.employee_id = %s
"""
DIRECTORY_SQL = """
SELECT e.employee_id, e.employee_code, e.full_name, e.grade, e.is_active,
       EXISTS (SELECT 1 FROM employees r WHERE r.manager_id = e.employee_id AND r.is_active)
FROM employees e
WHERE e.is_active
ORDER BY e.employee_id
"""
QUEUE_SQL = """
SELECT c.claim_id, c.claim_ref, e.employee_code, e.full_name, c.category_code, c.amount,
       c.currency, c.incurred_at, c.submitted_at, c.merchant, c.description, c.city, c.nights,
       c.anomaly_score, c.review_reasons
FROM claims c
JOIN employees e ON e.employee_id = c.employee_id
WHERE e.manager_id = %s AND c.status = 'pending_manager'
ORDER BY c.submitted_at DESC, c.claim_id DESC
LIMIT 100
"""
LOCK_PENDING_CLAIM_SQL = """
SELECT c.claim_id, c.submitted_at
FROM claims c
JOIN employees e ON e.employee_id = c.employee_id
WHERE c.claim_id = %s AND c.status = 'pending_manager' AND e.manager_id = %s
FOR UPDATE OF c
"""
UPDATE_STATUS_SQL = "UPDATE claims SET status = %s WHERE claim_id = %s"
INSERT_MANAGER_APPROVAL_SQL = """
INSERT INTO approvals (claim_id, decider_type, decided_by, decision, reason, requested_at,
                       decided_at)
VALUES (%s, 'manager', %s, %s, %s, %s, %s)
"""
INSERT_DECISION_AUDIT_SQL = """
INSERT INTO audit_log (actor_type, actor_employee_id, action, entity_type, entity_id, details)
VALUES ('manager', %s, 'manager_decision', 'claim', %s, %s::jsonb)
"""
DECISIONS = ("approved", "rejected")
MAX_REASON = 1000


@dataclass(frozen=True)
class Profile:
    employee_id: int
    employee_code: str
    full_name: str
    grade: str
    is_active: bool
    is_manager: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "employee_code": self.employee_code,
            "full_name": self.full_name,
            "grade": self.grade,
            "is_manager": self.is_manager,
        }


def _profile(row: tuple) -> Profile:
    return Profile(int(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4]), bool(row[5]))


def load_profile(conn: Any, employee_id: int) -> Profile | None:
    row = conn.execute(PROFILE_SQL, (employee_id,)).fetchone()
    return _profile(row) if row else None


def directory(conn: Any) -> list[Profile]:
    return [_profile(r) for r in conn.execute(DIRECTORY_SQL).fetchall()]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def manager_queue(conn: Any, manager_id: int) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(QUEUE_SQL, (manager_id,)).fetchall():
        out.append(
            {
                "claim_id": int(r[0]),
                "claim_ref": r[1],
                "employee_code": r[2],
                "employee_name": r[3],
                "category": r[4],
                "amount": str(Decimal(r[5]).quantize(Decimal("0.01"))),
                "currency": r[6],
                "incurred_at": _iso(r[7]),
                "submitted_at": _iso(r[8]),
                "merchant": r[9],
                "description": r[10],
                "city": r[11],
                "nights": r[12],
                "anomaly_score": None if r[13] is None else round(float(r[13]), 3),
                "review_reasons": list(r[14] or []),
            }
        )
    return out


class DecisionError(ValueError):
    pass


def validate_decision(decision: str, reason: str) -> tuple[str, str]:
    decision = (decision or "").strip().lower()
    reason = (reason or "").strip()
    if decision not in DECISIONS:
        raise DecisionError("decision must be 'approved' or 'rejected'")
    if not 1 <= len(reason) <= MAX_REASON:
        raise DecisionError(f"a reason of 1-{MAX_REASON} characters is required")
    return decision, reason


def decide(
    conn: Any, manager_id: int, claim_id: int, decision: str, reason: str, now: datetime
) -> dict[str, Any] | None:
    """Run on an autocommit app_writer connection. None if the claim isn't a pending claim of one
    of the caller's direct reports (same answer as "doesn't exist")."""
    decision, reason = validate_decision(decision, reason)
    with conn.transaction():
        row = conn.execute(LOCK_PENDING_CLAIM_SQL, (claim_id, manager_id)).fetchone()
        if row is None:
            return None
        conn.execute(UPDATE_STATUS_SQL, (decision, claim_id))
        conn.execute(
            INSERT_MANAGER_APPROVAL_SQL, (claim_id, manager_id, decision, reason, row[1], now)
        )
        details = json.dumps({"decision": decision, "reason": reason})
        conn.execute(INSERT_DECISION_AUDIT_SQL, (manager_id, str(claim_id), details))
    return {"claim_id": claim_id, "status": decision, "reason": reason}
