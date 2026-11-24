"""Synthetic, reproducible expense data with planted anomalies.

Pure module: no database, no network, no clock. The same seed always gives identical data.
Window: 1 Nov 2025 - 31 Oct 2026 (Europe/London). See docs/DATA.md for the plain-words design.
"""

from __future__ import annotations

import itertools
import math
import random
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- constants

LONDON = ZoneInfo("Europe/London")
WINDOW_START = date(2025, 11, 1)
WINDOW_END = date(2026, 10, 31)  # inclusive
AS_OF = datetime(2026, 11, 1, 9, 0, tzinfo=LONDON)  # "now" for the dataset
DEFAULT_SEED = 42
ANOMALY_RATE = 0.04
CURRENCY = "GBP"
CENT = Decimal("0.01")

Grade = Literal["associate", "senior", "director"]
GRADES: tuple[Grade, ...] = ("associate", "senior", "director")
ClaimStatus = Literal["submitted", "approved", "pending_manager", "rejected"]
AnomalyType = Literal[
    "duplicate_exact",
    "duplicate_near",
    "amount_outlier",
    "odd_time",
    "category_mismatch",
    "split_claim",
]
ANOMALY_TYPES: tuple[AnomalyType, ...] = (
    "duplicate_exact",
    "duplicate_near",
    "amount_outlier",
    "odd_time",
    "category_mismatch",
    "split_claim",
)
ANOMALY_WEIGHTS: dict[AnomalyType, float] = {
    "duplicate_exact": 0.15,
    "duplicate_near": 0.15,
    "amount_outlier": 0.20,
    "odd_time": 0.18,
    "category_mismatch": 0.17,
    "split_claim": 0.15,
}

PENDING_WINDOW = timedelta(days=14)  # over-limit claims this recent still await a manager
UNPROCESSED_WINDOW = timedelta(days=3)  # within-limit claims this recent are still "submitted"
N_REPEAT_OFFENDERS = 6
OFFENDER_SHARE = 0.6


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class Department:
    department_id: int
    code: str
    name: str
    cost_centre: str


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    description: str
    is_travel: bool
    receipt_required_over: Decimal


@dataclass(frozen=True)
class PolicyLimit:
    category_code: str
    grade: Grade
    per_claim_limit: Decimal
    monthly_limit: Decimal


@dataclass(frozen=True)
class Employee:
    employee_id: int
    employee_code: str
    full_name: str
    email: str
    department_id: int
    grade: Grade
    manager_id: int | None
    hired_on: date
    home_currency: str = CURRENCY


@dataclass(frozen=True)
class Claim:
    claim_id: int
    claim_ref: str
    employee_id: int
    category_code: str
    amount: Decimal
    currency: str
    incurred_at: datetime
    submitted_at: datetime
    merchant: str
    description: str
    status: ClaimStatus


@dataclass(frozen=True)
class Approval:
    claim_id: int
    decider_type: Literal["system", "manager"]
    decided_by: int | None
    decision: Literal["approved", "rejected"]
    reason: str
    requested_at: datetime
    decided_at: datetime


@dataclass(frozen=True)
class ClaimLabel:
    claim_id: int
    anomaly_type: AnomalyType
    related_claim_id: int | None
    notes: str


@dataclass(frozen=True)
class Dataset:
    seed: int
    departments: tuple[Department, ...]
    categories: tuple[Category, ...]
    policy_limits: tuple[PolicyLimit, ...]
    employees: tuple[Employee, ...]
    claims: tuple[Claim, ...]
    approvals: tuple[Approval, ...]
    labels: tuple[ClaimLabel, ...]


@dataclass(frozen=True)
class _Draft:
    """A claim before IDs and statuses are assigned."""

    key: int
    employee_id: int
    category_code: str
    amount: Decimal
    incurred_at: datetime
    submitted_at: datetime
    merchant: str
    description: str
    anomaly_type: AnomalyType | None = None
    related_key: int | None = None
    notes: str = ""


# --------------------------------------------------------------------------- reference data


@dataclass(frozen=True)
class _DeptSpec:
    dept: Department
    size: int
    monthly_rate: float  # mean claims per employee per month
    category_weights: dict[str, float]


