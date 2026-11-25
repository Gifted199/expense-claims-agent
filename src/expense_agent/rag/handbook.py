"""Synthetic expense policy handbook, generated from the database (decision D24).

Every money figure in the text comes from ``policy_limits``, ``expense_categories``
(receipt thresholds) or ``policy_city_hotel_caps``. Nothing monetary is typed into the prose,
so the handbook can never disagree with the limits the workflow enforces.

Two versions are rendered: the previous one (kept for audit) and the latest (the only one
searched). They share section IDs and differ only in rules (deadline, splitting, receipts).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

GRADES = ("associate", "senior", "director")
GRADE_LABEL = {"associate": "Associate", "senior": "Senior", "director": "Director"}


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    description: str
    is_travel: bool
    receipt_required_over: Decimal


@dataclass(frozen=True)
class Limit:
    category_code: str
    grade: str
    per_claim: Decimal
    monthly: Decimal


@dataclass(frozen=True)
class CityCap:
    city: str
    grade: str
    nightly_cap: Decimal


@dataclass(frozen=True)
class PolicyData:
    categories: tuple[Category, ...]
    limits: tuple[Limit, ...]
    city_caps: tuple[CityCap, ...]

    def limit(self, category: str, grade: str) -> Limit:
        return next(x for x in self.limits if x.category_code == category and x.grade == grade)

    def category(self, code: str) -> Category:
        return next(c for c in self.categories if c.code == code)

    def money_values(self) -> set[Decimal]:
        """Every money figure the handbook is allowed to contain."""
        values = {x.per_claim for x in self.limits} | {x.monthly for x in self.limits}
        values |= {c.receipt_required_over for c in self.categories}
        values |= {c.nightly_cap for c in self.city_caps}
        return values


@dataclass(frozen=True)
class VersionSpec:
    version: str
    effective_from: date
    supersedes: str | None
    deadline_days: int
    late_cutoff_days: int
    split_rule: str
    receipt_rule: str
    city_caps: bool
    changes: tuple[str, ...]


PREVIOUS = VersionSpec(
    version="2025.1",
    effective_from=date(2025, 1, 1),
    supersedes=None,
    deadline_days=60,
    late_cutoff_days=120,
    split_rule=(
        "Claims relating to a single purchase should normally be submitted together. "
        "Questions about how to submit a large bill should be raised with Finance."
    ),
    receipt_rule="Photographs of receipts are accepted if the merchant and total are readable.",
    city_caps=False,
    changes=(),
)
LATEST = VersionSpec(
    version="2026.1",
    effective_from=date(2026, 4, 1),
    supersedes="2025.1",
    deadline_days=30,
    late_cutoff_days=90,
    split_rule=(
        "Splitting one purchase into several claims so that each part stays under a limit is a "
        "**policy breach**, even if every part looks acceptable on its own. One bill is one "
        "claim. If the total is above your limit, submit it as one claim: it will be routed to "
        "your manager, who can approve it."
    ),
    receipt_rule=(
        "Photographs and PDFs are accepted only if the merchant name, date, itemised lines, "
        "total and VAT (where applicable) are all readable. Card-terminal slips that show only a "
        "total are not receipts."
    ),
    city_caps=True,
    changes=(
        "The submission deadline is shorter (see EXP-2.1).",
        "Splitting a purchase to stay under a limit is now an explicit breach (see EXP-11.2).",
        "Receipt evidence requirements are stricter (see EXP-3.2).",
        "Hotel caps by city added (see EXP-6.3).",
    ),
)
VERSIONS: tuple[VersionSpec, ...] = (PREVIOUS, LATEST)


def money(value: Decimal) -> str:
    """£30 for whole pounds, £12.50 otherwise."""
    value = Decimal(value)
    return f"£{value:,.0f}" if value == value.to_integral_value() else f"£{value:,.2f}"


def _grade_table(data: PolicyData, categories: Sequence[str], field: str) -> str:
    header = "| Category | " + " | ".join(GRADE_LABEL[g] for g in GRADES) + " |"
    sep = "|---|" + "---|" * len(GRADES)
    rows = [
        f"| {data.category(code).name} | "
        + " | ".join(money(getattr(data.limit(code, g), field)) for g in GRADES)
        + " |"
        for code in categories
    ]
    return "\n".join([header, sep, *rows])


def _per_grade_sentence(data: PolicyData, code: str) -> str:
    parts = [
        f"{GRADE_LABEL[g]} {money(data.limit(code, g).per_claim)} per claim "
        f"({money(data.limit(code, g).monthly)} per month)"
        for g in GRADES
    ]
    return "; ".join(parts)


def _city_table(data: PolicyData) -> str:
    cities = sorted({c.city for c in data.city_caps})
    caps = {(c.city, c.grade): c.nightly_cap for c in data.city_caps}
    header = "| City | " + " | ".join(GRADE_LABEL[g] for g in GRADES) + " |"
    sep = "|---|" + "---|" * len(GRADES)
    rows = [
        f"| {city} | " + " | ".join(money(caps[(city, g)]) for g in GRADES) + " |"
        for city in cities
    ]
    elsewhere = (
        "| Elsewhere in the UK | "
        + " | ".join(money(data.limit("hotel", g).per_claim) for g in GRADES)
        + " |"
    )
    return "\n".join([header, sep, *rows, elsewhere])


def _hotel_section(data: PolicyData, spec: VersionSpec) -> str:
    extras = (
        "Breakfast may be included in the room rate; other hotel extras (minibar, films, spa) "
        "are not reimbursed."
    )
    if not spec.city_caps:
        return (
            "Hotel stays are limited by the hotel per-claim limit for your grade: "
            f"{_per_grade_sentence(data, 'hotel')}. {extras}"
        )
    intro = "The most you can claim per night for a hotel depends on the city and your grade:"
    outro = f"{extras} A stay above the cap for your city is sent to your manager."
    return "\n\n".join([intro, _city_table(data), outro])


def render(data: PolicyData, spec: VersionSpec) -> str:
    all_codes = [c.code for c in data.categories]
    receipt_rows = "\n".join(
        f"| {c.name} | {money(c.receipt_required_over)} |"
        if c.receipt_required_over > 0
        else f"| {c.name} | Always |"
        for c in data.categories
    )
    header = [
        "# Expense Policy Handbook",
        "",
        f"**Version:** {spec.version} · **Effective from:** {spec.effective_from.isoformat()}"
        + (f" · **Supersedes:** {spec.supersedes}" if spec.supersedes else ""),
        "",
        "All amounts are in pounds sterling (GBP). Limits are maintained by Finance and are "
        "the same limits the expense system enforces automatically.",
    ]
    if spec.changes:
        header += ["", "**What changed in this version:**", *[f"- {c}" for c in spec.changes]]

    body = f"""
