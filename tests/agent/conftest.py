"""A scripted fake chat model and fake tools named like the 7 MCP tools (no cloud, no LLM)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptedModel(BaseChatModel):
    """Replays scripted replies in order; records what it was sent on every call."""

    script: list[Any]
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("the model was called more times than scripted")
        step = self.script.pop(0)
        reply = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=reply)])


def scripted(*steps: Step) -> ScriptedModel:
    return ScriptedModel(script=list(steps), calls=[])


def call(name: str, args: dict[str, Any] | None = None, cid: str = "c1") -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": cid, "type": "tool_call"}


def ai_tools(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


POLICY_RESULT = {
    "not_covered": False,
    "top_similarity": 0.55,
    "passages": [
        {"section_id": "EXP-6.3", "title": "Hotels by city", "text": "London senior £230.00"},
        {"section_id": "EXP-3.1", "title": "When a receipt is required", "text": "over £10.00"},
    ],
}
NOT_COVERED = {"not_covered": True, "top_similarity": 0.2, "passages": []}
SUMMARY_RESULT = {
    "period": "this_year",
    "groups": [{"category": "taxi", "total": "120.50"}],
    "total": "120.50",
}
CLAIMS_RESULT = {
    "claims": [{"claim_ref": "CLM-2026-000042", "amount": "24.00", "status": "approved"}]
}


class FakeTools:
    """Seven StructuredTools; every call is recorded with its arguments."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results = {
            "search_policy": POLICY_RESULT,
            "spending_summary": SUMMARY_RESULT,
            "get_my_claims": CLAIMS_RESULT,
            "get_claim_status": {"found": True, "status": "pending_manager"},
            "score_claim_anomaly": {"score": 0.4, "label": "normal"},
            "extract_receipt": {"vendor": "CityCab", "total": "24.00"},
            "submit_claim": {"result": "submitted", "status": "approved"},
            **(results or {}),
        }

    def _tool(self, name: str) -> StructuredTool:
        def run(**kwargs: Any) -> str:
            self.calls.append((name, kwargs))
            result = self.results[name]
            return json.dumps(result(**kwargs) if callable(result) else result)

        return StructuredTool.from_function(
            func=run,
            name=name,
            description=f"fake {name}",
            args_schema={"type": "object", "properties": {}},
        )

    def tools(self) -> list[StructuredTool]:
        return [self._tool(n) for n in self.results]

    def loader(self) -> Callable[[int], Any]:
        async def load(employee_id: int) -> list[StructuredTool]:
            self.loaded_for = employee_id
            return self.tools()

        return load