DEPARTMENT_SPECS: tuple[_DeptSpec, ...] = (
    _DeptSpec(
        Department(1, "OPS", "Operations", "CC-1000"),
        6,
        5.5,
        {
            "meals": 1.2,
            "taxi": 0.8,
            "rail": 1.0,
            "hotel": 0.6,
            "air": 0.3,
            "mileage": 1.5,
            "client_entertainment": 0.3,
            "office_supplies": 1.2,
            "training": 0.5,
        },
    ),
    _DeptSpec(
        Department(2, "SALES", "Sales", "CC-2000"),
        10,
        9.0,
        {
            "meals": 3.0,
            "taxi": 2.5,
            "rail": 2.0,
            "hotel": 2.0,
            "air": 1.0,
            "mileage": 2.0,
            "client_entertainment": 2.0,
            "office_supplies": 0.5,
            "training": 0.3,
        },
    ),
    _DeptSpec(
        Department(3, "ENG", "Engineering", "CC-3000"),
        10,
        4.5,
        {
            "meals": 1.5,
            "taxi": 0.8,
            "rail": 0.8,
            "hotel": 0.6,
            "air": 0.4,
            "mileage": 0.3,
            "client_entertainment": 0.2,
            "office_supplies": 1.5,
            "training": 1.0,
        },
    ),
    _DeptSpec(
        Department(4, "MKT", "Marketing", "CC-4000"),
        8,
        6.5,
        {
            "meals": 2.0,
            "taxi": 1.5,
            "rail": 1.2,
            "hotel": 1.0,
            "air": 0.6,
            "mileage": 0.5,
            "client_entertainment": 1.2,
            "office_supplies": 1.0,
            "training": 0.6,
        },
    ),
    _DeptSpec(
        Department(5, "FIN", "Finance", "CC-5000"),
        6,
        4.5,
        {
            "meals": 1.0,
            "taxi": 0.5,
            "rail": 0.6,
            "hotel": 0.3,
            "air": 0.2,
            "mileage": 0.2,
            "client_entertainment": 0.2,
            "office_supplies": 1.0,
            "training": 0.8,
        },
    ),
)
DEPARTMENTS: tuple[Department, ...] = tuple(s.dept for s in DEPARTMENT_SPECS)
TOP_DEPARTMENT = "OPS"  # its director is the top of the tree
FALLBACK_APPROVER_DEPARTMENT = "FIN"  # approves the top person's claims


@dataclass(frozen=True)
class _CategorySpec:
    category: Category
    median_gbp: float
    sigma: float
    weekend_prob: float  # chance a normal claim falls on a weekend
    time_windows: tuple[tuple[float, int, int], ...]  # (weight, start_minute, end_minute)
    merchants: tuple[str, ...]
    descriptions: tuple[str, ...]
    limits: dict[Grade, tuple[str, str]]  # grade -> (per_claim, monthly)


def _c(code: str, name: str, desc: str, travel: bool, receipt_over: str) -> Category:
    return Category(code, name, desc, travel, Decimal(receipt_over))


def _hm(h: int, m: int = 0) -> int:
    return h * 60 + m


