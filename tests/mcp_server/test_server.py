"""MCP server: auth middleware, identity from the header, tool schemas, end-to-end over HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from starlette.testclient import TestClient

from expense_agent.mcp_server import auth, tools
from expense_agent.mcp_server.server import build_server
from expense_agent.rag.search import Passage
from expense_agent.workflow import queries

TOKEN = "t" * 48
TOOLS = {
    "search_policy",
    "score_claim_anomaly",
    "extract_receipt",
    "get_my_claims",
    "get_claim_status",
    "spending_summary",
    "submit_claim",
}
NOW = datetime(2026, 11, 20, 12)


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return self.rows


class FakeConn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def execute(self, sql: str, params: Any = None) -> Result:
        self.log.append((sql, params))
        if sql == queries.EMPLOYEE_SQL:
            active = params[0] != 99
            return Result([(params[0], f"E{params[0]:04d}", "senior", active)])
        if sql == queries.MY_CLAIMS_SQL:
            return Result(
                [
                    (
                        1,
                        "CLM-1",
                        "meals",
                        Decimal("9"),
                        "GBP",
                        NOW,
                        NOW,
                        "Cafe",
                        "approved",
                        None,
                        None,
                    )
                ]
            )
        return Result([])


class FakeServices:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.roles: list[str] = []

    @contextlib.contextmanager
    def db(self, role: str) -> Iterator[FakeConn]:
        self.roles.append(role)
        yield FakeConn(self.log)

    def client(self, name: str) -> Any:
        raise AssertionError("no AWS clients in these tests")

    def embed(self, text: str) -> list[float]:
        return [0.0]

    def model(self) -> Any:
        raise AssertionError("no model in these tests")


def scope(headers: dict[str, str] | list[tuple[str, str]], path: str = "/mcp") -> dict:
    items = headers.items() if isinstance(headers, dict) else headers
    return {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": [(k.lower().encode(), v.encode()) for k, v in items],
    }


GOOD = {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "7"}


def test_check_request_accepts_token_and_employee() -> None:
    assert auth.check_request(scope(GOOD), TOKEN) == 7


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Employee-Id": "7"},
        {"Authorization": "Bearer wrong" + "x" * 44, "X-Employee-Id": "7"},
        {"Authorization": TOKEN, "X-Employee-Id": "7"},  # no Bearer prefix
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "seven"},
        {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "0"},
        {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "-7"},
        {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "99999999999"},
        {"Authorization": f"Bearer {TOKEN}", "X-Employee-Id": "²"},  # superscript two
        [("Authorization", f"Bearer {TOKEN}"), ("X-Employee-Id", "7"), ("X-Employee-Id", "8")],
    ],
)
def test_check_request_rejects(headers: Any) -> None:
    with pytest.raises(auth.AuthError):
        auth.check_request(scope(headers), TOKEN)


def test_middleware_refuses_short_tokens_and_hides_the_token() -> None:
    with pytest.raises(auth.AuthError):
        auth.InternalAuthMiddleware(lambda s, r, w: None, "short")  # type: ignore[arg-type,return-value]
    mw = auth.InternalAuthMiddleware(lambda s, r, w: None, TOKEN)  # type: ignore[arg-type,return-value]
    assert TOKEN not in repr(mw)


def test_load_token_reads_the_named_secret() -> None:
    class SM:
        exceptions = type("E", (), {"ResourceNotFoundException": KeyError})

        def get_secret_value(self, SecretId: str) -> dict:  # noqa: N803
            assert SecretId == auth.DEFAULT_TOKEN_SECRET_ID
            return {"SecretString": json.dumps({auth.TOKEN_KEY: TOKEN})}

    class Session:
        def client(self, name: str) -> SM:
            return SM()

    assert auth.load_token(Session()) == TOKEN


def test_tool_list_and_no_identity_arguments() -> None:
    tools_ = asyncio.run(build_server(FakeServices()).list_tools())
    assert {t.name for t in tools_} == TOOLS
    banned = ("employee", "user", "identity", "token", "ctx", "manager", "approve")
    for t in tools_:
        for prop in t.inputSchema.get("properties", {}):
            assert not any(b in prop.lower() for b in banned), (t.name, prop)
    assert not any("approve" in t.name or "reject" in t.name for t in tools_)


def test_policy_result_not_covered_rule() -> None:
    low = [Passage("EXP-1.1", 0, "Scope", "text", "2026.1", 0.30, "vector")]
    high = [Passage("EXP-6.3", 0, "Hotels", "text", "2026.1", 0.52, "vector")]
    exact = [Passage("EXP-9.2", 0, "Manager", "text", "2026.1", 1.0, "exact_id"), *low]
    assert tools.policy_result(low)["not_covered"] is True
    assert tools.policy_result(high)["not_covered"] is False
    assert tools.policy_result(exact)["not_covered"] is False
    assert tools.policy_result([])["not_covered"] is True
    assert tools.policy_result(high)["passages"][0]["section_id"] == "EXP-6.3"


def test_inactive_employee_is_refused() -> None:
    with pytest.raises(tools.ToolInputError):
        tools.employee_from_db(FakeServices(), 99)


def test_search_policy_rejects_empty_or_huge_questions() -> None:
    for q in ("", "x" * 1001):
        with pytest.raises(tools.ToolInputError):
            tools.tool_search_policy(FakeServices(), q)


# ---- end to end through the real MCP app (streamable HTTP, stateless, JSON responses) ----

RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


@pytest.fixture
def client_and_services() -> Iterator[tuple[TestClient, FakeServices]]:
    services = FakeServices()
    app = auth.InternalAuthMiddleware(build_server(services).streamable_http_app(), TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield client, services


def test_http_rejects_missing_token(client_and_services: tuple[TestClient, FakeServices]) -> None:
    client, services = client_and_services
    r = client.post("/mcp", json=rpc("tools/list"), headers=RPC_HEADERS)
    assert r.status_code == 401 and services.log == []


def test_http_health_is_open(client_and_services: tuple[TestClient, FakeServices]) -> None:
    client, _ = client_and_services
    assert client.get("/health").json() == {"status": "ok"}


def test_http_tool_call_uses_header_identity(
    client_and_services: tuple[TestClient, FakeServices],
) -> None:
    client, services = client_and_services
    headers = {**RPC_HEADERS, **GOOD}
    call = rpc("tools/call", {"name": "get_my_claims", "arguments": {"status": "approved"}})
    r = client.post("/mcp", json=call, headers=headers)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    assert result["structuredContent"]["count"] == 1
    claims_call = next(p for sql, p in services.log if sql == queries.MY_CLAIMS_SQL)
    assert claims_call["employee_id"] == 7  # from X-Employee-Id, not from the model
    assert set(services.roles) == {"readonly"}


def test_http_bad_arguments_become_tool_errors(
    client_and_services: tuple[TestClient, FakeServices],
) -> None:
    client, _ = client_and_services
    call = rpc("tools/call", {"name": "get_claim_status", "arguments": {"claim": "1; DROP"}})
    r = client.post("/mcp", json=call, headers={**RPC_HEADERS, **GOOD})
    assert r.json()["result"]["isError"] is True
