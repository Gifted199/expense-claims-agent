"""Agent API with a fake backend: identity, chat, upload checks, claims, manager endpoints."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from expense_agent.agent.graph import TurnResult
from expense_agent.agent.memory import thread_key
from expense_agent.api import db
from expense_agent.api.app import DEMO_HEADER, create_app
from expense_agent.api.uploads import MAX_UPLOAD_BYTES, check_upload, upload_key

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
PEOPLE = {
    1: db.Profile(1, "E0001", "Zane Ellison", "director", True, True),
    6: db.Profile(6, "E0006", "Jonah Quarry", "senior", True, False),
    9: db.Profile(9, "E0009", "Left Company", "senior", False, False),
}


class FakeBackend:
    def __init__(self) -> None:
        self.chats: list[tuple[int, str, str]] = []
        self.tool_calls: list[tuple[int, str, dict]] = []
        self.puts: list[tuple[str, int, str]] = []
        self.decisions: list[tuple] = []

    def profile(self, employee_id: int) -> db.Profile | None:
        return PEOPLE.get(employee_id)

    def directory(self) -> list[db.Profile]:
        return [p for p in PEOPLE.values() if p.is_active]

    async def chat(self, employee_id: int, thread_id: str, message: str) -> TurnResult:
        thread_key(employee_id, thread_id)  # same validation as the real runner
        self.chats.append((employee_id, thread_id, message))
        return TurnResult("hello", ["search_policy"], [])

    async def call_tool(self, employee_id: int, name: str, args: dict[str, Any]) -> dict:
        self.tool_calls.append((employee_id, name, args))
        return {"claims": [], "count": 0} if name == "get_my_claims" else {"vendor": "CityCab"}

    def put_receipt(self, key: str, body: bytes, content_type: str) -> None:
        self.puts.append((key, len(body), content_type))

    def manager_queue(self, manager_id: int) -> list[dict[str, Any]]:
        return [{"claim_id": 5, "review_reasons": ["over cap"]}]

    def decide(self, manager_id: int, claim_id: int, decision: str, reason: str) -> dict | None:
        db.validate_decision(decision, reason)
        self.decisions.append((manager_id, claim_id, decision, reason))
        return None if claim_id == 999 else {"claim_id": claim_id, "status": decision}


@pytest.fixture
def setup() -> Iterator[tuple[TestClient, FakeBackend]]:
    backend = FakeBackend()
    with TestClient(create_app(backend, demo_auth=True)) as client:
        yield client, backend


def as_user(employee_id: int) -> dict[str, str]:
    return {DEMO_HEADER: str(employee_id)}


def test_health_is_open(setup: tuple[TestClient, FakeBackend]) -> None:
    client, _ = setup
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("header", [None, "", "abc", "6; DROP", "1e3", "-6", "9", "404"])
def test_identity_is_required_and_must_be_an_active_employee(
    setup: tuple[TestClient, FakeBackend], header: str | None
) -> None:
    client, _ = setup
    headers = {} if header is None else {DEMO_HEADER: header}
    assert client.get("/me", headers=headers).status_code == 401


def test_demo_header_is_ignored_when_demo_auth_is_off() -> None:
    with TestClient(create_app(FakeBackend(), demo_auth=False)) as client:
        assert client.get("/me", headers=as_user(6)).status_code == 401
        assert client.get("/demo/employees").status_code == 404


def test_me_and_directory(setup: tuple[TestClient, FakeBackend]) -> None:
    client, _ = setup
    me = client.get("/me", headers=as_user(1)).json()
    assert me["is_manager"] is True and me["employee_code"] == "E0001"
    people = client.get("/demo/employees").json()
    assert [p["employee_id"] for p in people] == [1, 6]  # inactive people are not offered


def test_chat_passes_the_session_identity(setup: tuple[TestClient, FakeBackend]) -> None:
    client, backend = setup
    thread = str(uuid.uuid4())
    body = {"thread_id": thread, "message": "hi", "employee_id": 1}  # extra field is ignored
    r = client.post("/chat", json=body, headers=as_user(6))
    assert r.status_code == 200 and r.json()["reply"] == "hello"
    assert backend.chats == [(6, thread, "hi")]


def test_chat_rejects_a_non_uuid_thread(setup: tuple[TestClient, FakeBackend]) -> None:
    client, _ = setup
    r = client.post("/chat", json={"thread_id": "e1:abc", "message": "hi"}, headers=as_user(6))
    assert r.status_code == 422


def test_upload_stores_under_own_prefix_and_returns_ocr(
    setup: tuple[TestClient, FakeBackend],
) -> None:
    client, backend = setup
    files = {"file": ("../../evil.exe", PNG, "application/octet-stream")}
    out = client.post("/upload", files=files, headers=as_user(6)).json()
    assert out["s3_key"].startswith("uploads/E0006/") and out["s3_key"].endswith(".png")
    assert "evil" not in out["s3_key"]
    assert backend.puts[0][2] == "image/png"
    assert backend.tool_calls == [(6, "extract_receipt", {"s3_key": out["s3_key"]})]
    assert out["ocr"]["vendor"] == "CityCab"


@pytest.mark.parametrize(
    "body",
    [b"", b"MZ\x90\x00 not an image", b"<html>", b"\x89PNG\r\n\x1a\n" + b"0" * MAX_UPLOAD_BYTES],
)
def test_upload_rejects_bad_files(setup: tuple[TestClient, FakeBackend], body: bytes) -> None:
    client, backend = setup
    r = client.post("/upload", files={"file": ("r.png", body, "image/png")}, headers=as_user(6))
    assert r.status_code == 422 and backend.puts == []


def test_upload_helpers() -> None:
    assert check_upload(b"%PDF-1.7 ...") == ("pdf", "application/pdf")
    assert check_upload(b"\xff\xd8\xff\xe0 jpeg") == ("jpg", "image/jpeg")
    assert upload_key("E0006", "png").startswith("uploads/E0006/")


def test_claims_uses_the_mcp_tool_for_the_session_user(
    setup: tuple[TestClient, FakeBackend],
) -> None:
    client, backend = setup
    client.get("/claims", params={"status": "approved"}, headers=as_user(6))
    assert backend.tool_calls == [(6, "get_my_claims", {"status": "approved"})]


def test_manager_endpoints_are_for_managers_only(setup: tuple[TestClient, FakeBackend]) -> None:
    client, backend = setup
    assert client.get("/manager/queue", headers=as_user(6)).status_code == 403
    r = client.post(
        "/manager/claims/5/decision",
        json={"decision": "approved", "reason": "ok"},
        headers=as_user(6),
    )
    assert r.status_code == 403 and backend.decisions == []
    assert client.get("/manager/queue", headers=as_user(1)).json()["count"] == 1


def test_manager_decision(setup: tuple[TestClient, FakeBackend]) -> None:
    client, backend = setup
    ok = client.post(
        "/manager/claims/5/decision",
        json={"decision": "rejected", "reason": "Over cap"},
        headers=as_user(1),
    )
    assert ok.json() == {"claim_id": 5, "status": "rejected"}
    assert backend.decisions == [(1, 5, "rejected", "Over cap")]
    other = client.post(
        "/manager/claims/999/decision",
        json={"decision": "approved", "reason": "x"},
        headers=as_user(1),
    )
    assert other.status_code == 404
    bad = client.post(
        "/manager/claims/5/decision", json={"decision": "maybe", "reason": "x"}, headers=as_user(1)
    )
    assert bad.status_code == 422
    empty = client.post(
        "/manager/claims/5/decision",
        json={"decision": "approved", "reason": ""},
        headers=as_user(1),
    )
    assert empty.status_code == 422


# ---- manager decision SQL against a fake writer connection ----------------------------------


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return self.rows


class WriterConn:
    def __init__(self, pending: bool) -> None:
        self.pending = pending
        self.executed: list[tuple[str, Any]] = []
        self.open = False

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self.open = True
        yield
        self.open = False

    def execute(self, sql: str, params: Any = None) -> Result:
        assert self.open
        self.executed.append((sql, params))
        if sql == db.LOCK_PENDING_CLAIM_SQL:
            return Result([(params[0], datetime(2026, 11, 20, 9))] if self.pending else [])
        return Result([])


def test_decide_writes_status_approval_and_audit_in_order() -> None:
    conn = WriterConn(pending=True)
    out = db.decide(conn, 1, 5, "Approved", " fine ", datetime(2026, 11, 26, 10))
    assert out == {"claim_id": 5, "status": "approved", "reason": "fine"}
    sqls = [s for s, _ in conn.executed]
    assert sqls == [
        db.LOCK_PENDING_CLAIM_SQL,
        db.UPDATE_STATUS_SQL,
        db.INSERT_MANAGER_APPROVAL_SQL,
        db.INSERT_DECISION_AUDIT_SQL,
    ]
    assert conn.executed[0][1] == (5, 1)  # only a pending claim of the caller's direct report
    assert conn.executed[2][1][1] == 1  # decided_by = the manager


def test_decide_on_someone_elses_claim_changes_nothing() -> None:
    conn = WriterConn(pending=False)
    assert db.decide(conn, 1, 5, "approved", "ok", datetime(2026, 11, 26)) is None
    assert [s for s, _ in conn.executed] == [db.LOCK_PENDING_CLAIM_SQL]


def test_api_sql_is_constant_and_parameterised() -> None:
    statements = [v for k, v in vars(db).items() if k.endswith("_SQL")]
    assert len(statements) >= 7
    for sql in statements:
        assert "{" not in sql
    assert "manager_id = %s" in db.QUEUE_SQL and "manager_id = %s" in db.LOCK_PENDING_CLAIM_SQL