CATEGORY_SPECS: tuple[_CategorySpec, ...] = (
    _CategorySpec(
        _c("meals", "Meals", "Working meals while travelling or working late", False, "10.00"),
        18.0,
        0.45,
        0.0,
        ((0.7, _hm(12), _hm(14)), (0.3, _hm(18), _hm(21, 30))),
        (
            "Brew & Bite Cafe",
            "Green Leaf Kitchen",
            "Station Deli",
            "Noodle Corner",
            "The Daily Grain",
        ),
        ("Lunch during client visit", "Working lunch", "Dinner while travelling", "Team lunch"),
        {"associate": ("30", "300"), "senior": ("40", "400"), "director": ("60", "600")},
    ),
    _CategorySpec(
        _c("taxi", "Taxi", "Taxis and ride-hailing for business journeys", True, "10.00"),
        22.0,
        0.5,
        0.06,
        (
            (0.40, _hm(7), _hm(10)),
            (0.35, _hm(17), _hm(20)),
            (0.20, _hm(21), _hm(23, 59)),  # legit: home after a client dinner
            (0.05, _hm(0), _hm(1)),
        ),
        ("CityCab", "RideNow", "Metro Taxis", "Airport Express Cars"),
        ("Taxi to client office", "Taxi from station", "Taxi home after client dinner"),
        {"associate": ("50", "300"), "senior": ("60", "400"), "director": ("100", "600")},
    ),
    _CategorySpec(
        _c("rail", "Rail", "Train tickets for business travel", True, "0.00"),
        60.0,
        0.55,
        0.05,
        ((0.55, _hm(6), _hm(9)), (0.45, _hm(16), _hm(19))),
        ("Northline Rail", "Coastway Trains", "Midlands Rail Co"),
        ("Return ticket to client site", "Train to regional office", "Rail to conference"),
        {"associate": ("200", "800"), "senior": ("250", "1000"), "director": ("400", "1500")},
    ),
    _CategorySpec(
        _c("hotel", "Hotel", "Overnight accommodation for business travel", True, "0.00"),
        120.0,
        0.35,
        0.15,  # legit: Sunday night before a Monday meeting
        ((1.0, _hm(7), _hm(10)),),
        ("Harbour Hotels", "Citystay Inn", "Riverside Lodge", "Grand Central Suites"),
        (
            "Hotel for client workshop",
            "Overnight stay for conference",
            "Hotel near regional office",
        ),
        {"associate": ("150", "1200"), "senior": ("180", "1500"), "director": ("250", "2500")},
    ),
    _CategorySpec(
        _c("air", "Air travel", "Flights for business travel", True, "0.00"),
        180.0,
        0.5,
        0.08,
        ((1.0, _hm(6), _hm(20)),),
        ("SkyBridge Airways", "EuroHop Air", "Albion Air"),
        ("Flight to Edinburgh client", "Flight to Dublin office", "Flight to conference"),
        {"associate": ("400", "1500"), "senior": ("600", "2000"), "director": ("1000", "3500")},
    ),
    _CategorySpec(
        _c("mileage", "Mileage", "Business miles in a personal car at 45p per mile", True, "0.00"),
        35.0,
        0.6,
        0.03,
        ((1.0, _hm(8), _hm(18)),),
        ("Mileage claim",),
        ("Drive to client site", "Drive to supplier", "Drive to regional office"),
        {"associate": ("100", "500"), "senior": ("120", "600"), "director": ("150", "800")},
    ),
    _CategorySpec(
        _c(
            "client_entertainment",
            "Client entertainment",
            "Hospitality for clients and prospects",
            False,
            "0.00",
        ),
        150.0,
        0.5,
        0.0,
        ((1.0, _hm(19), _hm(22, 30)),),
        ("The Oak Room Restaurant", "Bistro Lumiere", "The Riverside Grill", "Saffron House"),
        ("Dinner with client", "Client lunch to discuss renewal", "Prospect dinner"),
        {"associate": ("100", "400"), "senior": ("250", "1000"), "director": ("500", "2000")},
    ),
    _CategorySpec(
        _c(
            "office_supplies",
            "Office supplies",
            "Stationery and small office items",
            False,
            "20.00",
        ),
        25.0,
        0.6,
        0.0,
        ((1.0, _hm(9), _hm(17)),),
        ("OfficeHub", "PaperTrail Stationers", "DeskMate Supplies"),
        ("Printer paper and pens", "Notebooks for workshop", "Replacement keyboard"),
        {"associate": ("75", "200"), "senior": ("100", "250"), "director": ("150", "400")},
    ),
    _CategorySpec(
        _c("training", "Training", "Courses, certifications and conferences", False, "0.00"),
        400.0,
        0.4,
        0.0,
        ((1.0, _hm(9), _hm(17)),),
        ("SkillUp Academy", "CloudCert Institute", "Leadership Forum UK"),
        ("Certification exam fee", "Two-day course", "Conference ticket"),
        {"associate": ("750", "1500"), "senior": ("1000", "2000"), "director": ("1500", "3000")},
    ),
)
CATEGORIES: tuple[Category, ...] = tuple(s.category for s in CATEGORY_SPECS)
_SPEC_BY_CODE: dict[str, _CategorySpec] = {s.category.code: s for s in CATEGORY_SPECS}

# Merchants that belong to no business category (used for category-mismatch anomalies).
PERSONAL_MERCHANTS: tuple[str, ...] = (
    "Pixel Electronics",
    "Fairway Golf Club",
    "Glow Day Spa",
    "Luxe Fashion House",
    "GameZone Store",
    "Skyline Cocktail Bar",
)
MERCHANT_CATEGORY: dict[str, str] = {
    **{m: s.category.code for s in CATEGORY_SPECS for m in s.merchants},
    **{m: "personal" for m in PERSONAL_MERCHANTS},
}

# Category pairs too similar to count as a mismatch (a restaurant is plausible for either).
OVERLAPPING: frozenset[frozenset[str]] = frozenset({frozenset(("meals", "client_entertainment"))})

ODD_TIME_CATEGORIES = ("meals", "office_supplies", "training", "client_entertainment")
OUTLIER_CATEGORIES = ("meals", "taxi", "office_supplies", "rail", "hotel")
MISMATCH_CATEGORIES = ("office_supplies", "training", "meals", "client_entertainment", "taxi")
SPLIT_CATEGORIES = ("client_entertainment", "hotel", "office_supplies", "training", "meals")

