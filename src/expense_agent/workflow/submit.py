"""The deterministic ``submit_claim`` workflow (decision D34).

Always the same steps, in the same order, and **every** check runs so all reasons are reported:

1. validate the fields (hotel without city or nights -> ``missing_fields``; other problems ->
   ``invalid``; nothing is written in either case)
2. limits: per-claim (hotels: nightly rate vs the city cap), monthly total
3. duplicate rule: same merchant and amount within 10 days
4. split rule: same merchant within +/-1 day, combined over the per-claim limit
5. submission deadline (late claims need a manager, EXP-2.3)
6. anomaly score (high >= 0.95)
7. decision **in code**: every check passed -> ``approved``; otherwise ``pending_manager``.

The LLM never decides: it only passes the fields. Writes run as app_writer in one transaction
that first takes a per-employee advisory lock, so two simultaneous claims can't both slip under
the monthly limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from expense_agent.ml.records import load_claims, load_limits
from expense_agent.ml.serving import AnomalyResult, features_for_new, score_row
from expense_agent.ocr.textract import ReceiptKeyError, check_receipt_key
from expense_agent.rag.handbook import LATEST
from expense_agent.workflow.queries import Employee

LONDON = ZoneInfo("Europe/London")
MAX_AMOUNT = Decimal("100000.00")
DUPLICATE_DAYS = 10
SPLIT_DAYS = 1
HOTEL = "hotel"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CATEGORIES_SQL = "SELECT category_code, is_travel, receipt_required_over FROM expense_categories"
LIMITS_SQL = """
SELECT per_claim_limit, monthly_limit FROM policy_limits WHERE category_code = %s AND grade = %s
"""
CITY_CAP_SQL = """
SELECT city, nightly_cap FROM policy_city_hotel_caps WHERE lower(city) = lower(%s) AND grade = %s
"""
MONTH_TOTAL_SQL = """
SELECT COALESCE(sum(amount), 0) FROM claims
WHERE employee_id = %s AND category_code = %s AND status <> 'rejected'
  AND date_trunc('month', incurred_at AT TIME ZONE 'Europe/London')
    = date_trunc('month', %s::timestamptz AT TIME ZONE 'Europe/London')
"""
DUPLICATE_SQL = """
SELECT count(*) FROM claims
WHERE employee_id = %s AND lower(merchant) = lower(%s) AND amount = %s AND status <> 'rejected'
  AND incurred_at BETWEEN %s::timestamptz - make_interval(days => %s)
                      AND %s::timestamptz + make_interval(days => %s)
"""
SPLIT_SQL = """
SELECT count(*), COALESCE(sum(amount), 0) FROM claims
WHERE employee_id = %s AND lower(merchant) = lower(%s) AND status <> 'rejected'
  AND abs((incurred_at AT TIME ZONE 'Europe/London')::date
          - (%s::timestamptz AT TIME ZONE 'Europe/London')::date) <= %s
"""
LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"
NEXT_ID_SQL = "SELECT nextval(pg_get_serial_sequence('claims', 'claim_id'))"
INSERT_CLAIM_SQL = """
INSERT INTO claims (claim_id, claim_ref, employee_id, category_code, amount, currency, incurred_at,
                    submitted_at, merchant, description, status, receipt_s3_key, anomaly_score,
                    city, nights, review_reasons)
VALUES (%s, %s, %s, %s, %s, 'GBP', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
INSERT_APPROVAL_SQL = """
INSERT INTO approvals (claim_id, decider_type, decided_by, decision, reason,
                       requested_at, decided_at)
VALUES (%s, 'system', NULL, 'approved', %s, %s, %s)
"""
INSERT_AUDIT_SQL = """
INSERT INTO audit_log (actor_type, actor_employee_id, action, entity_type, entity_id, details)
VALUES ('agent', %s, 'submit_claim', 'claim', %s, %s::jsonb)
"""


@dataclass(frozen=True)
class Category:
    code: str
    is_travel: bool
    receipt_required_over: Decimal


@dataclass(frozen=True)
class ClaimInput:
    category: str
    amount: Decimal
    merchant: str
    incurred_at: datetime
    description: str
    receipt_s3_key: str | None
    city: str | None
    nights: int | None


@dataclass
class Validation:
    claim: ClaimInput | None = None
    missing: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyFacts:
    per_claim_limit: Decimal
    monthly_limit: Decimal
    month_total_before: Decimal
    city: str | None  # canonical city name when it has its own cap
    city_cap: Decimal | None
    duplicates: int
    split_count: int
    split_total: Decimal


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    reason: str


def money(value: Decimal) -> str:
    return f"£{value:,.2f}"


def parse_incurred_at(value: str) -> datetime:
    """ISO date or datetime. A date alone means midday London time; naive times are London."""
    text = str(value).strip()
    if _DATE_ONLY.match(text):
        return datetime.fromisoformat(text).replace(hour=12, tzinfo=LONDON)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=LONDON)


