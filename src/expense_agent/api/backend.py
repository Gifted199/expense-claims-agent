"""What the agent API needs from the cloud, behind one small interface so tests can fake it."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from expense_agent.agent.graph import AgentRunner, TurnResult
from expense_agent.api import db
from expense_agent.ocr.textract import receipts_bucket

LONDON = ZoneInfo("Europe/London")


class Backend(Protocol):
    def profile(self, employee_id: int) -> db.Profile | None: ...
    def directory(self) -> list[db.Profile]: ...
    async def chat(self, employee_id: int, thread_id: str, message: str) -> TurnResult: ...
    async def call_tool(self, employee_id: int, name: str, args: dict[str, Any]) -> dict: ...
    def put_receipt(self, key: str, body: bytes, content_type: str) -> None: ...
    def manager_queue(self, manager_id: int) -> list[dict[str, Any]]: ...
    def decide(self, manager_id: int, claim_id: int, decision: str, reason: str) -> dict | None: ...


async def call_mcp_tool(
    url: str, token: str, employee_id: int, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Call one MCP tool directly (no LLM), as the session's employee."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}", "X-Employee-Id": str(int(employee_id))}
    async with (
        httpx.AsyncClient(headers=headers, timeout=120) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, args)
    if result.isError:
        text = "".join(getattr(c, "text", "") for c in result.content)
        raise ValueError(text or f"{name} failed")
    if result.structuredContent:
        return dict(result.structuredContent)
    return json.loads("".join(getattr(c, "text", "") for c in result.content))


class CloudBackend:
    """RDS (readonly / writer), S3, the MCP server and the agent runner."""

    def __init__(self, services: Any, runner: AgentRunner, mcp_url: str, token: str) -> None:
        self.services = services  # expense_agent.mcp_server.services.Services
        self.runner = runner
        self.mcp_url = mcp_url
        self._token = token

    def __repr__(self) -> str:
        return f"CloudBackend(mcp_url={self.mcp_url!r}, token='***')"

    def profile(self, employee_id: int) -> db.Profile | None:
        with self.services.db("readonly") as conn:
            return db.load_profile(conn, employee_id)

    def directory(self) -> list[db.Profile]:
        with self.services.db("readonly") as conn:
            return db.directory(conn)

    async def chat(self, employee_id: int, thread_id: str, message: str) -> TurnResult:
        return await self.runner.turn(employee_id, thread_id, message)

    async def call_tool(self, employee_id: int, name: str, args: dict[str, Any]) -> dict:
        return await call_mcp_tool(self.mcp_url, self._token, employee_id, name, args)

    def put_receipt(self, key: str, body: bytes, content_type: str) -> None:
        self.services.client("s3").put_object(
            Bucket=receipts_bucket(), Key=key, Body=body, ContentType=content_type
        )

    def manager_queue(self, manager_id: int) -> list[dict[str, Any]]:
        with self.services.db("readonly") as conn:
            return db.manager_queue(conn, manager_id)

    def decide(self, manager_id: int, claim_id: int, decision: str, reason: str) -> dict | None:
        with self.services.db("writer") as conn:
            return db.decide(conn, manager_id, claim_id, decision, reason, datetime.now(LONDON))
