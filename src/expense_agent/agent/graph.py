"""One agent, built per request (decision D39).

Each turn: load the MCP tools with **this employee's** headers (internal token + X-Employee-Id
from the API session, never from the LLM), build ``create_agent`` with the guardrail middleware
and the Postgres checkpointer, and run one turn. Nothing conversational is kept in process
memory, so a restarted API continues the same thread from RDS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from expense_agent.agent.memory import thread_key
from expense_agent.agent.prompt import build_system_prompt
from expense_agent.guardrails.middleware import (
    EmptyClaimsRetry,
    InputGuardrail,
    OutputGuardrail,
    current_turn,
)
from expense_agent.guardrails.rules import text_of

log = logging.getLogger(__name__)
RECURSION_LIMIT = 30

ToolLoader = Callable[[int], Awaitable[Sequence[Any]]]


@dataclass(frozen=True)
class TurnResult:
    reply: str
    tools_used: list[str]
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"reply": self.reply, "tools_used": self.tools_used, "flags": self.flags}


def mcp_tool_loader(url: str, token: str) -> ToolLoader:
    """Tools whose every call carries the internal token and the session's employee ID."""

    async def load(employee_id: int) -> Sequence[Any]:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        headers = {"Authorization": f"Bearer {token}", "X-Employee-Id": str(int(employee_id))}
        client = MultiServerMCPClient(
            {"expense": {"transport": "streamable_http", "url": url, "headers": headers}}
        )
        return await client.get_tools()

    return load


@dataclass
class AgentRunner:
    model: Any
    load_tools: ToolLoader
    checkpointer: Any

    async def turn(
        self, employee_id: int, thread_id: str, message: str, now: datetime | None = None
    ) -> TurnResult:
        from langchain.agents import create_agent

        started = time.monotonic()
        key = thread_key(employee_id, thread_id)  # validates the UUID first
        inp, out, claims_retry = InputGuardrail(), OutputGuardrail(), EmptyClaimsRetry()
        tools = await self.load_tools(employee_id)
        agent = create_agent(
            self.model,
            list(tools),
            system_prompt=build_system_prompt(now),
            middleware=[inp, out, claims_retry],
            checkpointer=self.checkpointer,
        )
        state = await agent.ainvoke(
            {"messages": [HumanMessage(content=message)]},
            {"configurable": {"thread_id": key}, "recursion_limit": RECURSION_LIMIT},
        )
        messages = state["messages"]
        _, turn = current_turn(messages)
        final = next((m for m in reversed(turn) if isinstance(m, AIMessage)), None)
        result = TurnResult(
            reply=text_of(final.content) if final else "",
            tools_used=[m.name or "" for m in turn if isinstance(m, ToolMessage)],
            flags=[*inp.flags, *claims_retry.flags, *out.flags],
        )
        log.info(  # no message text (PII) and no token in logs
            json.dumps(
                {
                    "event": "agent_turn",
                    "employee_id": employee_id,
                    "thread": hashlib.sha256(key.encode()).hexdigest()[:12],
                    "tools": result.tools_used,
                    "flags": result.flags,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
        )
        return result
