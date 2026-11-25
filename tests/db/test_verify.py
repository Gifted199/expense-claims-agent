"""verify.security_checks pass/fail logic with a fake connection."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from expense_agent.db import verify


class FakeConn:
    """Answers privilege queries from the expected matrix, optionally flipping one."""

    def __init__(self, flip: tuple[Any, ...] | None = None, over_auto: int = 0) -> None:
        self.flip = flip
        self.over_auto = over_auto

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> MagicMock:
        result = MagicMock()
        if "has_table_privilege" in query:
            expected = {m[:3]: m[3] for m in verify.PRIVILEGE_MATRIX}[params]  # type: ignore[index]
            value = (not expected) if params == self.flip else expected
        elif "has_column_privilege" in query:
            expected = {m[:4]: m[4] for m in verify.COLUMN_PRIVILEGES}[params]  # type: ignore[index]
            value = (not expected) if params == self.flip else expected
        elif "pg_extension" in query or "policy_documents" in query:
            value = 1
        else:
            value = self.over_auto
        result.fetchone.return_value = (value,)
        return result


def test_all_pass_when_database_matches_policy() -> None:
    results = verify.security_checks(FakeConn())
    assert results and all(passed for _, passed in results)


def test_extra_writer_privilege_is_a_failure() -> None:
    results = verify.security_checks(FakeConn(flip=("app_writer", "approvals", "UPDATE")))
    failed = [d for d, passed in results if not passed]
    assert failed == ["app_writer cannot UPDATE approvals"]


def test_writer_amount_column_update_is_a_failure() -> None:
    results = verify.security_checks(FakeConn(flip=("app_writer", "claims", "amount", "UPDATE")))
    assert [d for d, p in results if not p] == ["app_writer cannot UPDATE claims.amount"]


def test_auto_approved_over_limit_claim_is_a_failure() -> None:
    results = verify.security_checks(FakeConn(over_auto=3))
    assert [d for d, p in results if not p] == [
        "no over-limit claim (per-claim or monthly) was auto-approved (3)"
    ]


def test_policy_matrix_covers_the_rules_we_promised() -> None:
    m = {r[:3]: r[3] for r in verify.PRIVILEGE_MATRIX}
    assert m[("app_readonly", "claim_labels", "SELECT")] is False
    assert m[("app_writer", "claim_labels", "SELECT")] is False
    assert m[("app_writer", "audit_log", "UPDATE")] is False
    assert m[("app_writer", "approvals", "UPDATE")] is False
    deletes = {k for k, expected in m.items() if k[2] == "DELETE" and expected}
    assert deletes == {("app_ingest", "policy_chunks", "DELETE")}  # only RAG re-ingest
