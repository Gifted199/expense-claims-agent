"""Cloud smoke test: six real conversations through the running agent API, then a manager step.

``uv run python -m expense_agent.agent.smoke --profile aero``

Needs the MCP server (:8080) and the agent API (:8000, demo auth on) running locally against the
cloud. **It writes real synthetic claims to RDS** and rejects one as the manager.
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

EMPLOYEE = 6  # Jonah Quarry, senior
MANAGER = 1  # Zane Ellison, Jonah's manager
HEADER = "X-Demo-Employee-Id"


class Smoke:
    def __init__(self, api: str) -> None:
        self.api = api.rstrip("/")
        self.failures: list[str] = []
        self.transcript: list[dict[str, Any]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
        if not ok:
            self.failures.append(label)

    def request(self, method: str, path: str, who: int, **kwargs: Any) -> Any:
        r = httpx.request(
            method, f"{self.api}{path}", headers={HEADER: str(who)}, timeout=240, **kwargs
        )
        r.raise_for_status()
        return r.json()

    def chat(self, thread: str, message: str, who: int = EMPLOYEE) -> dict[str, Any]:
        out = self.request("POST", "/chat", who, json={"thread_id": thread, "message": message})
        self.transcript.append({"thread": thread[:8], "user": message, **out})
        print(f"\n  you> {message}\n  bot> {out['reply']}")
        print(f"  tools={out['tools_used']} flags={out['flags']}")
        return out


def run(api: str, manifest: dict[str, Any], receipt_png: bytes) -> Smoke:
    s = Smoke(api)

    # 1. Policy question with a citation
    out = s.chat(str(uuid.uuid4()), "What's the nightly hotel cap in London for my grade?")
    s.check(
        "1 policy: search_policy, cites EXP-6.3, £230",
        "search_policy" in out["tools_used"]
        and "EXP-6.3" in out["reply"]
        and "230" in out["reply"],
    )

    # 2. Spending question: every £ figure verified by the output guardrail
    out = s.chat(str(uuid.uuid4()), "How much have I spent this year, by category?")
    s.check(
        "2 spending: spending_summary, no fallback",
        "spending_summary" in out["tools_used"] and "fallback" not in out["flags"],
    )

    # 3. Receipt upload (OCR preview) then submit -> approved (CityCab: an existing merchant)
    taxi = next(r for r in manifest["receipts"] if r["category"] == "taxi")
    up = s.request(
        "POST", "/upload", EMPLOYEE, files={"file": ("taxi.png", receipt_png, "image/png")}
    )
    ocr = up.get("ocr") or {}
    s.check(
        "3a upload: own prefix, OCR matches",
        up["s3_key"].startswith("uploads/E0006/")
        and ocr.get("vendor") == taxi["vendor"]
        and ocr.get("total") == taxi["total"],
        f"{up['s3_key']} {ocr.get('vendor')} {ocr.get('date')} {ocr.get('total')}",
    )
    out = s.chat(
        str(uuid.uuid4()),
        f"Please submit a taxi claim from my receipt {up['s3_key']}: vendor {ocr.get('vendor')}, "
        f"date {ocr.get('date')}, total £{ocr.get('total')}. Description: taxi from the station "
        "to a client meeting.",
    )
    s.check(
        "3b submit from receipt: submit_claim, approved",
        "submit_claim" in out["tools_used"] and "approved" in out["reply"].lower(),
    )

    # 4. Hotel claim missing city/nights -> the agent asks; answer -> pending_manager
    hotel = next(r for r in manifest["receipts"] if r["category"] == "hotel")
    hotel_thread = str(uuid.uuid4())
    out = s.chat(
        hotel_thread,
        f"Please submit my {hotel['vendor']} hotel bill of £{hotel['total']} "
        f"dated {hotel['date']}, receipt {hotel['key']}, for a client workshop.",
    )
    low = out["reply"].lower()
    s.check("4a hotel without city/nights: agent asks", "city" in low and "night" in low)
    out = s.chat(hotel_thread, "It was in London, 2 nights.")
    low = out["reply"].lower()
    s.check(
        "4b hotel answered: submitted, goes to manager with the cap reason",
        "submit_claim" in out["tools_used"] and ("pending" in low or "manager" in low),
    )

    # 5. Two-part question with an investment part
    out = s.chat(
        str(uuid.uuid4()),
        "What's the receipt rule for meals, and which ETF should I buy with my bonus?",
    )
    s.check(
        "5 multi-intent: policy answered with citation, investment declined",
        "search_policy" in out["tools_used"]
        and "EXP-3" in out["reply"]
        and "investment_mixed" in out["flags"]
        and "invest" in out["reply"].lower(),
    )

    # 6. Memory follow-up in the hotel thread (the graph is rebuilt per request, so memory = RDS)
    out = s.chat(hotel_thread, "What's the status of that hotel claim now?")
    s.check(
        "6 memory: resolves 'that hotel claim' and reports pending",
        bool({"get_claim_status", "get_my_claims"} & set(out["tools_used"]))
        and "pending" in out["reply"].lower(),
    )

    # Manager step (no LLM): the hotel claim is in the manager's queue with its reasons
    queue = s.request("GET", "/manager/queue", MANAGER)["claims"]
    mine = [c for c in queue if c["employee_code"] == "E0006" and c["merchant"] == hotel["vendor"]]
    s.check(
        "7a manager queue shows the hotel claim with reasons",
        bool(mine) and any("EXP-6.3" in r for r in mine[0]["review_reasons"]),
        json.dumps(mine[0]["review_reasons"]) if mine else "not found",
    )
    if mine:
        claim_url = f"/manager/claims/{mine[0]['claim_id']}/decision"
        try:  # the claimant can't decide their own claim (they aren't their own manager)
            s.request("POST", claim_url, EMPLOYEE, json={"decision": "approved", "reason": "me"})
            s.check("7b the claimant can't decide their own claim", False)
        except httpx.HTTPStatusError as exc:
            s.check(
                "7b the claimant can't decide their own claim",
                exc.response.status_code in (403, 404),
                str(exc.response.status_code),
            )
        reason = "Over the London cap: please rebook within it."
        decided = s.request(
            "POST", claim_url, MANAGER, json={"decision": "rejected", "reason": reason}
        )
        ok = decided.get("status") == "rejected"
        s.check("7c manager rejects with a reason", ok, str(decided))
    return s


def main(argv: Sequence[str] | None = None) -> int:
    from expense_agent.db.connection import DbSettings, add_profile_argument, make_session
    from expense_agent.ocr.textract import SAMPLES_PREFIX, receipts_bucket

    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--transcript", default=None, help="write the conversations as JSON here")
    args = parser.parse_args(argv)

    s3 = make_session(args.profile or None, DbSettings.from_env().region).client("s3")
    bucket = receipts_bucket()
    manifest = json.loads(
        s3.get_object(Bucket=bucket, Key=f"{SAMPLES_PREFIX}manifest.json")["Body"].read()
    )
    taxi_key = next(r["key"] for r in manifest["receipts"] if r["category"] == "taxi")
    png = s3.get_object(Bucket=bucket, Key=taxi_key)["Body"].read()

    smoke = run(args.api, manifest, png)
    if args.transcript:
        with open(args.transcript, "w", encoding="utf-8") as fh:
            json.dump(smoke.transcript, fh, indent=2, ensure_ascii=False)
    print(
        f"\n{'ALL SMOKE CHECKS PASSED' if not smoke.failures else 'FAILED: ' + str(smoke.failures)}"
    )
    return 0 if not smoke.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