def validate(
    raw: dict[str, Any],
    categories: dict[str, Category],
    employee: Employee,
    now: datetime,
) -> Validation:
    """Step 1. Pure: no database. Hotels need city and nights; nothing is ever guessed."""
    v = Validation()
    category = str(raw.get("category") or "").strip().lower()
    if category not in categories:
        v.errors["category"] = f"unknown category; use one of: {', '.join(sorted(categories))}"

    amount: Decimal | None = None
    try:
        amount = Decimal(str(raw.get("amount"))).quantize(Decimal("0.01"))
        if not (Decimal("0") < amount <= MAX_AMOUNT):
            v.errors["amount"] = f"amount must be more than £0 and at most {money(MAX_AMOUNT)}"
    except (InvalidOperation, ValueError):
        v.errors["amount"] = "amount must be a number, e.g. 45.60"

    merchant = str(raw.get("merchant") or "").strip()
    if not 1 <= len(merchant) <= 200:
        v.errors["merchant"] = "merchant is required (at most 200 characters)"
    description = str(raw.get("description") or "").strip()
    if len(description) > 1000:
        v.errors["description"] = "description must be at most 1000 characters"

    incurred_at: datetime | None = None
    try:
        text = str(raw.get("incurred_at") or "").strip()
        incurred_at = parse_incurred_at(text)
        if _DATE_ONLY.match(text) and incurred_at.date() == now.astimezone(LONDON).date():
            incurred_at = min(incurred_at, now)  # "today" before midday is not the future
        # No tolerance: the claims table requires incurred_at <= submitted_at (= now).
        if incurred_at > now:
            v.errors["incurred_at"] = "the expense date can't be in the future"
        elif (now - incurred_at).days > LATEST.late_cutoff_days:
            v.errors["incurred_at"] = (
                f"claims more than {LATEST.late_cutoff_days} days old are not reimbursed (EXP-2.3)"
            )
    except ValueError:
        v.errors["incurred_at"] = "incurred_at must be a date like 2026-11-20 (or ISO date-time)"

    receipt = (raw.get("receipt_s3_key") or None) and str(raw["receipt_s3_key"]).strip()
    if receipt:
        try:
            receipt = check_receipt_key(receipt, employee.employee_code)
        except ReceiptKeyError as exc:
            v.errors["receipt_s3_key"] = str(exc)
    cat = categories.get(category)
    if cat and amount is not None and not receipt and amount > cat.receipt_required_over:
        v.errors["receipt_s3_key"] = (
            f"a receipt is required for {category} claims over "
            f"{money(cat.receipt_required_over)} (EXP-3.1)"
        )

    city: str | None = None
    nights: int | None = None
    if category == HOTEL:
        city = str(raw.get("city") or "").strip() or None
        if city is None:
            v.missing.append("city")
        elif len(city) > 100:
            v.errors["city"] = "city must be at most 100 characters"
        if raw.get("nights") in (None, ""):
            v.missing.append("nights")
        else:
            try:
                nights = int(raw["nights"])
                if not 1 <= nights <= 60:
                    v.errors["nights"] = "nights must be between 1 and 60"
            except (TypeError, ValueError):
                v.errors["nights"] = "nights must be a whole number"

    if not v.missing and not v.errors:
        assert amount is not None and incurred_at is not None  # noqa: S101 - narrowed above
        v.claim = ClaimInput(
            category, amount, merchant, incurred_at, description, receipt, city, nights
        )
    return v


