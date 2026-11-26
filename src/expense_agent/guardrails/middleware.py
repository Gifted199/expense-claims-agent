"""Guardrails as LangChain agent middleware (decision D41). Deterministic: no LLM judges.

* ``InputGuardrail.before_agent``: refuse empty or too-long messages, prompt injection and
  investment-only messages **without calling the LLM**. For a multi-part message with an
  investment part, ``wrap_model_call`` adds a note so the model declines only that part.
* ``OutputGuardrail.after_model`` (final answers only): strip investment advice; require every
  GBP figure to come from this turn's tool results (or the user's own message); require a
  section-ID citation when the policy search covered the question. One retry, then a fallback.

Middleware instances are created per request, so their ``flags`` describe exactly one turn.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, hook_config
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from expense_agent.guardrails import rules

GUARD_NAME = "guardrail"
WITHHELD = "[message withheld by a guardrail]"
MAX_RETRIES = 1
FALLBACK = (
    "I couldn't double-check that answer against the system's own figures and policy "
    "sections, so I won't guess. Please ask again in a different way, or check My claims."
)


def current_turn(messages: list[AnyMessage]) -> tuple[str, list[AnyMessage]]:
    """(the latest user message, every message after it)."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return rules.text_of(messages[i].content), list(messages[i + 1 :])
    return "", list(messages)


class InputGuardrail(AgentMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.flags: list[str] = []

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state["messages"]
        user_text, _ = current_turn(messages)
        verdict = rules.check_input(user_text)
        self.flags.extend(verdict.flags)
        if verdict.action == "refuse":
            # Replace the refused text in memory (same id = replaced by the reducer), so a later
            # turn such as "answer my previous question" can't smuggle it to the model.
            human = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
            withheld = HumanMessage(content=WITHHELD, id=human.id)
            reply = AIMessage(content=verdict.reply or "")
            return {"messages": [withheld, reply], "jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    def _with_note(self, request: ModelRequest) -> ModelRequest:
        user_text, _ = current_turn(request.messages)
        if not rules.check_input(user_text).mixed_investment:
            return request
        base = request.system_message.content if request.system_message else ""
        note = f"{rules.text_of(base)}\n\n{rules.MIXED_INTENT_NOTE}".strip()
        return request.override(system_message=SystemMessage(content=note))

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(self._with_note(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]
    ) -> Any:
        return await handler(self._with_note(request))


class OutputGuardrail(AgentMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self.flags: list[str] = []

    def problems(self, answer: str, user_text: str, turn: list[AnyMessage]) -> list[str]:
        tools = [m for m in turn if isinstance(m, ToolMessage)]
        texts = [rules.text_of(m.content) for m in tools]
        found: list[str] = []
        bad = rules.unverified_money(answer, texts, user_text)
        if bad:
            self.flags.append("unverified_money")
            found.append(
                f"these amounts appear in no tool result: {', '.join(bad)}. Quote amounts exactly "
                "as the tools returned them, and don't compute new totals"
            )
        evidence = rules.policy_evidence(
            (m.name or "", t) for m, t in zip(tools, texts, strict=True)
        )
        if rules.missing_citation(answer, evidence):
            self.flags.append("missing_citation")
            found.append("cite the section_id (for example EXP-6.3) of each passage you relied on")
        return found

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state["messages"]
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # only judge final answers
        user_text, turn = current_turn(messages)
        answer, removed = rules.strip_investment_advice(rules.text_of(last.content))
        if removed:
            self.flags.append("investment_advice_removed")
        problems = self.problems(answer, user_text, turn)
        retries = sum(1 for m in turn if isinstance(m, SystemMessage) and m.name == GUARD_NAME)
        if problems and retries < MAX_RETRIES:
            self.flags.append("retry")
            feedback = "Guardrail: rewrite your last answer. Fix: " + "; ".join(problems) + "."
            return {
                "messages": [RemoveMessage(id=last.id), SystemMessage(feedback, name=GUARD_NAME)],
                "jump_to": "model",
            }
        if problems:
            self.flags.append("fallback")
            return {"messages": [RemoveMessage(id=last.id), AIMessage(content=FALLBACK)]}
        if removed:
            return {"messages": [RemoveMessage(id=last.id), AIMessage(content=answer)]}
        return None

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


CLAIMS_FILTERS = ("status", "date_from", "date_to")
UNFILTERED_NOTE = (
    "No claims matched the filters, so the list was fetched again without filters. Say that no "
    "claims matched those filters, then answer from these claims (newest submitted first)."
)


class EmptyClaimsRetry(AgentMiddleware):
    """If a filtered get_my_claims call finds nothing, run it once more without filters.

    Deterministic safety net for a real bug: the model invented a date range for "my latest
    claim", got no rows and told the user they had no claims. The model then sees both facts:
    nothing matched the filters, and here are the latest claims.
    """

    def __init__(self) -> None:
        super().__init__()
        self.flags: list[str] = []

    @staticmethod
    def _needs_retry(request: Any, result: Any) -> bool:
        call = request.tool_call
        if call.get("name") != "get_my_claims" or not isinstance(result, ToolMessage):
            return False
        if not any(call.get("args", {}).get(k) for k in CLAIMS_FILTERS):
            return False
        data = rules.parse_json(rules.text_of(result.content))
        return isinstance(data, dict) and data.get("count") == 0

    def _merge(self, request: Any, first: ToolMessage, retry: Any) -> ToolMessage:
        self.flags.append("claims_retry_unfiltered")
        data = rules.parse_json(rules.text_of(getattr(retry, "content", ""))) or {}
        filters = {k: v for k, v in request.tool_call.get("args", {}).items() if v}
        merged = {"filtered_call": {"filters": filters, "count": 0}, "note": UNFILTERED_NOTE}
        if isinstance(data, dict):
            merged.update({k: v for k, v in data.items() if k != "note"})
        return ToolMessage(
            content=json.dumps(merged, ensure_ascii=False),
            tool_call_id=first.tool_call_id,
            name=first.name,
        )

    @staticmethod
    def _unfiltered(request: Any) -> Any:
        args = {
            k: v for k, v in request.tool_call.get("args", {}).items() if k not in CLAIMS_FILTERS
        }
        return request.override(tool_call={**request.tool_call, "args": args})

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        result = handler(request)
        if not self._needs_retry(request, result):
            return result
        return self._merge(request, result, handler(self._unfiltered(request)))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        result = await handler(request)
        if not self._needs_retry(request, result):
            return result
        return self._merge(request, result, await handler(self._unfiltered(request)))
