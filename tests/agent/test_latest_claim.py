"""Regression: "What's the status of my latest claim?" (bug found in manual testing, 2026-11-26).

The model invented a date range (2026-11-01..2026-11-26), got no rows because the synthetic data
ends in Oct 2026, and told the user they had no claims. Now a filtered get_my_claims that finds
nothing is retried once without filters, and the model sees the latest claims.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from expense_agent.guardrails import rules

from .conftest import FakeTools, ai_tools, call, scripted
from .test_agent import run

QUESTION = "What's the status of my latest claim?"
LATEST = {
    "claim_id": 2864,
    "claim_ref": "CLM-2026-002864",
    "category": "mileage",
    "amount": "43.95",
    "status": "approved",
    "merchant": "Mileage claim",
    "submitted_display": "26 Oct 2026, 14:26",
}


def claims(**filters: Any) -> dict[str, Any]:
    """Like the real tool on the synthetic data: nothing in Nov 2026, claims without filters."""
    if any(filters.get(k) for k in ("date_from", "date_to", "status")):
        return {
            "claims": [],
            "count": 0,
            "filters": filters,
            "note": "No claims match these filters.",
        }
    return {"claims": [LATEST], "count": 1, "order": "most recently submitted first"}


def answer(messages: list[BaseMessage]) -> AIMessage:
    result = [m for m in messages if isinstance(m, ToolMessage)][-1]
    data = json.loads(rules.text_of(result.content))
    first = data["claims"][0]
    return AIMessage(
        content=f"Your most recently submitted claim is {first['claim_ref']} "
        f"(submitted {first['submitted_display']}): {first['status']}, £{first['amount']}."
    )


def test_invented_date_filter_is_retried_without_filters() -> None:
    tools = FakeTools({"get_my_claims": claims})
    model = scripted(
        ai_tools(call("get_my_claims", {"date_from": "2026-11-01", "date_to": "2026-11-26"})),
        answer,
    )
    out = run(model, QUESTION, tools)
    assert [args for _, args in tools.calls] == [
        {"date_from": "2026-11-01", "date_to": "2026-11-26"},
        {},  # the retry, without filters
    ]
    seen = rules.text_of([m for m in model.calls[1] if isinstance(m, ToolMessage)][-1].content)
    assert "CLM-2026-002864" in seen and "fetched again without filters" in seen
    assert "most recently submitted" in out.reply and "CLM-2026-002864" in out.reply
    assert "26 Oct 2026, 14:26" in out.reply and "T14:26" not in out.reply
    assert "claims_retry_unfiltered" in out.flags
    assert "no claims" not in out.reply.lower()


def test_unfiltered_call_is_not_retried() -> None:
    tools = FakeTools({"get_my_claims": claims})
    model = scripted(ai_tools(call("get_my_claims", {})), answer)
    out = run(model, QUESTION, tools)
    assert len(tools.calls) == 1 and "claims_retry_unfiltered" not in out.flags


def test_filtered_call_with_results_is_not_retried() -> None:
    tools = FakeTools({"get_my_claims": {"claims": [LATEST], "count": 1}})
    model = scripted(ai_tools(call("get_my_claims", {"status": "approved"})), answer)
    run(model, "Show my approved claims", tools)
    assert len(tools.calls) == 1


def test_prompt_and_tool_description_carry_the_rule() -> None:
    import asyncio

    from expense_agent.agent.prompt import build_system_prompt
    from expense_agent.mcp_server.server import build_server

    prompt = build_system_prompt()
    assert "WITHOUT filters" in prompt and "most recently SUBMITTED" in prompt
    assert "26 Oct 2026, 14:26" in prompt
    tool = next(
        t for t in asyncio.run(build_server(object()).list_tools()) if t.name == "get_my_claims"
    )
    assert "WITHOUT filters" in (tool.description or "")
    assert "never invent a date range" in (tool.description or "")
