"""The UI's only way out: HTTP calls to the agent API. The UI holds no secrets (D45)."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEMO_HEADER = "X-Demo-Employee-Id"


def api_url() -> str:
    return os.environ.get("EXPENSE_API_URL", DEFAULT_API_URL).rstrip("/")


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, employee_id: int | None, base_url: str | None = None) -> None:
        self.base_url = base_url or api_url()
        self.employee_id = employee_id

    def _headers(self) -> dict[str, str]:
        return {DEMO_HEADER: str(self.employee_id)} if self.employee_id else {}

    def _request(self, method: str, path: str, timeout: float = 30, **kwargs: Any) -> Any:
        try:
            r = httpx.request(
                method, f"{self.base_url}{path}", headers=self._headers(), timeout=timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ApiError("The assistant service is not reachable right now.") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise ApiError(
                detail if isinstance(detail, str) else f"Request failed ({r.status_code})"
            )
        return r.json()

    def employees(self) -> list[dict[str, Any]]:
        return self._request("GET", "/demo/employees")

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def chat(self, thread_id: str, message: str) -> dict[str, Any]:
        body = {"thread_id": thread_id, "message": message}
        return self._request("POST", "/chat", json=body, timeout=180)

    def upload(self, filename: str, data: bytes, content_type: str) -> dict[str, Any]:
        files = {"file": (filename, data, content_type or "application/octet-stream")}
        return self._request("POST", "/upload", files=files, timeout=120)

    def claims(self, status: str | None = None) -> dict[str, Any]:
        params = {"status": status} if status else None
        return self._request("GET", "/claims", params=params, timeout=60)

    def queue(self) -> dict[str, Any]:
        return self._request("GET", "/manager/queue")

    def decide(self, claim_id: int, decision: str, reason: str) -> dict[str, Any]:
        body = {"decision": decision, "reason": reason}
        return self._request("POST", f"/manager/claims/{int(claim_id)}/decision", json=body)
