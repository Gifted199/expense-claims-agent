"""Smoke test against a running MCP server that uses the real cloud services.

``uv run python -m expense_agent.mcp_server.smoke --profile aero --employee 5``

Lists the tools and calls each one as an MCP client would. It **writes real synthetic claims to
RDS** (descriptions start with "[smoke]"); their approval is decided by the workflow code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any

EXPECTED_TOOLS = {
    "search_policy",
    "score_claim_anomaly",
    "extract_receipt",
    "get_my_claims",
    "get_claim_status",
    "spending_summary",
    "submit_claim",
}


class Smoke:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
        if not ok:
            self.failures.append(label)


def payload(result: Any) -> dict[str, Any]:
    if result.structuredContent:
        return dict(result.structuredContent)
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


async def run(url: str, token: str, employee: int, manifest: dict[str, Any]) -> Smoke:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    s = Smoke()
    async with httpx.AsyncClient(timeout=120) as anon:
        r = await anon.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        s.check("call without the token is rejected", r.status_code == 401, str(r.status_code))
        r = await anon.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer wrong-token-" + "x" * 40, "X-Employee-Id": "1"},
        )
        s.check("call with a wrong token is rejected", r.status_code == 401, str(r.status_code))

    headers = {"Authorization": f"Bearer {token}", "X-Employee-Id": str(employee)}
    async with (
        httpx.AsyncClient(headers=headers, timeout=180) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        s.check("lists the 7 tools", names == EXPECTED_TOOLS, ", ".join(sorted(names)))

        async def call(name: str, **args: Any) -> tuple[bool, dict[str, Any]]:
            res = await session.call_tool(name, args)
            return bool(res.isError), payload(res)

        err, out = await call(
            "search_policy", question="What is the nightly hotel cap in London for a senior?"
        )
        ids = [p["section_id"] for p in out.get("passages", [])]
        s.check(
            "search_policy: London hotel cap -> EXP-6.3", not err and "EXP-6.3" in ids, str(ids)
        )
        err, out = await call("search_policy", question="Which ETF should I buy for my pension?")
        s.check(
            "search_policy: investment question -> not_covered",
            not err and out.get("not_covered") is True,
            f"top_similarity={out.get('top_similarity')}",
        )

        err, out = await call("get_my_claims")
        claims = out.get("claims", [])
        s.check("get_my_claims", not err and "claims" in out, f"{out.get('count')} claims")
        err, out = await call("spending_summary", period="last_12_months", group_by="category")
        s.check("spending_summary", not err and "total" in out, f"total £{out.get('total')}")
        if claims:
            first = claims[0]
            err, out = await call("get_claim_status", claim=first["claim_ref"])
            s.check("get_claim_status (own claim)", not err and out.get("found"), out.get("status"))
            err, out = await call("score_claim_anomaly", claim_id=first["claim_id"])
            s.check(
                "score_claim_anomaly (existing claim)",
                not err and "score" in out,
                f"score={out.get('score')} label={out.get('label')} reasons={out.get('reasons')}",
            )
        err, out = await call("get_claim_status", claim="1")
        s.check("get_claim_status: other people's claim isn't visible", not err, str(out))

        hotel = next(r for r in manifest["receipts"] if r["category"] == "hotel")
        err, out = await call("extract_receipt", s3_key=hotel["key"])
        s.check(
            "extract_receipt matches the manifest",
            not err and out.get("total") == hotel["total"] and out.get("date") == hotel["date"],
            f"vendor={out.get('vendor')!r} date={out.get('date')} total={out.get('total')} "
            f"currency={out.get('currency')} confidence={out.get('confidence')}",
        )
        err, out = await call("extract_receipt", s3_key="uploads/E9999/receipt.png")
        s.check("extract_receipt refuses another employee's upload", err, str(out)[:80])

        err, out = await call(
            "score_claim_anomaly",
            category="meals",
            amount=18.5,
            merchant="Copperleaf Kitchen",
            incurred_at=manifest["receipts"][0]["date"],
        )
        s.check("score_claim_anomaly (new claim)", not err and "score" in out, str(out))

        base = {
            "category": "hotel",
            "amount": float(hotel["total"]),
            "merchant": hotel["vendor"],
            "incurred_at": hotel["date"],
            "description": "[smoke] client workshop",
            "receipt_s3_key": hotel["key"],
        }
        err, out = await call("submit_claim", **base)
        s.check(
            "submit_claim: hotel without city/nights -> missing_fields",
            not err and out.get("result") == "missing_fields",
            str(out.get("missing")),
        )
        err, out = await call("submit_claim", **base, city=hotel["city"], nights=hotel["nights"])
        s.check(
            "submit_claim: hotel claim submitted",
            not err and out.get("result") == "submitted",
            f"{out.get('claim_ref')} {out.get('status')}: {out.get('reasons')}",
        )
        lunch = manifest["receipts"][0]
        err, out = await call(
            "submit_claim",
            category=lunch["category"],
            amount=float(lunch["total"]),
            merchant=lunch["vendor"],
            incurred_at=lunch["date"],
            description="[smoke] working lunch",
            receipt_s3_key=lunch["key"],
        )
        s.check(
            "submit_claim: lunch submitted",
            not err and out.get("result") == "submitted",
            f"{out.get('claim_ref')} {out.get('status')}: {out.get('reasons')}",
        )
    return s


def main(argv: Sequence[str] | None = None) -> int:
    from expense_agent.db.connection import DbSettings, add_profile_argument, make_session
    from expense_agent.mcp_server.auth import load_token
    from expense_agent.ocr.textract import SAMPLES_PREFIX, receipts_bucket

    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--employee", type=int, required=True)
    args = parser.parse_args(argv)

    session = make_session(args.profile or None, DbSettings.from_env().region)
    token = load_token(session)
    body = session.client("s3").get_object(
        Bucket=receipts_bucket(), Key=f"{SAMPLES_PREFIX}manifest.json"
    )["Body"]
    smoke = asyncio.run(run(args.url, token, args.employee, json.loads(body.read())))
    print(
        f"\n{'ALL SMOKE CHECKS PASSED' if not smoke.failures else 'FAILED: ' + str(smoke.failures)}"
    )
    return 0 if not smoke.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