## EXP-1 Scope

This handbook explains what the company will reimburse, how to claim and what happens next.

### EXP-1.1 Who this applies to

This policy applies to all employees, at every grade, who spend their own money on company
business. Contractors follow the terms of their own contract.

### EXP-1.2 What counts as a business expense

A business expense is a cost you would not have had if you were not doing your job: travel
to a client or another office, a working meal while travelling, training your manager agreed,
or hospitality for a client. Everyday costs such as your commute to your usual office are not
business expenses.

### EXP-1.3 Currency and reimbursement

Claims are paid in pounds sterling. If you paid in another currency, claim the amount shown on
your card statement in pounds. Approved claims are paid with the next monthly payroll.

## EXP-2 Submission deadlines and process

When and how to submit a claim.

### EXP-2.1 Submission deadline

Submit each claim within **{spec.deadline_days} days** of the date you spent the money. The
date that counts is the date on the receipt, not the date you submit.

### EXP-2.2 How to submit a claim

Submit one claim per receipt through the expense assistant or the expenses portal. Choose the
category that matches what you bought, describe the business purpose in a sentence, and attach
the receipt. The assistant can read a receipt photo and fill in the claim for you to check.

### EXP-2.3 Late claims

A claim submitted after the deadline needs your manager's approval and a short explanation.
Claims more than {spec.late_cutoff_days} days old are not reimbursed.

## EXP-3 Receipts

What evidence you need for a claim.

### EXP-3.1 When a receipt is required

A receipt is required for any claim above the amount shown for its category:

| Category | Receipt required above |
|---|---|
{receipt_rows}

### EXP-3.2 What counts as a valid receipt

{spec.receipt_rule} Keep the original until the claim has been paid.

### EXP-3.3 Lost receipts

If a receipt is lost, submit a signed missing-receipt declaration with the claim. Your manager
must approve it. Repeated missing receipts are reported to Finance.

## EXP-4 Limits by category and grade

The amounts you can claim, by category and by your grade.

### EXP-4.1 Per-claim limits

The most you can claim on a single claim:

{_grade_table(data, all_codes, "per_claim")}

### EXP-4.2 Monthly limits

The most you can claim in each category in one calendar month, counting all your claims that
were not rejected:

{_grade_table(data, all_codes, "monthly")}

### EXP-4.3 Claims above a limit

A claim above the per-claim limit, or one that would take you over the monthly limit, is not
rejected automatically. It is never approved automatically either: it is always sent to your
manager to decide (see EXP-9.2).

## EXP-5 Meals, taxis and mileage

Day-to-day costs while working away from your usual office.

### EXP-5.1 Meals

You can claim a reasonable working meal when you are travelling on business or working late at
a client site. Limits: {_per_grade_sentence(data, "meals")}. Alcohol is not reimbursed with a
solo meal. Team meals in the office are not claimable unless your director agreed them first.

### EXP-5.2 Taxis

Use public transport where it is practical. A taxi is claimable when there is no practical
public transport, when you are carrying heavy equipment, or for your safety when travelling
late at night after a business event. Limits: {_per_grade_sentence(data, "taxi")}.

