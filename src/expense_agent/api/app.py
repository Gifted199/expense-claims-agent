"""The agent API (FastAPI, internal port 8000). The Streamlit UI is its only client.

Identity (D42): in demo mode the UI's user switcher sends ``X-Demo-Employee-Id``; it is accepted
only when demo auth is switched on, and only for an active employee. In production this would be
a Cognito JWT. Whatever the source, the identity is set here and passed down as headers; the LLM
never sees or chooses it. Manager endpoints are plain HTTP, never agent tools.
"""

# No "from __future__ import annotations" here: FastAPI must resolve the Annotated
# dependency aliases defined inside create_app at runtime.
import logging
import os
from typing import Annotated, Any

import anyio
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from expense_agent.agent.memory import BadThreadId
from expense_agent.api import db
from expense_agent.api.backend import Backend
from expense_agent.api.uploads import MAX_UPLOAD_BYTES, UploadError, check_upload, upload_key

log = logging.getLogger(__name__)
DEMO_HEADER = "X-Demo-Employee-Id"


def demo_auth_enabled() -> bool:
    return os.environ.get("EXPENSE_DEMO_AUTH", "false").strip().lower() == "true"


class ChatIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)  # the guardrail limit (2000) is stricter


class DecisionIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=db.MAX_REASON)


def create_app(backend: Backend, *, demo_auth: bool | None = None) -> FastAPI:
    demo = demo_auth_enabled() if demo_auth is None else demo_auth
    app = FastAPI(title="Expense Claims Assistant API", docs_url=None, redoc_url=None)

    def current_user(
        x_demo_employee_id: Annotated[str | None, Header(alias=DEMO_HEADER)] = None,
    ) -> db.Profile:
        if not demo:
            raise HTTPException(401, "sign-in required")  # production: Cognito (future work)
        text = (x_demo_employee_id or "").strip()
        if not (text.isascii() and text.isdigit()) or len(text) > 10:
            raise HTTPException(401, "sign-in required")
        profile = backend.profile(int(text))
        if profile is None or not profile.is_active:
            raise HTTPException(401, "sign-in required")
        return profile

    User = Annotated[db.Profile, Depends(current_user)]

    def manager(user: User) -> db.Profile:
        if not user.is_manager:
            raise HTTPException(403, "managers only")
        return user

    Manager = Annotated[db.Profile, Depends(manager)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me")
    def me(user: User) -> dict[str, Any]:
        return user.to_dict()

    @app.get("/demo/employees")
    def demo_employees() -> list[dict[str, Any]]:
        if not demo:
            raise HTTPException(404, "not found")
        return [p.to_dict() for p in backend.directory()]

    @app.post("/chat")
    async def chat(body: ChatIn, user: User) -> dict[str, Any]:
        try:
            result = await backend.chat(user.employee_id, body.thread_id, body.message)
        except BadThreadId as exc:
            raise HTTPException(422, str(exc)) from exc
        return result.to_dict()

    @app.post("/upload")
    async def upload(user: User, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        body = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            ext, content_type = check_upload(body)
        except UploadError as exc:
            raise HTTPException(422, str(exc)) from exc
        key = upload_key(user.employee_code, ext)
        await anyio.to_thread.run_sync(backend.put_receipt, key, body, content_type)
        log.info("upload employee=%s key=%s bytes=%d", user.employee_id, key, len(body))
        try:
            ocr = await backend.call_tool(user.employee_id, "extract_receipt", {"s3_key": key})
        except ValueError as exc:
            return {"s3_key": key, "ocr": None, "error": str(exc)}
        return {"s3_key": key, "ocr": ocr}

    @app.get("/claims")
    async def claims(user: User, status: str | None = None) -> dict[str, Any]:
        args = {"status": status} if status else {}
        try:
            return await backend.call_tool(user.employee_id, "get_my_claims", args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/manager/queue")
    def queue(user: Manager) -> dict[str, Any]:
        items = backend.manager_queue(user.employee_id)
        return {"claims": items, "count": len(items)}

    @app.post("/manager/claims/{claim_id}/decision")
    def decision(claim_id: int, body: DecisionIn, user: Manager) -> dict[str, Any]:
        try:
            result = backend.decide(user.employee_id, claim_id, body.decision, body.reason)
        except db.DecisionError as exc:
            raise HTTPException(422, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "no pending claim of your team with that number")
        log.info(
            "manager_decision manager=%s claim=%s %s", user.employee_id, claim_id, result["status"]
        )
        return result

    return app
