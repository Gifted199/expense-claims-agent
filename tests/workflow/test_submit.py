"""The deterministic submit_claim workflow with a fake database (D34, D36)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from expense_agent.workflow import submit as wf

from .conftest import EMPLOYEE, NOW, FakeWriterConn, anomaly, fixed_scorer


def run(conn: FakeWriterConn, raw: dict[str, Any], label: str = "normal") -> dict[str, Any]:
    def no_model() -> None:
        raise AssertionError("the injected scorer is used")

    return wf.submit_claim(conn, EMPLOYEE, raw, no_model, NOW, scorer=fixed_scorer(anomaly(label)))


def failed(out: dict[str, Any]) -> list[str]:
    return [name for name, c in out["checks"].items() if not c["passed"]]


def test_within_policy_is_approved_with_approval_and_audit_rows(lunch: dict) -> None:
    conn = FakeWriterConn()
    out = run(conn, lunch)
    assert out["result"] == "submitted" and out["status"] == "approved"
    assert out["claim_ref"] == "CLM-2026-004321" and failed(out) == []
    assert conn.statements()[0] == wf.LOCK_SQL  # the per-employee lock is taken first
    assert conn.executed[0][1] == (EMPLOYEE.employee_id,)
    assert conn.inserts() == [wf.INSERT_CLAIM_SQL, wf.INSERT_APPROVAL_SQL, wf.INSERT_AUDIT_SQL]
    params = dict(conn.executed)
    assert params[wf.INSERT_CLAIM_SQL][2] == EMPLOYEE.employee_id
    assert params[wf.INSERT_CLAIM_SQL][9] == "approved"
    assert json.loads(params[wf.INSERT_AUDIT_SQL][2])["status"] == "approved"


@pytest.mark.parametrize(
    ("conn_kwargs", "changes", "check", "section"),
    [
        ({}, {"amount": 75.0}, "per_claim_limit", "EXP-4.1"),
        ({"month_total": 390}, {}, "monthly_limit", "EXP-4.2"),
        ({"duplicates": 1}, {}, "duplicate", "EXP-11.1"),
        ({"split": (1, 50)}, {}, "split", "EXP-11.2"),
        ({}, {"incurred_at": "2026-10-01"}, "deadline", "EXP-2.3"),
    ],
)
def test_each_failed_rule_goes_to_the_manager(
    lunch: dict, conn_kwargs: dict, changes: dict, check: str, section: str
) -> None:
    conn = FakeWriterConn(**conn_kwargs)
    out = run(conn, {**lunch, **changes})
    assert out["status"] == "pending_manager"
    assert failed(out) == [check]
    assert section in out["checks"][check]["reason"]
    assert conn.inserts() == [wf.INSERT_CLAIM_SQL, wf.INSERT_AUDIT_SQL]  # no system approval


def test_high_anomaly_goes_to_the_manager_with_reasons(lunch: dict) -> None:
    out = run(FakeWriterConn(), lunch, label="high")
    assert out["status"] == "pending_manager" and failed(out) == ["anomaly"]
    assert "spent at a weekend" in out["reasons"][0]


def test_all_reasons_are_reported_together(lunch: dict) -> None:
    out = run(FakeWriterConn(duplicates=1), {**lunch, "amount": 75.0}, label="high")
    assert set(failed(out)) == {"per_claim_limit", "duplicate", "anomaly"}
    assert len(out["reasons"]) == 3


def test_hotel_over_city_cap(hotel: dict) -> None:
    out = run(FakeWriterConn(), {**hotel, "amount": 520.0})  # 260/night vs 230 London senior
    assert out["status"] == "pending_manager" and failed(out) == ["hotel_nightly_cap"]
    assert "£260.00/night" in out["reasons"][0] and "London" in out["reasons"][0]


def test_hotel_within_city_cap_is_not_held_to_the_uk_wide_limit(hotel: dict) -> None:
    out = run(FakeWriterConn(), hotel)  # 200/night: over UK-wide 170, within London 230
    assert out["status"] == "approved"


def test_unlisted_city_uses_the_uk_wide_hotel_limit(hotel: dict) -> None:
    out = run(FakeWriterConn(), {**hotel, "city": "Bristol"})  # 200/night vs 170
    assert failed(out) == ["hotel_nightly_cap"]
    assert "UK-wide" in out["reasons"][0]


@pytest.mark.parametrize(
    "drop", [("city",), ("nights",), ("city", "nights")], ids=["city", "nights", "both"]
)
def test_hotel_without_city_or_nights_asks_instead_of_guessing(
    hotel: dict, drop: tuple[str, ...]
) -> None:
    raw = {k: v for k, v in hotel.items() if k not in drop}
    conn = FakeWriterConn()
    out = run(conn, raw)
    assert out["result"] == "missing_fields" and out["missing"] == list(drop)
    assert all(word in out["message"] for word in drop)
    assert conn.inserts() == []


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"category": "crypto"}, "category"),
        ({"amount": -5}, "amount"),
        ({"amount": "lots"}, "amount"),
        ({"merchant": "  "}, "merchant"),
        ({"incurred_at": "2026-12-25"}, "incurred_at"),  # future
        ({"incurred_at": "2026-01-01"}, "incurred_at"),  # past the 90-day cutoff
        ({"incurred_at": "yesterday"}, "incurred_at"),
        ({"receipt_s3_key": None}, "receipt_s3_key"),  # required over GBP 10
        ({"receipt_s3_key": "uploads/E0008/r.png"}, "receipt_s3_key"),  # someone else's
    ],
)
def test_invalid_input_writes_nothing(lunch: dict, changes: dict, field: str) -> None:
    conn = FakeWriterConn()
    out = run(conn, {**lunch, **changes})
    assert out["result"] == "invalid" and field in out["errors"]
    assert conn.inserts() == []


def test_hotel_nights_out_of_range_is_invalid(hotel: dict) -> None:
    out = run(FakeWriterConn(), {**hotel, "nights": 0})
    assert out["result"] == "invalid" and "nights" in out["errors"]


def test_identity_comes_only_from_the_session(lunch: dict) -> None:
    conn = FakeWriterConn()
    run(conn, {**lunch, "employee_id": 1, "employee": "E0001"})  # extras are ignored
    assert dict(conn.executed)[wf.INSERT_CLAIM_SQL][2] == EMPLOYEE.employee_id


def test_parse_incurred_at_defaults_to_london_midday() -> None:
    assert wf.parse_incurred_at("2026-11-20").hour == 12
    assert str(wf.parse_incurred_at("2026-11-20T09:30").tzinfo) == "Europe/London"


def test_all_workflow_sql_is_constant_and_parameterised() -> None:
    statements = [v for k, v in vars(wf).items() if k.endswith("_SQL")]
    assert len(statements) >= 10
    for sql in statements:
        assert "{" not in sql and "%(" not in sql


def test_today_as_a_date_is_accepted_before_midday(lunch: dict) -> None:
    morning = NOW.replace(hour=9)
    conn = FakeWriterConn()
    out = wf.submit_claim(
        conn,
        EMPLOYEE,
        {**lunch, "incurred_at": morning.date().isoformat()},
        lambda: None,
        morning,
        scorer=fixed_scorer(anomaly()),
    )
    assert out["result"] == "submitted"
    incurred = dict(conn.executed)[wf.INSERT_CLAIM_SQL][5]
    assert incurred <= morning  # the DB requires incurred_at <= submitted_at


def test_a_time_even_a_minute_ahead_is_refused_not_a_db_error(lunch: dict) -> None:
    ahead = NOW.replace(minute=1).isoformat()
    conn = FakeWriterConn()
    out = run(conn, {**lunch, "incurred_at": ahead})
    assert out["result"] == "invalid" and "incurred_at" in out["errors"]
    assert conn.inserts() == []


def test_review_reasons_are_written_at_insert_only(lunch: dict) -> None:
    held = FakeWriterConn(duplicates=1)
    run(held, lunch)
    params = dict(held.executed)[wf.INSERT_CLAIM_SQL]
    assert params[-1] and "EXP-11.1" in params[-1][0]
    ok = FakeWriterConn()
    run(ok, lunch)
    assert dict(ok.executed)[wf.INSERT_CLAIM_SQL][-1] is None
    for conn in (held, ok):
        assert not any(s.lstrip().upper().startswith("UPDATE") for s in conn.statements())
