"""Tool logic, separate from the MCP plumbing so it can be unit-tested with fakes.

Every function takes the already-authenticated ``Employee`` (from the request header, never
from the model) and the ``Services`` with the cloud clients.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from expense_agent.ml.records import load_claims, load_limits
from expense_agent.ml.serving import features_for_existing, features_for_new, score_row
from expense_agent.ocr.textract import extract_receipt
from expense_agent.rag.search import Passage, search_policy
from expense_agent.workflow import queries, submit
from expense_agent.workflow.queries import Employee

NOT_COVERED_BELOW = 0.35  # top vector similarity; from the Phase 3 out-of-scope check (RAG.md)
MAX_QUESTION_CHARS = 1000
POLICY_K = 5
OWNED_CLAIM_SQL = "SELECT 1 FROM claims WHERE claim_id = %s AND employee_id = %s"


class ToolInputError(ValueError):
    """A problem with the arguments, worded for the agent to relay or fix."""


class ServicesLike(Protocol):
    def db(self, role: Any) -> Any: ...
    def client(self, name: str) -> Any: ...
    def embed(self, text: str) -> list[float]: ...
    def model(self) -> Any: ...


def employee_from_db(services: ServicesLike, employee_id: int) -> Employee:
    with services.db("readonly") as conn:
        employee = queries.load_employee(conn, employee_id)
    if employee is None or not employee.is_active:
        raise ToolInputError("unknown or inactive employee")
    return employee


def policy_result(passages: list[Passage], threshold: float = NOT_COVERED_BELOW) -> dict[str, Any]:
    exact = any(p.method == "exact_id" for p in passages)
    top_similarity = max((p.score for p in passages if p.method != "exact_id"), default=0.0)
    not_covered = not exact and top_similarity < threshold
    return {
        "not_covered": not_covered,
        "top_similarity": round(top_similarity, 3),
        "note": "The handbook does not appear to cover this; say so rather than guess."
        if not_covered
        else "Cite the section_id of every passage you rely on.",
        "passages": [
            {
                "section_id": p.section_id,
                "title": p.title,
                "text": p.text,
                "version": p.version,
                "score": round(p.score, 3),
                "match": p.method,
            }
            for p in passages
        ],
    }


def tool_search_policy(services: ServicesLike, question: str) -> dict[str, Any]:
    question = (question or "").strip()
    if not 1 <= len(question) <= MAX_QUESTION_CHARS:
        raise ToolInputError(f"question must be 1-{MAX_QUESTION_CHARS} characters")
    from expense_agent.mcp_server.services import rag_model

    with services.db("readonly") as conn:
        passages = search_policy(
            conn, question, services.embed, rag_model(), k=POLICY_K, method="vector_id"
        )
    return policy_result(passages)


def tool_score_claim_anomaly(
    services: ServicesLike,
    employee: Employee,
    claim_id: int | None = None,
    category: str | None = None,
    amount: float | None = None,
    merchant: str | None = None,
    incurred_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    with services.db("readonly") as conn:
        if claim_id is not None:
            owned = conn.execute(OWNED_CLAIM_SQL, (claim_id, employee.employee_id)).fetchone()
            if owned is None:  # someone else's claim looks exactly like a missing one
                return {"found": False, "message": queries.NOT_FOUND}
            row = features_for_existing(load_claims(conn), load_limits(conn), claim_id)
            return {
                "found": True,
                "claim_id": claim_id,
                **score_row(services.model(), row).to_dict(),
            }

        if not (category and amount is not None and merchant and incurred_at):
            raise ToolInputError(
                "give either claim_id, or all of category, amount, merchant and incurred_at"
            )
        categories = submit.load_categories(conn)
        code = category.strip().lower()
        if code not in categories:
            raise ToolInputError(f"unknown category; use one of: {', '.join(sorted(categories))}")
        try:
            value = float(Decimal(str(amount)))
            when = submit.parse_incurred_at(incurred_at)
        except (InvalidOperation, ValueError) as exc:
            raise ToolInputError("amount must be a number and incurred_at an ISO date") from exc
        if value <= 0:
            raise ToolInputError("amount must be more than 0")
        row = features_for_new(
            load_claims(conn),
            load_limits(conn),
            employee_id=employee.employee_id,
            grade=employee.grade,
            category_code=code,
            is_travel=categories[code].is_travel,
            amount=value,
            merchant=merchant.strip(),
            incurred_at=when,
            now=now or datetime.now(submit.LONDON),
        )
    return {"found": True, "claim_id": None, **score_row(services.model(), row).to_dict()}


def tool_extract_receipt(services: ServicesLike, employee: Employee, s3_key: str) -> dict:
    return extract_receipt(services.client("textract"), s3_key, employee.employee_code)


def tool_get_my_claims(
    services: ServicesLike,
    employee: Employee,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    with services.db("readonly") as conn:
        return queries.get_my_claims(conn, employee.employee_id, status, date_from, date_to)


def tool_get_claim_status(services: ServicesLike, employee: Employee, claim: str) -> dict:
    with services.db("readonly") as conn:
        return queries.get_claim_status(conn, employee.employee_id, claim)


def tool_spending_summary(
    services: ServicesLike, employee: Employee, period: str, group_by: str
) -> dict[str, Any]:
    with services.db("readonly") as conn:
        return queries.spending_summary(conn, employee.employee_id, period, group_by)


def tool_submit_claim(
    services: ServicesLike, employee: Employee, fields: dict[str, Any]
) -> dict[str, Any]:
    with services.db("writer") as conn:
        return submit.submit_claim(conn, employee, fields, services.model)