def evaluate(
    claim: ClaimInput, facts: PolicyFacts, anomaly: AnomalyResult, grade: str, now: datetime
) -> list[Check]:
    """Steps 2-6. Pure: every check always runs, in a fixed order."""
    checks: list[Check] = []
    if claim.category == HOTEL and claim.nights:
        nightly = (claim.amount / claim.nights).quantize(Decimal("0.01"))
        cap = facts.city_cap if facts.city_cap is not None else facts.per_claim_limit
        where = facts.city or "this city (UK-wide hotel limit)"
        ok = nightly <= cap
        checks.append(
            Check(
                "hotel_nightly_cap",
                ok,
                f"{money(nightly)}/night is {'within' if ok else 'over'} the {money(cap)} cap "
                f"for {where} at {grade} grade (EXP-6.3)",
            )
        )
    else:
        ok = claim.amount <= facts.per_claim_limit
        checks.append(
            Check(
                "per_claim_limit",
                ok,
                f"{money(claim.amount)} is {'within' if ok else 'over'} the "
                f"{money(facts.per_claim_limit)} per-claim limit for {claim.category} at "
                f"{grade} grade (EXP-4.1)",
            )
        )

    month_total = facts.month_total_before + claim.amount
    ok = month_total <= facts.monthly_limit
    checks.append(
        Check(
            "monthly_limit",
            ok,
            f"this month's {claim.category} total would be {money(month_total)}, "
            f"{'within' if ok else 'over'} the {money(facts.monthly_limit)} "
            "monthly limit (EXP-4.2)",
        )
    )

    ok = facts.duplicates == 0
    checks.append(
        Check(
            "duplicate",
            ok,
            "no earlier claim with the same merchant and amount within 10 days"
            if ok
            else f"possible duplicate: {facts.duplicates} earlier claim(s) at {claim.merchant} "
            f"for {money(claim.amount)} within 10 days (EXP-11.1)",
        )
    )

    combined = facts.split_total + claim.amount
    ok = not (facts.split_count > 0 and combined > facts.per_claim_limit)
    checks.append(
        Check(
            "split",
            ok,
            "no sign of a split purchase"
            if ok
            else f"possible split purchase: with {facts.split_count} other claim(s) at "
            f"{claim.merchant} within a day the total is {money(combined)}, over the "
            f"{money(facts.per_claim_limit)} per-claim limit (EXP-11.2)",
        )
    )

    age_days = (now - claim.incurred_at).days
    ok = age_days <= LATEST.deadline_days
    checks.append(
        Check(
            "deadline",
            ok,
            f"submitted within the {LATEST.deadline_days}-day deadline"
            if ok
            else f"submitted {age_days} days after the expense, after the "
            f"{LATEST.deadline_days}-day deadline (EXP-2.3)",
        )
    )

    ok = anomaly.label != "high"
    detail = f"; {', '.join(anomaly.reasons)}" if anomaly.reasons and not ok else ""
    checks.append(
        Check(
            "anomaly",
            ok,
            f"anomaly score {anomaly.score:.2f} is {'normal' if ok else 'high'}{detail} (EXP-9.4)",
        )
    )
    return checks


def decide(checks: list[Check]) -> str:
    """Step 7, in code: only a claim that passes every check is approved automatically."""
    return "approved" if all(c.passed for c in checks) else "pending_manager"


def load_categories(conn: Any) -> dict[str, Category]:
    return {
        str(code): Category(str(code), bool(travel), Decimal(receipt))
        for code, travel, receipt in conn.execute(CATEGORIES_SQL).fetchall()
    }


def load_facts(conn: Any, claim: ClaimInput, employee: Employee) -> PolicyFacts:
    per_claim, monthly = conn.execute(LIMITS_SQL, (claim.category, employee.grade)).fetchone()
    city, cap = None, None
    if claim.category == HOTEL and claim.city:
        row = conn.execute(CITY_CAP_SQL, (claim.city, employee.grade)).fetchone()
        if row:
            city, cap = str(row[0]), Decimal(row[1])
    eid, at = employee.employee_id, claim.incurred_at
    month_total = conn.execute(MONTH_TOTAL_SQL, (eid, claim.category, at)).fetchone()[0]
    dup_params = (eid, claim.merchant, claim.amount, at, DUPLICATE_DAYS, at, DUPLICATE_DAYS)
    duplicates = conn.execute(DUPLICATE_SQL, dup_params).fetchone()[0]
    split_n, split_total = conn.execute(SPLIT_SQL, (eid, claim.merchant, at, SPLIT_DAYS)).fetchone()
    return PolicyFacts(
        per_claim_limit=Decimal(per_claim),
        monthly_limit=Decimal(monthly),
        month_total_before=Decimal(month_total),
        city=city,
        city_cap=cap,
        duplicates=int(duplicates),
        split_count=int(split_n),
        split_total=Decimal(split_total),
    )