# Month seasonality (overall volume) and category-specific spikes.
MONTH_FACTOR: dict[int, float] = {
    1: 0.90, 2: 1.00, 3: 1.20, 4: 0.95, 5: 1.00, 6: 1.05,
    7: 0.95, 8: 0.65, 9: 1.20, 10: 1.15, 11: 1.05, 12: 0.85,
}  # fmt: skip
CATEGORY_MONTH_FACTOR: dict[tuple[str, int], float] = {
    ("client_entertainment", 12): 4.0,  # Christmas client hospitality
    ("training", 1): 2.5,  # new-year training budgets
    ("training", 9): 2.5,  # autumn conference season
    ("air", 8): 0.5,
}
GRADE_CATEGORY_FACTOR: dict[tuple[Grade, str], float] = {
    ("director", "client_entertainment"): 2.5,
    ("director", "air"): 2.0,
    ("director", "training"): 0.7,
    ("senior", "client_entertainment"): 1.2,
    ("associate", "client_entertainment"): 0.15,
    ("associate", "air"): 0.6,
}

FIRST_NAMES = (
    "Amara", "Ben", "Chloe", "Dev", "Elena", "Femi", "Grace", "Hamza", "Isla", "Jonah",
    "Kemi", "Liam", "Maya", "Nikhil", "Olivia", "Priya", "Quinn", "Rosa", "Sam", "Tariq",
    "Uma", "Victor", "Wren", "Xavier", "Yara", "Zane", "Aisha", "Callum", "Darcy", "Ewan",
)  # fmt: skip
LAST_NAMES = (
    "Adeyemi", "Brook", "Carver", "Dalton", "Ellison", "Fairfax", "Garnet", "Hollis", "Ingram",
    "Jessop", "Kendrick", "Lomax", "Marsh", "Norwood", "Okafor", "Pennant", "Quarry", "Rowan",
    "Sefton", "Thorne", "Underhill", "Vance", "Whitlow", "Yardley", "Zeller", "Ashdown",
)  # fmt: skip

DUPLICATE_REWORDINGS = ("{d} - resubmitted", "{d} (receipt attached)", "Re: {d}", "{d}.")


# --------------------------------------------------------------------------- helpers


def money(x: float | Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=rounding)


def policy_limits() -> tuple[PolicyLimit, ...]:
    return tuple(
        PolicyLimit(s.category.code, g, Decimal(s.limits[g][0]), Decimal(s.limits[g][1]))
        for s in CATEGORY_SPECS
        for g in GRADES
    )


def limit_lookup(limits: Sequence[PolicyLimit]) -> dict[tuple[str, Grade], PolicyLimit]:
    return {(pl.category_code, pl.grade): pl for pl in limits}


def months_in_window() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = WINDOW_START.year, WINDOW_START.month
    while date(y, m, 1) <= WINDOW_END:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def is_odd_time(ts: datetime) -> bool:
    """Weekend, or between 23:00 and 05:00 (local time)."""
    local = ts.astimezone(LONDON)
    return local.weekday() >= 5 or local.hour >= 23 or local.hour < 5


def _in_christmas_break(d: date) -> bool:
    return (d.month == 12 and d.day >= 22) or (d.month == 1 and d.day <= 2)


def _is_regular_day(d: date) -> bool:
    """A working weekday inside the window and outside the Christmas break."""
    return d.weekday() < 5 and not _in_christmas_break(d) and WINDOW_START <= d <= WINDOW_END


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _weighted(rng: random.Random, options: dict[str, float]) -> str:
    keys = list(options)
    return rng.choices(keys, weights=[options[k] for k in keys], k=1)[0]


def _days(year: int, month: int, *, weekend: bool) -> list[date]:
    days = [date(year, month, d) for d in range(1, monthrange(year, month)[1] + 1)]
    return [
        d
        for d in days
        if (d.weekday() >= 5) == weekend
        and not _in_christmas_break(d)
        and WINDOW_START <= d <= WINDOW_END
    ]