### EXP-5.3 Mileage

If you drive your own car on business, claim the business miles at the approved mileage
allowance rate set by Finance, not your fuel cost. Record the start and end point and the
purpose. Your usual commute does not count. Limits: {_per_grade_sentence(data, "mileage")}.

## EXP-6 Travel and hotels

Rail, air and overnight stays.

### EXP-6.1 Rail travel

Book standard class, as early as possible. First class is allowed only for directors or when
it is cheaper than standard. Limits: {_per_grade_sentence(data, "rail")}.

### EXP-6.2 Air travel

Fly economy on routes within the UK and Europe. Book through the travel portal where possible.
Limits: {_per_grade_sentence(data, "air")}.

### EXP-6.3 Hotels by city

{_hotel_section(data, spec)}

### EXP-6.4 Booking travel and weekend stays

Book travel and hotels through the travel portal where possible. A Sunday-night stay before an
early Monday meeting is a business expense. Extending a trip for personal reasons is allowed
only if it costs the company nothing extra.

## EXP-7 Client entertainment

Hospitality for clients and prospects.

### EXP-7.1 Who can entertain clients and limits

Seniors and directors can entertain clients. Associates need their manager's agreement in
advance. Limits: {_per_grade_sentence(data, "client_entertainment")}.

### EXP-7.2 Attendees and records

Record the names and companies of everyone who attended and the business purpose. Entertaining
only colleagues is not client entertainment.

### EXP-7.3 Gifts and the hospitality register

Gifts to clients and any hospitality you receive must be recorded in the hospitality register.
Cash or cash-equivalent gifts are never allowed.

## EXP-8 Training and conferences

Learning and development.

### EXP-8.1 Courses and certifications

Courses and exam fees need your manager's agreement before you book. Limits:
{_per_grade_sentence(data, "training")}.

### EXP-8.2 Conferences

Conference tickets are claimed as training. Travel and hotels for a conference follow EXP-6.
Office supplies for workshops are limited to: {_per_grade_sentence(data, "office_supplies")}.

## EXP-9 Approvals

What happens after you submit.

### EXP-9.1 Automatic approval

A claim within the per-claim and monthly limits for your grade, with the right receipt and no
unusual features, is approved automatically.

### EXP-9.2 Manager review

A claim that is over a limit, or that the system flags as unusual, always goes to your manager.
It is never approved automatically and the expense assistant never makes the decision: only
your manager can approve or reject it.

### EXP-9.3 You cannot approve your own claim

Nobody can approve or reject their own claim. Claims from someone without a manager are
reviewed by the Finance director.

### EXP-9.4 Unusual-claim checks

Every claim is checked for unusual patterns, for example an amount far above your normal spend
for that category, a possible duplicate, or a purchase that looks split. A flag is not an
accusation: it means a person takes a second look.

## EXP-10 Prohibited items

What is never reimbursed.

### EXP-10.1 Items that are never reimbursed

The company never reimburses personal investments or financial products of any kind,
including shares, cryptocurrency, funds, bonds or pension top-ups. The expense assistant does
not give investment or financial advice. Also never reimbursed: personal subscriptions, gifts
for colleagues, and personal travel.

### EXP-10.2 Alcohol and personal items

Alcohol-only bills, clothing, toiletries, personal electronics and spa or leisure services are
not reimbursed, even when bought during a business trip.

### EXP-10.3 Fines and penalties

Parking fines, speeding fines, congestion-charge penalties and late-payment fees are your
responsibility and are never reimbursed.

## EXP-11 Policy breaches

Claims that break the rules on purpose or by mistake.

### EXP-11.1 Duplicate claims

Each purchase can be claimed once. Submitting the same receipt twice, or the same purchase
with a slightly different amount or date, is a duplicate claim. Duplicates are rejected and
repeated duplicates are reported.

### EXP-11.2 Splitting a purchase

{spec.split_rule}

### EXP-11.3 Consequences and reporting

Breaches are reviewed by your manager and Finance. Money paid for a claim that breaks this
policy is recovered through payroll, and serious or repeated breaches are handled under the
disciplinary policy.
"""
    return "\n".join(header) + "\n" + body


def policy_data_from_rows(
    categories: Sequence[tuple[Any, ...]],
    limits: Sequence[tuple[Any, ...]],
    city_caps: Sequence[tuple[Any, ...]],
) -> PolicyData:
    """Build PolicyData from raw DB rows (see ``ingest.LOAD_*_SQL`` for column order)."""
    return PolicyData(
        categories=tuple(
            Category(str(c), str(n), str(d), bool(t), Decimal(r)) for c, n, d, t, r in categories
        ),
        limits=tuple(Limit(str(c), str(g), Decimal(p), Decimal(m)) for c, g, p, m in limits),
        city_caps=tuple(CityCap(str(c), str(g), Decimal(v)) for c, g, v in city_caps),
    )