def score_new_claim(
    conn: Any, model: Any, claim: ClaimInput, employee: Employee, is_travel: bool, now: datetime
) -> AnomalyResult:
    row = features_for_new(
        load_claims(conn),
        load_limits(conn),
        employee_id=employee.employee_id,
        grade=employee.grade,
        category_code=claim.category,
        is_travel=is_travel,
        amount=float(claim.amount),
        merchant=claim.merchant,
        incurred_at=claim.incurred_at,
        now=now,
    )
    return score_row(model, row)


Scorer = Callable[[Any, ClaimInput, Employee, bool, datetime], AnomalyResult]


def submit_claim(
    conn: Any,
    employee: Employee,
    raw: dict[str, Any],
    get_model: Callable[[], Any],
    now: datetime | None = None,
    scorer: Scorer | None = None,
) -> dict[str, Any]:
    """Run the whole workflow on an app_writer connection (autocommit off)."""
    now = now or datetime.now(LONDON)
    with conn.transaction():
        conn.execute(LOCK_SQL, (employee.employee_id,))
        categories = load_categories(conn)
        v = validate(raw, categories, employee, now)
        if v.missing:
            return {
                "result": "missing_fields",
                "missing": v.missing,
                "errors": v.errors,
                "message": "Hotel claims need the city and the number of nights. "
                "Please ask the employee: " + " and ".join(_QUESTIONS[m] for m in v.missing),
            }
        if v.claim is None:
            return {"result": "invalid", "errors": v.errors}
        claim = v.claim

        facts = load_facts(conn, claim, employee)
        is_travel = categories[claim.category].is_travel
        if scorer is None:
            anomaly = score_new_claim(conn, get_model(), claim, employee, is_travel, now)
        else:
            anomaly = scorer(conn, claim, employee, is_travel, now)
        checks = evaluate(claim, facts, anomaly, employee.grade, now)
        status = decide(checks)

        failed = [c.reason for c in checks if not c.passed]
        claim_id = int(conn.execute(NEXT_ID_SQL).fetchone()[0])
        claim_ref = f"CLM-{now.year}-{claim_id:06d}"
        conn.execute(
            INSERT_CLAIM_SQL,
            (
                claim_id,
                claim_ref,
                employee.employee_id,
                claim.category,
                claim.amount,
                claim.incurred_at,
                now,
                claim.merchant,
                claim.description,
                status,
                claim.receipt_s3_key,
                anomaly.score,
                claim.city,
                claim.nights,
                failed or None,  # set here only; app_writer can't UPDATE it later (D44)
            ),
        )
        if status == "approved":
            conn.execute(
                INSERT_APPROVAL_SQL,
                (claim_id, "Within policy: every automatic check passed (EXP-9.1)", now, now),
            )
        details = {
            "status": status,
            "checks": [asdict(c) for c in checks],
            "anomaly": anomaly.to_dict(),
        }
        conn.execute(INSERT_AUDIT_SQL, (employee.employee_id, str(claim_id), json.dumps(details)))

    return {
        "result": "submitted",
        "claim_id": claim_id,
        "claim_ref": claim_ref,
        "status": status,
        "reasons": failed
        or ["Within policy: every automatic check passed, so it was approved (EXP-9.1)"],
        "checks": {c.name: {"passed": c.passed, "reason": c.reason} for c in checks},
        "anomaly": anomaly.to_dict(),
        "next_step": "Your manager will review it (EXP-9.2)."
        if status == "pending_manager"
        else "No further action needed.",
    }


_QUESTIONS = {
    "city": "which city was the hotel in?",
    "nights": "how many nights was the stay?",
}