def _at(d: date, minute_of_day: int) -> datetime:
    return datetime.combine(d, time(minute_of_day // 60, minute_of_day % 60), tzinfo=LONDON)


def _normal_minute(rng: random.Random, spec: _CategorySpec) -> int:
    weights = [w for w, _, _ in spec.time_windows]
    _, start, end = rng.choices(spec.time_windows, weights=weights, k=1)[0]
    return rng.randint(start, end)


def _normal_incurred(rng: random.Random, spec: _CategorySpec, year: int, month: int) -> datetime:
    weekend = rng.random() < spec.weekend_prob
    days = _days(year, month, weekend=weekend) or _days(year, month, weekend=False)
    return _at(rng.choice(days), _normal_minute(rng, spec))


def _normal_amount(rng: random.Random, spec: _CategorySpec, spend_level: float) -> Decimal:
    x = spec.median_gbp * spend_level * rng.lognormvariate(0.0, spec.sigma)
    if spec.category.code == "hotel" and rng.random() < 0.25:
        x *= 1.6  # legit: London hotels
    return max(money(x), Decimal("1.00"))


def _submission_time(rng: random.Random, incurred: datetime) -> datetime:
    lag_days = min(int(rng.expovariate(0.25)), 30)
    t = _at(incurred.date() + timedelta(days=lag_days), _hm(rng.randint(8, 18), rng.randint(0, 59)))
    if t <= incurred:
        t = incurred + timedelta(minutes=rng.randint(10, 180))
    if t >= AS_OF:
        t = max(incurred + timedelta(minutes=10), AS_OF - timedelta(minutes=rng.randint(10, 600)))
    return t


# --------------------------------------------------------------------------- employees


def _make_employees(rng: random.Random) -> tuple[tuple[Employee, ...], dict[int, float]]:
    """40 people. IDs are ordered so every manager is inserted before their reports."""
    total = sum(s.size for s in DEPARTMENT_SPECS)
    pairs = rng.sample([(f, la) for f in FIRST_NAMES for la in LAST_NAMES], total)
    names = iter(pairs)
    ids = itertools.count(1)
    rows: list[tuple[int, Department, Grade, int | None]] = []

    ordered = sorted(DEPARTMENT_SPECS, key=lambda s: s.dept.code != TOP_DEPARTMENT)
    director: dict[str, int] = {}
    for s in ordered:
        director[s.dept.code] = next(ids)
    top = director[TOP_DEPARTMENT]
    for s in ordered:
        did = director[s.dept.code]
        rows.append((did, s.dept, "director", None if did == top else top))
    seniors: dict[str, list[int]] = defaultdict(list)
    for s in ordered:
        for _ in range(2):
            sid = next(ids)
            seniors[s.dept.code].append(sid)
            rows.append((sid, s.dept, "senior", director[s.dept.code]))
    for s in ordered:
        for i in range(s.size - 3):
            rows.append((next(ids), s.dept, "associate", seniors[s.dept.code][i % 2]))

    employees: list[Employee] = []
    spend_level: dict[int, float] = {}
    for emp_id, dept, grade, manager in sorted(rows, key=lambda r: r[0]):
        first, last = next(names)
        employees.append(
            Employee(
                employee_id=emp_id,
                employee_code=f"E{emp_id:04d}",
                full_name=f"{first} {last}",
                email=f"{first}.{last}@example.com".lower(),
                department_id=dept.department_id,
                grade=grade,
                manager_id=manager,
                hired_on=WINDOW_START - timedelta(days=rng.randint(60, 3000)),
            )
        )
        spend_level[emp_id] = min(max(rng.lognormvariate(0.0, 0.25), 0.6), 1.8)
    return tuple(employees), spend_level


# --------------------------------------------------------------------------- normal claims


def _normal_claims(
    rng: random.Random,
    employees: Sequence[Employee],
    spend_level: dict[int, float],
    keys: Iterator[int],
) -> list[_Draft]:
    dept_spec = {s.dept.department_id: s for s in DEPARTMENT_SPECS}
    drafts: list[_Draft] = []
    for emp in employees:
        ds = dept_spec[emp.department_id]
        for year, month in months_in_window():
            for _ in range(_poisson(rng, ds.monthly_rate * MONTH_FACTOR[month])):
                weights = {
                    code: w
                    * CATEGORY_MONTH_FACTOR.get((code, month), 1.0)
                    * GRADE_CATEGORY_FACTOR.get((emp.grade, code), 1.0)
                    for code, w in ds.category_weights.items()
                }
                spec = _SPEC_BY_CODE[_weighted(rng, weights)]
                incurred = _normal_incurred(rng, spec, year, month)
                drafts.append(
                    _Draft(
                        key=next(keys),
                        employee_id=emp.employee_id,
                        category_code=spec.category.code,
                        amount=_normal_amount(rng, spec, spend_level[emp.employee_id]),
                        incurred_at=incurred,
                        submitted_at=_submission_time(rng, incurred),
                        merchant=rng.choice(spec.merchants),
                        description=rng.choice(spec.descriptions),
                    )
                )
    return drafts


# --------------------------------------------------------------------------- anomalies


class _Planter:
    def __init__(
        self,
        rng: random.Random,
        employees: Sequence[Employee],
        spend_level: dict[int, float],
        normal: Sequence[_Draft],
        keys: Iterator[int],
    ) -> None:
        self.rng = rng
        self.employees = {e.employee_id: e for e in employees}
        self.spend_level = spend_level
        self.keys = keys
        self.by_emp: dict[int, list[_Draft]] = defaultdict(list)
        for d in normal:
            self.by_emp[d.employee_id].append(d)
        self.limits = limit_lookup(policy_limits())
        self.months = months_in_window()

    def _month(self) -> tuple[int, int]:
        return self.rng.choice(self.months)

    def duplicate(self, emp_id: int, exact: bool) -> list[_Draft]:
        latest = AS_OF - timedelta(days=11)
        candidates = [
            d
            for d in self.by_emp[emp_id]
            if d.submitted_at <= latest and WINDOW_START < d.incurred_at.date() < WINDOW_END
        ]
        if not candidates:
            return []
        orig = self.rng.choice(candidates)
        submitted = orig.submitted_at + timedelta(
            days=self.rng.randint(1, 10), minutes=self.rng.randint(1, 600)
        )
        if exact:
            return [
                replace(
                    orig,
                    key=next(self.keys),
                    submitted_at=submitted,
                    anomaly_type="duplicate_exact",
                    related_key=orig.key,
                    notes="same merchant, amount, date and description resubmitted",
                )
            ]
        delta = money(self.rng.randint(1, 200) / 100)
        amount = (
            orig.amount + delta
            if self.rng.random() < 0.5 or orig.amount <= delta
            else orig.amount - delta
        )
        incurred = orig.incurred_at
        if self.rng.random() < 0.5:
            shifted = incurred + timedelta(days=self.rng.choice((-1, 1)))
            # Only shift onto a regular working day, so the label stays a pure duplicate signal.
            if _is_regular_day(shifted.date()) and not is_odd_time(shifted):
                incurred = shifted
        description = self.rng.choice(DUPLICATE_REWORDINGS).format(d=orig.description)
        return [
            replace(
                orig,
                key=next(self.keys),
                amount=amount,
                incurred_at=incurred,
                submitted_at=submitted,
                description=description,
                anomaly_type="duplicate_near",
                related_key=orig.key,
                notes=f"near-duplicate: amount differs by {abs(amount - orig.amount)}",
            )
        ]

    def amount_outlier(self, emp_id: int) -> list[_Draft]:
        options = []
        for code in OUTLIER_CATEGORIES:
            amounts = [d.amount for d in self.by_emp[emp_id] if d.category_code == code]
            if len(amounts) >= 3:
                options.append((code, Decimal(str(median(amounts)))))
        if not options:
            return []
        code, med = self.rng.choice(options)
        factor = self.rng.uniform(4.0, 8.0)
        amount = money(med * Decimal(str(factor)), ROUND_UP)
        spec = _SPEC_BY_CODE[code]
        incurred = _normal_incurred(self.rng, spec, *self._month())
        return [
            _Draft(
                key=next(self.keys),
                employee_id=emp_id,
                category_code=code,
                amount=amount,
                incurred_at=incurred,
                submitted_at=_submission_time(self.rng, incurred),
                merchant=self.rng.choice(spec.merchants),
                description=self.rng.choice(spec.descriptions),
                anomaly_type="amount_outlier",
                notes=f"{factor:.1f}x this employee's median {code} claim ({med})",
            )
        ]

    def odd_time(self, emp_id: int) -> list[_Draft]:
        spec = _SPEC_BY_CODE[self.rng.choice(ODD_TIME_CATEGORIES)]
        year, month = self._month()
        if self.rng.random() < 0.5:
            days = _days(year, month, weekend=True)
            incurred = _at(self.rng.choice(days), _normal_minute(self.rng, spec))
            note = "non-travel expense on a weekend"
        else:
            days = _days(year, month, weekend=False)
            minute = self.rng.randint(_hm(23), _hm(23) + _hm(5) + 59) % (24 * 60)
            incurred = _at(self.rng.choice(days), minute)
            note = "non-travel expense between 23:00 and 05:00"
        if not is_odd_time(incurred):  # e.g. clocks-change edge case
            return []
        return [
            _Draft(
                key=next(self.keys),
                employee_id=emp_id,
                category_code=spec.category.code,
                amount=_normal_amount(self.rng, spec, self.spend_level[emp_id]),
                incurred_at=incurred,
                submitted_at=_submission_time(self.rng, incurred),
                merchant=self.rng.choice(spec.merchants),
                description=self.rng.choice(spec.descriptions),
                anomaly_type="odd_time",
                notes=note,
            )
        ]

    def category_mismatch(self, emp_id: int) -> list[_Draft]:
        claimed = _SPEC_BY_CODE[self.rng.choice(MISMATCH_CATEGORIES)]
        if self.rng.random() < 0.6:
            merchant = self.rng.choice(PERSONAL_MERCHANTS)
        else:
            other = self.rng.choice(
                [
                    s
                    for s in CATEGORY_SPECS
                    if s is not claimed
                    and s.category.code != "mileage"
                    and frozenset((s.category.code, claimed.category.code)) not in OVERLAPPING
                ]
            )
            merchant = self.rng.choice(other.merchants)
        incurred = _at(
            self.rng.choice(_days(*self._month(), weekend=False)),
            _normal_minute(self.rng, claimed),
        )
        return [
            _Draft(
                key=next(self.keys),
                employee_id=emp_id,
                category_code=claimed.category.code,
                amount=_normal_amount(self.rng, claimed, self.spend_level[emp_id]),
                incurred_at=incurred,
                submitted_at=_submission_time(self.rng, incurred),
                merchant=merchant,
                description=self.rng.choice(claimed.descriptions),
                anomaly_type="category_mismatch",
                notes=(
                    f"merchant is {MERCHANT_CATEGORY[merchant]}, claimed as {claimed.category.code}"
                ),
            )
        ]

    def split_claim(self, emp_id: int) -> list[_Draft]:
        emp = self.employees[emp_id]
        spec = _SPEC_BY_CODE[self.rng.choice(SPLIT_CATEGORIES)]
        limit = self.limits[(spec.category.code, emp.grade)].per_claim_limit
        parts = self.rng.choice((2, 2, 3))
        days = [d for d in _days(*self._month(), weekend=False) if d < WINDOW_END]
        day = self.rng.choice(days)
        next_day = day + timedelta(days=1)
        can_roll = _is_regular_day(next_day)
        # Start by 20:30 so up to two 60-minute gaps still end before 23:00 (not "odd time").
        minute = min(_normal_minute(self.rng, spec), _hm(20, 30))
        merchant = self.rng.choice(spec.merchants)
        out: list[_Draft] = []
        first_key: int | None = None
        for i in range(parts):
            amount = money(limit * Decimal(str(self.rng.uniform(0.85, 0.99))), ROUND_DOWN)
            amount = min(amount, limit - CENT)
            if i:
                minute += self.rng.randint(5, 60)
            on = next_day if i and can_roll and self.rng.random() < 0.3 else day
            incurred = _at(on, minute)
            key = next(self.keys)
            out.append(
                _Draft(
                    key=key,
                    employee_id=emp_id,
                    category_code=spec.category.code,
                    amount=amount,
                    incurred_at=incurred,
                    submitted_at=_submission_time(self.rng, incurred),
                    merchant=merchant,
                    description=self.rng.choice(spec.descriptions),
                    anomaly_type="split_claim",
                    related_key=first_key,
                    notes=f"part {i + 1} of {parts}; one purchase split under the {limit} limit",
                )
            )
            first_key = first_key or key
        return out

    def plant(self, kind: AnomalyType, emp_id: int) -> list[_Draft]:
        if kind in ("duplicate_exact", "duplicate_near"):
            return self.duplicate(emp_id, exact=kind == "duplicate_exact")
        return getattr(self, kind)(emp_id)  # type: ignore[no-any-return]


def _plant_anomalies(
    rng: random.Random,
    employees: Sequence[Employee],
    spend_level: dict[int, float],
    normal: Sequence[_Draft],
    keys: Iterator[int],
) -> list[_Draft]:
    target = round(ANOMALY_RATE * len(normal) / (1 - ANOMALY_RATE))
    planter = _Planter(rng, employees, spend_level, normal, keys)
    offenders = rng.sample(
        [e.employee_id for e in employees if e.manager_id is not None], N_REPEAT_OFFENDERS
    )
    everyone = [e.employee_id for e in employees]
    kinds = list(ANOMALY_WEIGHTS)
    weights = [ANOMALY_WEIGHTS[k] for k in kinds]
    planted: list[_Draft] = []
    attempt = 0
    while len(planted) < target:
        # Every type at least once first, then weighted at random.
        kind = (
            kinds[attempt] if attempt < len(kinds) else rng.choices(kinds, weights=weights, k=1)[0]
        )
        emp_id = rng.choice(offenders) if rng.random() < OFFENDER_SHARE else rng.choice(everyone)
        new = planter.plant(kind, emp_id)
        if new or attempt >= len(kinds):
            attempt += 1
        planted.extend(new)
    return planted


# --------------------------------------------------------------------------- statuses


def _finalise(
    rng: random.Random, employees: Sequence[Employee], drafts: Sequence[_Draft]
) -> tuple[tuple[Claim, ...], tuple[Approval, ...], tuple[ClaimLabel, ...]]:
    """Assign IDs in submission order and apply the deterministic workflow rules."""
    emp = {e.employee_id: e for e in employees}
    fallback = next(
        e.employee_id
        for e in employees
        if e.grade == "director"
        and e.department_id
        == next(d.department_id for d in DEPARTMENTS if d.code == FALLBACK_APPROVER_DEPARTMENT)
    )
    limits = limit_lookup(policy_limits())
    ordered = sorted(drafts, key=lambda d: (d.submitted_at, d.key))
    id_of = {d.key: i for i, d in enumerate(ordered, start=1)}
    month_total: dict[tuple[int, str, int, int], Decimal] = defaultdict(Decimal)

    claims: list[Claim] = []
    approvals: list[Approval] = []
    labels: list[ClaimLabel] = []
    for d in ordered:
        cid = id_of[d.key]
        e = emp[d.employee_id]
        pl = limits[(d.category_code, e.grade)]
        month_key = (d.employee_id, d.category_code, d.incurred_at.year, d.incurred_at.month)
        over_limit = (
            d.amount > pl.per_claim_limit or month_total[month_key] + d.amount > pl.monthly_limit
        )
        age = AS_OF - d.submitted_at
        status: ClaimStatus
        if over_limit:
            decided_at = d.submitted_at + timedelta(minutes=rng.randint(240, 5 * 24 * 60))
            if age < PENDING_WINDOW or decided_at >= AS_OF:
                status = "pending_manager"
            else:
                p_reject = 0.35 if d.anomaly_type else 0.12
                decision: Literal["approved", "rejected"] = (
                    "rejected" if rng.random() < p_reject else "approved"
                )
                status = decision
                approver = e.manager_id if e.manager_id is not None else fallback
                over = "per-claim" if d.amount > pl.per_claim_limit else "monthly"
                approvals.append(
                    Approval(
                        claim_id=cid,
                        decider_type="manager",
                        decided_by=approver,
                        decision=decision,
                        reason=f"Over {over} limit; manager {decision} after review",
                        requested_at=d.submitted_at,
                        decided_at=decided_at,
                    )
                )
        elif age < UNPROCESSED_WINDOW:
            status = "submitted"
        else:
            status = "approved"
            approvals.append(
                Approval(
                    claim_id=cid,
                    decider_type="system",
                    decided_by=None,
                    decision="approved",
                    reason="Within policy limits; auto-approved",
                    requested_at=d.submitted_at,
                    decided_at=d.submitted_at + timedelta(minutes=rng.randint(1, 5)),
                )
            )
        if status != "rejected":
            month_total[month_key] += d.amount
        claims.append(
            Claim(
                claim_id=cid,
                claim_ref=f"CLM-{d.submitted_at.year}-{cid:06d}",
                employee_id=d.employee_id,
                category_code=d.category_code,
                amount=d.amount,
                currency=CURRENCY,
                incurred_at=d.incurred_at,
                submitted_at=d.submitted_at,
                merchant=d.merchant,
                description=d.description,
                status=status,
            )
        )
        if d.anomaly_type:
            labels.append(
                ClaimLabel(
                    claim_id=cid,
                    anomaly_type=d.anomaly_type,
                    related_claim_id=id_of[d.related_key] if d.related_key is not None else None,
                    notes=d.notes,
                )
            )
    return tuple(claims), tuple(approvals), tuple(labels)


# --------------------------------------------------------------------------- public API


def generate(seed: int = DEFAULT_SEED) -> Dataset:
    """Build the full synthetic dataset. Deterministic for a given seed."""
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not cryptography
    keys = itertools.count(1)
    employees, spend_level = _make_employees(rng)
    normal = _normal_claims(rng, employees, spend_level, keys)
    planted = _plant_anomalies(rng, employees, spend_level, normal, keys)
    claims, approvals, labels = _finalise(rng, employees, [*normal, *planted])
    return Dataset(
        seed=seed,
        departments=DEPARTMENTS,
        categories=CATEGORIES,
        policy_limits=policy_limits(),
        employees=employees,
        claims=claims,
        approvals=approvals,
        labels=labels,
    )


def unlabelled_category_medians(dataset: Dataset) -> dict[tuple[int, str], Decimal]:
    """Median amount per (employee, category) over claims with no anomaly label."""
    labelled = {lb.claim_id for lb in dataset.labels}
    groups: dict[tuple[int, str], list[Decimal]] = defaultdict(list)
    for c in dataset.claims:
        if c.claim_id not in labelled:
            groups[(c.employee_id, c.category_code)].append(c.amount)
    return {k: Decimal(str(median(v))) for k, v in groups.items()}
