"""Read-only query tools: scoping by employee, period bounds, input checks."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from expense_agent.workflow import queries as q

from .conftest import NOW, Result


class FakeReadConn:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Result:
        self.executed.append((sql, params))
        return Result(self.rows)


def test_get_my_claims_is_scoped_and_filtered() -> None:
    row = (
        1,
        "CLM-2026-000001",
        "meals",
        Decimal("18.5"),
        "GBP",
        NOW,
        NOW,
        "Cafe",
        "approved",
        None,
        None,
    )
    conn = FakeReadConn([row])
    out = q.get_my_claims(conn, 7, "approved", "2026-11-01", "2026-11-30")
    sql, params = conn.executed[0]
    assert sql == q.MY_CLAIMS_SQL and params["employee_id"] == 7
    assert params["from"] == date(2026, 11, 1) and params["limit"] == q.MAX_CLAIMS
    assert out["claims"][0]["amount"] == "18.50" and out["count"] == 1


def test_get_my_claims_rejects_bad_filters() -> None:
    with pytest.raises(ValueError, match="status"):
        q.get_my_claims(FakeReadConn(), 7, status="paid")
    with pytest.raises(ValueError, match="date"):
        q.get_my_claims(FakeReadConn(), 7, date_from="last week")


def test_claim_status_not_found_and_not_yours_look_the_same() -> None:
    conn = FakeReadConn([])
    out = q.get_claim_status(conn, 7, "CLM-2026-000001")
    assert out == {"found": False, "message": q.NOT_FOUND}
    params = conn.executed[0][1]
    assert params == {"employee_id": 7, "claim_id": None, "claim_ref": "CLM-2026-000001"}


def test_claim_status_with_decision() -> None:
    row = (
        5,
        "CLM-2026-000005",
        "taxi",
        Decimal("24"),
        "GBP",
        NOW,
        NOW,
        "Cabs",
        "approved",
        0.31,
        "system",
        "approved",
        "Within policy",
        NOW,
    )
    out = q.get_claim_status(FakeReadConn([row]), 7, 5)
    assert out["found"] and out["latest_decision"]["by"] == "system"


def test_claim_identifier_parsing() -> None:
    assert q.parse_claim_identifier("1234") == (1234, None)
    assert q.parse_claim_identifier("clm-2026-000001") == (None, "CLM-2026-000001")
    with pytest.raises(ValueError):
        q.parse_claim_identifier("1; DROP TABLE claims")


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("this_month", date(2026, 11, 1), date(2026, 12, 1)),
        ("last_month", date(2026, 10, 1), date(2026, 11, 1)),
        ("this_quarter", date(2026, 10, 1), date(2027, 1, 1)),
        ("this_year", date(2026, 1, 1), date(2027, 1, 1)),
        ("last_12_months", date(2025, 12, 1), date(2026, 12, 1)),
    ],
)
def test_period_bounds(period: str, start: date, end: date) -> None:
    assert q.period_bounds(period, date(2026, 11, 25)) == (start, end)


def test_period_bounds_january_wraps() -> None:
    assert q.period_bounds("last_month", date(2027, 1, 10)) == (date(2026, 12, 1), date(2027, 1, 1))


def test_spending_summary_uses_fixed_sql_per_group() -> None:
    conn = FakeReadConn([("meals", 2, Decimal("30.5")), ("taxi", 1, Decimal("24"))])
    out = q.spending_summary(conn, 7, "this_month", "category", now=NOW)
    assert conn.executed[0] == (
        q.SUMMARY_BY_CATEGORY_SQL,
        (7, date(2026, 11, 1), date(2026, 12, 1)),
    )
    assert out["total"] == "54.50" and out["groups"][0]["category"] == "meals"
    q.spending_summary(conn, 7, "this_year", "month", now=datetime(2026, 3, 1))
    assert conn.executed[1][0] == q.SUMMARY_BY_MONTH_SQL


def test_spending_summary_rejects_unknown_options() -> None:
    with pytest.raises(ValueError):
        q.spending_summary(FakeReadConn(), 7, "forever", "category")
    with pytest.raises(ValueError):
        q.spending_summary(FakeReadConn(), 7, "this_month", "merchant; --")


def test_every_query_is_scoped_by_employee() -> None:
    for sql in (q.MY_CLAIMS_SQL, q.CLAIM_STATUS_SQL, *q.SUMMARY_SQL.values()):
        assert "employee_id = %" in sql


def test_latest_means_most_recently_submitted() -> None:
    assert "ORDER BY submitted_at DESC, claim_id DESC" in q.MY_CLAIMS_SQL


def test_claims_carry_friendly_dates_and_an_empty_filter_note() -> None:
    from datetime import UTC

    when = datetime(2026, 10, 26, 14, 26, tzinfo=UTC)
    row = (
        1,
        "CLM-2026-000001",
        "meals",
        Decimal("9"),
        "GBP",
        when,
        when,
        "Cafe",
        "approved",
        None,
        None,
    )
    out = q.get_my_claims(FakeReadConn([row]), 7)
    first = out["claims"][0]
    assert first["submitted_display"] == "26 Oct 2026, 14:26"
    assert "filters" not in out and "note" not in out
    empty = q.get_my_claims(FakeReadConn([]), 7, date_from="2026-11-01")
    assert empty["filters"] == {"date_from": "2026-11-01"}
    assert "without filters" in empty["note"]


def test_friendly_uses_london_time() -> None:
    from datetime import UTC

    assert q.friendly(datetime(2026, 7, 1, 9, 5, tzinfo=UTC)) == "1 Jul 2026, 10:05"  # BST
    assert q.friendly(None) is None
