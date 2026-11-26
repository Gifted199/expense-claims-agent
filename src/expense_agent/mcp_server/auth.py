"""Internal-only authentication and server-side identity (decision D33).

The agent API (Phase 5) is the only caller. Every HTTP request must carry:

* ``Authorization: Bearer <token>`` where the token is the shared internal secret
  (``akinsds1126-expense-mcp-internal``, key ``MCP_INTERNAL_TOKEN``), and
* ``X-Employee-Id: <id>``, the logged-in employee, set by the agent API from its session.

Anything else gets **401 before any MCP handling**. The employee is never a tool argument, so
the model can't choose whose data it sees. The token is never logged.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from expense_agent.db.connection import get_secret_json

DEFAULT_TOKEN_SECRET_ID = "akinsds1126-expense-mcp-internal"  # noqa: S105 - a *name*
TOKEN_KEY = "MCP_INTERNAL_TOKEN"  # noqa: S105 - the JSON key, not a value
EMPLOYEE_HEADER = "x-employee-id"
AUTH_HEADER = "authorization"
HEALTH_PATH = "/health"
MAX_EMPLOYEE_ID = 2_147_483_647
MIN_TOKEN_LENGTH = 32

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class AuthError(Exception):
    pass


def token_secret_id() -> str:
    return os.environ.get("EXPENSE_MCP_TOKEN_SECRET_ID", DEFAULT_TOKEN_SECRET_ID)


def load_token(session: Any, secret_id: str | None = None) -> str:
    token = str(get_secret_json(session, secret_id or token_secret_id())[TOKEN_KEY])
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthError("internal token is too short")
    return token


def parse_employee_id(value: str | None) -> int:
    """A positive int32 written as plain digits, or AuthError."""
    text = (value or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 10:
        raise AuthError("bad employee header")
    employee_id = int(text)
    if not 0 < employee_id <= MAX_EMPLOYEE_ID:
        raise AuthError("bad employee header")
    return employee_id


def token_matches(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer ") :].encode(), token.encode())


def _header_values(scope: Scope, name: str) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers", []) if k.decode().lower() == name]


def check_request(scope: Scope, token: str) -> int:
    """Return the employee ID if the request is authorised; raise AuthError otherwise.
    Repeated auth or employee headers are rejected, so there's no ambiguity about which wins."""
    auth = _header_values(scope, AUTH_HEADER)
    employee = _header_values(scope, EMPLOYEE_HEADER)
    if len(auth) != 1 or not token_matches(auth[0], token):
        raise AuthError("unauthorised")
    if len(employee) != 1:
        raise AuthError("bad employee header")
    return parse_employee_id(employee[0])


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class InternalAuthMiddleware:
    """Pure ASGI middleware in front of the whole MCP app. ``/health`` is open (no data)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise AuthError("internal token is too short")
        self.app = app
        self._token = token

    def __repr__(self) -> str:
        return "InternalAuthMiddleware(token='***')"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":  # startup/shutdown events pass straight through
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets or other scope types
            return
        if scope.get("path") == HEALTH_PATH and scope.get("method") == "GET":
            await _send_json(send, 200, {"status": "ok"})
            return
        try:
            check_request(scope, self._token)
        except AuthError:
            await _send_json(send, 401, {"error": "unauthorised"})
            return
        await self.app(scope, receive, send)
