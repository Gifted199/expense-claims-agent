"""A fake app_writer connection that answers the workflow's fixed SQL constants."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from expense_agent.ml.serving import AnomalyResult
from expense_agent.workflow import submit as wf
from expense_agent.workflow.queries import Employee

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=LONDON)
EMPLOYEE = Employee(employee_id=7, employee_code="E0007", grade="senior", is_active=True)

CATEGORIES = [
    ("meals", False, Decimal("10.00")),
    ("hotel", True, Decimal("0.00")),
    ("office_supplies", False, Decimal("0.00")),
]
LIMITS = {
    ("meals", "senior"): (Decimal("60.00"), Decimal("400.00")),
    ("hotel", "senior"): (Decimal("170.00"), Decimal("1500.00")),
    ("office_supplies", "senior"): (Decimal("80.00"), Decimal("200.00")),
}
CITY_CAPS = {("london", "senior"): ("London", Decimal("230.00"))}


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return self.rows


class FakeWriterConn:
    def __init__(
        self,
        month_total: Decimal | int = Decimal("0"),
        duplicates: int = 0,
        split: tuple[int, Decimal | int] = (0, Decimal("0")),
    ) -> None:
        self.month_total = Decimal(month_total)
        self.duplicates = duplicates
        self.split = (split[0], Decimal(split[1]))
        self.executed: list[tuple[str, Any]] = []
        self.in_transaction = False

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self.in_transaction = True
        yield
        self.in_transaction = False

    def execute(self, sql: str, params: Any = None) -> Result:
        assert self.in_transaction, "workflow SQL must run inside the transaction"
        self.executed.append((sql, params))
        if sql == wf.CATEGORIES_SQL:
            return Result(CATEGORIES)
        if sql == wf.LIMITS_SQL:
            return Result([LIMITS[params]])
        if sql == wf.CITY_CAP_SQL:
            hit = CITY_CAPS.get((params[0].lower(), params[1]))
            return Result([hit] if hit else [])
        if sql == wf.MONTH_TOTAL_SQL:
            return Result([(self.month_total,)])
        if sql == wf.DUPLICATE_SQL:
            return Result([(self.duplicates,)])
        if sql == wf.SPLIT_SQL:
            return Result([self.split])
        if sql == wf.NEXT_ID_SQL:
            return Result([(4321,)])
        return Result([])

    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]

    def inserts(self) -> list[str]:
        return [s for s in self.statements() if s.lstrip().upper().startswith("INSERT")]


def anomaly(label: str = "normal", score: float = 0.4) -> AnomalyResult:
    return AnomalyResult(score, label, ["spent at a weekend"] if label == "high" else [], "v1")


def fixed_scorer(result: AnomalyResult) -> wf.Scorer:
    return lambda conn, claim, employee, is_travel, now: result


@pytest.fixture
def lunch() -> dict[str, Any]:
    return {
        "category": "meals",
        "amount": 18.5,
        "merchant": "Copperleaf Kitchen",
        "incurred_at": "2026-11-22",
        "description": "working lunch",
        "receipt_s3_key": "samples/receipts/r01.png",
    }


@pytest.fixture
def hotel() -> dict[str, Any]:
    return {
        "category": "hotel",
        "amount": 400.0,
        "merchant": "Riverside Lodge Hotel",
        "incurred_at": "2026-11-20",
        "description": "client workshop",
        "receipt_s3_key": "samples/receipts/r03.png",
        "city": "London",
        "nights": 2,
    }
