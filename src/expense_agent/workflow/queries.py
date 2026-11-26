"""Read-only queries for the employee-facing tools (run as app_readonly).

Every query is a fixed SQL constant scoped by ``employee_id``, which always comes from the
server-side session (D33), never from the model. No free-form SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
STATUSES = ("submitted", "approved", "pending_manager", "rejected")
Period = Literal["this_month", "last_month", "this_quarter", "this_year", "last_12_months"]
PERIODS: tuple[str, ...] = Period.__args__  # type: ignore[attr-defined]
GroupBy = Literal["category", "month"]
MAX_CLAIMS = 50
NOT_FOUND = "claim not found"


@dataclass(frozen=True)
class Employee:
    employee_id: int
    employee_code: str
    grade: str
    is_active: bool


EMPLOYEE_SQL = """
SELECT employee_id, employee_code, grade, is_active FROM employees WHERE employee_id = %s
"""

MY_CLAIMS_SQL = """
SELECT claim_id, claim_ref, category_code, amount, currency, incurred_at, submitted_at,
       merchant, status, city, nights
FROM claims
WHERE employee_id = %(employee_id)s
  AND (%(status)s::text IS NULL OR status = %(status)s::text)
  AND (%(from)s::date IS NULL OR (incurred_at AT TIME ZONE 'Europe/London')::date >= %(from)s::date)
  AND (%(to)s::date IS NULL OR (incurred_at AT TIME ZONE 'Europe/London')::date <= %(to)s::date)
ORDER BY incurred_at DESC, claim_id DESC
LIMIT %(limit)s
"""

CLAIM_STATUS_SQL = """
SELECT c.claim_id, c.claim_ref, c.category_code, c.amount, c.currency, c.incurred_at,
       c.submitted_at, c.merchant, c.status, c.anomaly_score,
       a.decider_type, a.decision, a.reason, a.decided_at
FROM claims c
LEFT JOIN LATERAL (
    SELECT decider_type, decision, reason, decided_at FROM approvals
    WHERE approvals.claim_id = c.claim_id ORDER BY decided_at DESC LIMIT 1
) a ON true
WHERE c.employee_id = %(employee_id)s
  AND (c.claim_id = %(claim_id)s::bigint OR c.claim_ref = %(claim_ref)s::text)
"""

SUMMARY_BY_CATEGORY_SQL = """
SELECT category_code AS bucket, count(*), sum(amount)
FROM claims
WHERE employee_id = %s AND status <> 'rejected'
  AND (incurred_at AT TIME ZONE 'Europe/London')::date >= %s
  AND (incurred_at AT TIME ZONE 'Europe/London')::date < %s
GROUP BY category_code
ORDER BY sum(amount) DESC
"""

SUMMARY_BY_MONTH_SQL = """
SELECT to_char(date_trunc('month', incurred_at AT TIME ZONE 'Europe/London'), 'YYYY-MM') AS bucket,
       count(*), sum(amount)
FROM claims
WHERE employee_id = %s AND status <> 'rejected'
  AND (incurred_at AT TIME ZONE 'Europe/London')::date >= %s
  AND (incurred_at AT TIME ZONE 'Europe/London')::date < %s
GROUP BY 1
ORDER BY 1
"""
SUMMARY_SQL: dict[str, str] = {"category": SUMMARY_BY_CATEGORY_SQL, "month": SUMMARY_BY_MONTH_SQL}


def load_employee(conn: Any, employee_id: int) -> Employee | None:
    row = conn.execute(EMPLOYEE_SQL, (employee_id,)).fetchone()
    if row is None:
        return None
    return Employee(int(row[0]), str(row[1]), str(row[2]), bool(row[3]))


def _money(value: Any) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def parse_day(value: str | None, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{name} must be a date like 2026-11-01") from exc


def get_my_claims(
    conn: Any,
    employee_id: int,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    params = {
        "employee_id": employee_id,
        "status": status,
        "from": parse_day(date_from, "from"),
        "to": parse_day(date_to, "to"),
        "limit": MAX_CLAIMS,
    }
    rows = conn.execute(MY_CLAIMS_SQL, params).fetchall()
    claims = [
        {
            "claim_id": int(r[0]),
            "claim_ref": r[1],
            "category": r[2],
            "amount": _money(r[3]),
            "currency": r[4],
            "incurred_at": _iso(r[5]),
            "submitted_at": _iso(r[6]),
            "merchant": r[7],
            "status": r[8],
            "city": r[9],
            "nights": r[10],
        }
        for r in rows
    ]
    return {"claims": claims, "count": len(claims), "limited_to": MAX_CLAIMS}


def parse_claim_identifier(value: str | int) -> tuple[int | None, str | None]:
    text = str(value).strip()
    if text.isdigit():
        return int(text), None
    if text.upper().startswith("CLM-"):
        return None, text.upper()
    raise ValueError("give a claim number (e.g. 1234) or reference (e.g. CLM-2026-001234)")


def get_claim_status(conn: Any, employee_id: int, claim: str | int) -> dict[str, Any]:
    claim_id, claim_ref = parse_claim_identifier(claim)
    params = {"employee_id": employee_id, "claim_id": claim_id, "claim_ref": claim_ref}
    r = conn.execute(CLAIM_STATUS_SQL, params).fetchone()
    if r is None:  # missing and "someone else's" look identical
        return {"found": False, "message": NOT_FOUND}
    decision = None
    if r[10] is not None:
        decision = {
            "by": "system" if r[10] == "system" else "manager",
            "decision": r[11],
            "reason": r[12],
            "decided_at": _iso(r[13]),
        }
    return {
        "found": True,
        "claim_id": int(r[0]),
        "claim_ref": r[1],
        "category": r[2],
        "amount": _money(r[3]),
        "currency": r[4],
        "incurred_at": _iso(r[5]),
        "submitted_at": _iso(r[6]),
        "merchant": r[7],
        "status": r[8],
        "anomaly_score": None if r[9] is None else round(float(r[9]), 3),
        "latest_decision": decision,
    }


def _month_start(d: date, months_back: int = 0) -> date:
    index = d.year * 12 + (d.month - 1) - months_back
    return date(index // 12, index % 12 + 1, 1)


def period_bounds(period: str, today: date) -> tuple[date, date]:
    """[start, end) in London calendar dates."""
    next_month = _month_start(today, -1)
    if period == "this_month":
        return _month_start(today), next_month
    if period == "last_month":
        return _month_start(today, 1), _month_start(today)
    if period == "this_quarter":
        q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        return q_start, _month_start(q_start, -3)
    if period == "this_year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    if period == "last_12_months":
        return _month_start(today, 11), next_month
    raise ValueError(f"period must be one of {', '.join(PERIODS)}")


def spending_summary(
    conn: Any, employee_id: int, period: str, group_by: str, now: datetime | None = None
) -> dict[str, Any]:
    if group_by not in SUMMARY_SQL:
        raise ValueError("group_by must be 'category' or 'month'")
    today = (now or datetime.now(LONDON)).astimezone(LONDON).date()
    start, end = period_bounds(period, today)
    rows = conn.execute(SUMMARY_SQL[group_by], (employee_id, start, end)).fetchall()
    groups = [{group_by: r[0], "claims": int(r[1]), "total": _money(r[2])} for r in rows]
    total = sum((Decimal(g["total"]) for g in groups), Decimal("0"))
    return {
        "period": period,
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "group_by": group_by,
        "currency": "GBP",
        "groups": groups,
        "total": _money(total),
        "excludes": "rejected claims",
    }
