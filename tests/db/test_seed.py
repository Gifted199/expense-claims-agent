"""Seed logic with a fake connection (no database)."""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from expense_agent.db import seed as seed_mod
from expense_agent.db.synthetic import generate


@pytest.fixture(scope="module")
def ds():  # type: ignore[no-untyped-def]
    return generate(42)


def test_every_row_matches_its_placeholders(ds) -> None:  # type: ignore[no-untyped-def]
    for statement, rows in seed_mod.rows_for(ds).items():
        placeholders = statement.count("%s")
        assert rows, statement
        assert all(len(r) == placeholders for r in rows), statement


def test_insert_order_respects_foreign_keys(ds) -> None:  # type: ignore[no-untyped-def]
    tables = [st.split()[2] for st in seed_mod.rows_for(ds)]
    assert tables == [
        "departments",
        "expense_categories",
        "policy_limits",
        "employees",
        "claims",
        "approvals",
        "claim_labels",
    ]


def test_claim_columns_line_up(ds) -> None:  # type: ignore[no-untyped-def]
    statement = seed_mod.INSERT_CLAIM
    columns = statement[statement.index("(") + 1 : statement.index(")")].replace(" ", "").split(",")
    first = seed_mod.rows_for(ds)[statement][0]
    row = dict(zip(columns, first, strict=True))
    claim = ds.claims[0]
    assert row["claim_id"] == claim.claim_id
    assert row["amount"] == claim.amount
    assert row["status"] == claim.status
    assert row["incurred_at"] == claim.incurred_at


class FakeConn:
    def __init__(self, existing_claims: int) -> None:
        self.existing = existing_claims
        self.executed: list[Any] = []
        self.cursor_mock = MagicMock()

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def execute(self, query: Any, params: Any = None) -> MagicMock:
        self.executed.append(query)
        result = MagicMock()
        result.fetchone.return_value = (self.existing,)
        return result

    def cursor(self) -> contextlib.AbstractContextManager[MagicMock]:
        return contextlib.nullcontext(self.cursor_mock)


def test_refuses_when_claims_exist_without_reset(ds) -> None:  # type: ignore[no-untyped-def]
    conn = FakeConn(existing_claims=10)
    with pytest.raises(SystemExit, match="--reset"):
        seed_mod.seed(conn, ds, reset=False)
    conn.cursor_mock.executemany.assert_not_called()


def test_reset_truncates_then_loads(ds) -> None:  # type: ignore[no-untyped-def]
    conn = FakeConn(existing_claims=10)
    counts = seed_mod.seed(conn, ds, reset=True)
    assert any("TRUNCATE" in repr(q) for q in conn.executed)
    assert counts["claims"] == len(ds.claims)
    assert counts["claim_labels"] == len(ds.labels)
    assert conn.cursor_mock.executemany.call_count == 7


def test_empty_database_loads_without_truncate(ds) -> None:  # type: ignore[no-untyped-def]
    conn = FakeConn(existing_claims=0)
    seed_mod.seed(conn, ds, reset=False)
    assert not any("TRUNCATE" in repr(q) for q in conn.executed)
    audit_calls = [
        c for c in conn.cursor_mock.execute.call_args_list if c.args[0] == seed_mod.INSERT_AUDIT
    ]
    assert len(audit_calls) == 1
