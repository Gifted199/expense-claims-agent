"""score_claim_anomaly tool: ownership scoping and argument checks (no cloud)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from expense_agent.mcp_server import tools
from expense_agent.ml.records import LOAD_CLAIMS_SQL, LOAD_LIMITS_SQL
from expense_agent.workflow import submit
from expense_agent.workflow.queries import NOT_FOUND, Employee

EMPLOYEE = Employee(7, "E0007", "senior", True)
NOW = datetime(2026, 11, 25, 15, tzinfo=UTC)


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return self.rows


class Conn:
    def __init__(self, owned: bool, log: list[str]) -> None:
        self.owned = owned
        self.log = log

    def execute(self, sql: str, params: Any = None) -> Result:
        self.log.append(sql)
        if sql == tools.OWNED_CLAIM_SQL:
            assert params == (42, EMPLOYEE.employee_id)  # scoped by the session employee
            return Result([(1,)] if self.owned else [])
        if sql == submit.CATEGORIES_SQL:
            return Result([("meals", False, Decimal("10.00"))])
        if sql == LOAD_CLAIMS_SQL:
            return Result([])
        if sql == LOAD_LIMITS_SQL:
            return Result([("meals", "senior", Decimal("60.00"))])
        raise AssertionError(f"unexpected SQL: {sql}")


class Services:
    def __init__(self, owned: bool = False) -> None:
        self.owned = owned
        self.log: list[str] = []
        self.model_loaded = False

    @contextlib.contextmanager
    def db(self, role: str) -> Iterator[Conn]:
        assert role == "readonly"
        yield Conn(self.owned, self.log)

    def client(self, name: str) -> Any:
        raise AssertionError("no AWS")

    def embed(self, text: str) -> list[float]:
        raise AssertionError("no Azure")

    def model(self) -> Any:
        self.model_loaded = True
        raise RuntimeError("model reached")  # stands in for scoring


def test_someone_elses_claim_is_not_found_and_nothing_is_loaded() -> None:
    services = Services(owned=False)
    out = tools.tool_score_claim_anomaly(services, EMPLOYEE, claim_id=42)
    assert out == {"found": False, "message": NOT_FOUND}
    assert services.log == [tools.OWNED_CLAIM_SQL]  # no claim history was read
    assert not services.model_loaded


def test_own_claim_goes_on_to_scoring() -> None:
    services = Services(owned=True)
    with pytest.raises(ValueError):  # empty history: claim 42 has no feature row
        tools.tool_score_claim_anomaly(services, EMPLOYEE, claim_id=42)
    assert LOAD_CLAIMS_SQL in services.log


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"category": "meals", "amount": 10.0, "merchant": "Cafe"},  # no date
        {"category": "crypto", "amount": 10.0, "merchant": "Cafe", "incurred_at": "2026-11-20"},
        {"category": "meals", "amount": -1.0, "merchant": "Cafe", "incurred_at": "2026-11-20"},
        {"category": "meals", "amount": 10.0, "merchant": "Cafe", "incurred_at": "soon"},
    ],
)
def test_new_claim_arguments_are_checked(args: dict) -> None:
    services = Services()
    with pytest.raises(tools.ToolInputError):
        tools.tool_score_claim_anomaly(services, EMPLOYEE, now=NOW, **args)
    assert not services.model_loaded


def test_valid_new_claim_reaches_the_model() -> None:
    services = Services()
    args = {"category": "Meals", "amount": 12.5, "merchant": "Cafe", "incurred_at": "2026-11-20"}
    with pytest.raises(RuntimeError, match="model reached"):
        tools.tool_score_claim_anomaly(services, EMPLOYEE, now=NOW, **args)
