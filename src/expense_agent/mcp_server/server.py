"""The MCP server: FastMCP over streamable HTTP on an internal port (decision D33).

Seven tools. None takes an employee or identity argument: the employee comes from the
``X-Employee-Id`` header, which the auth middleware only lets through with the internal token.
Manager approve/reject is deliberately **not** a tool (only a human does that, in the UI).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from expense_agent.mcp_server import tools
from expense_agent.mcp_server.auth import EMPLOYEE_HEADER, AuthError, parse_employee_id
from expense_agent.ocr.textract import ReceiptKeyError

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Expense claims tools for ONE employee: the logged-in user, set by the server. "
    "You cannot act for anyone else. Approval decisions are made by code and by human managers, "
    "never by you. Every money figure you state must come from a tool result, and policy "
    "answers must cite the section_id returned by search_policy."
)


def employee_id_from_context(ctx: Context) -> int:
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise ToolError("no authenticated HTTP request")
    try:
        return parse_employee_id(request.headers.get(EMPLOYEE_HEADER))
    except AuthError as exc:
        raise ToolError("unauthorised") from exc


async def _run[T](fn: Callable[[], T]) -> T:
    """Run blocking work (DB, AWS, Azure) off the event loop; turn input errors into ToolError."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except (tools.ToolInputError, ReceiptKeyError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def build_server(
    services: tools.ServicesLike, host: str = "127.0.0.1", port: int = 8080
) -> FastMCP:
    mcp = FastMCP(
        "expense-claims",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )

    async def who(ctx: Context) -> Any:
        employee_id = employee_id_from_context(ctx)
        return await _run(partial(tools.employee_from_db, services, employee_id))

    @mcp.tool()
    async def search_policy(question: str, ctx: Context) -> dict[str, Any]:
        """Search the latest expense policy handbook. Returns passages with section IDs to cite
        (e.g. EXP-6.3). If not_covered is true, say the handbook doesn't cover it."""
        await who(ctx)
        return await _run(partial(tools.tool_search_policy, services, question))

    @mcp.tool()
    async def score_claim_anomaly(
        ctx: Context,
        claim_id: int | None = None,
        category: str | None = None,
        amount: float | None = None,
        merchant: str | None = None,
        incurred_at: str | None = None,
    ) -> dict[str, Any]:
        """How unusual a claim looks (0-1; 'high' at 0.95 or more) with plain-word reasons.
        Give either one of your existing claim_id values, or category, amount (GBP), merchant
        and incurred_at (ISO date) for a claim you are about to submit."""
        employee = await who(ctx)
        return await _run(
            partial(
                tools.tool_score_claim_anomaly,
                services,
                employee,
                claim_id,
                category,
                amount,
                merchant,
                incurred_at,
            )
        )

    @mcp.tool()
    async def extract_receipt(s3_key: str, ctx: Context) -> dict[str, Any]:
        """Read a receipt image already uploaded to S3 (Amazon Textract) and return vendor,
        date, total, currency and a confidence, to prefill a claim. Ask the user to confirm
        any field when needs_review is true."""
        employee = await who(ctx)
        return await _run(partial(tools.tool_extract_receipt, services, employee, s3_key))

    @mcp.tool()
    async def get_my_claims(
        ctx: Context,
        status: Literal["submitted", "approved", "pending_manager", "rejected"] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """List the current employee's own claims, most recently SUBMITTED first (at most 50),
        so claims[0] is the latest claim. Call it WITHOUT filters by default. Only pass status
        or date_from / date_to (expense dates, YYYY-MM-DD, inclusive) when the user explicitly
        asked for that status or period; never invent a date range. If a filtered call returns
        no claims, call it again without filters before saying the user has no claims.
        Show dates using the *_display fields (e.g. "26 Oct 2026, 14:26")."""
        employee = await who(ctx)
        return await _run(
            partial(tools.tool_get_my_claims, services, employee, status, date_from, date_to)
        )

    @mcp.tool()
    async def get_claim_status(claim: str, ctx: Context) -> dict[str, Any]:
        """Status of one of the current employee's claims, by number (e.g. 1234) or reference
        (e.g. CLM-2026-001234), with the latest decision and its reason."""
        employee = await who(ctx)
        return await _run(partial(tools.tool_get_claim_status, services, employee, claim))

    @mcp.tool()
    async def spending_summary(
        ctx: Context,
        period: Literal[
            "this_month", "last_month", "this_quarter", "this_year", "last_12_months"
        ] = "this_month",
        group_by: Literal["category", "month"] = "category",
    ) -> dict[str, Any]:
        """Total of the current employee's claims (excluding rejected) for a period, grouped by
        category or by month, in GBP."""
        employee = await who(ctx)
        return await _run(
            partial(tools.tool_spending_summary, services, employee, period, group_by)
        )

    @mcp.tool()
    async def submit_claim(
        ctx: Context,
        category: str,
        amount: float,
        merchant: str,
        incurred_at: str,
        description: str = "",
        receipt_s3_key: str | None = None,
        city: str | None = None,
        nights: int | None = None,
    ) -> dict[str, Any]:
        """Submit an expense claim for the current employee. Fixed checks run in code (limits,
        hotel city caps, duplicates, split purchases, deadline, anomaly score): it is approved
        only if every check passes, otherwise it goes to the manager. Hotel claims need city
        and nights: if result is 'missing_fields', ask the user for them; never guess."""
        employee = await who(ctx)
        fields = {
            "category": category,
            "amount": amount,
            "merchant": merchant,
            "incurred_at": incurred_at,
            "description": description,
            "receipt_s3_key": receipt_s3_key,
            "city": city,
            "nights": nights,
        }
        result = await _run(partial(tools.tool_submit_claim, services, employee, fields))
        log.info(
            "submit_claim employee=%s result=%s status=%s",
            employee.employee_id,
            result.get("result"),
            result.get("status", "-"),
        )
        return result

    return mcp
