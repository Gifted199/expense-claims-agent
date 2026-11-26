"""Phase 8 evaluation: run the labelled conversations through the agent API and score them.

``uv run python -m expense_agent.evaluation.run --api http://127.0.0.1:8001 --out results.json``

Deterministic checks only (no LLM judge). Each case gets a fresh conversation (UUID thread);
its turns run in order, so multi-turn cases exercise memory. **It talks to the real cloud stack
and the workflow cases write synthetic claims to RDS.**

Families, scored per turn (a turn passes a family if all its checks in that family pass):
* tool_choice: tools_any / tools_all / tools_none / tools_not against the tools the turn used
* answer: reply_contains_any / _all, reply_not_contains, cites_any, flags_any, and
  "no invented £": the output guardrail did not have to fall back (flag ``fallback``)
* guardrail: refused (a guardrail refusal flag) equals the expected ``refused``
* memory: a case marked ``memory`` passes if its follow-up turns pass all their checks
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from expense_agent.evaluation import load_conversations

REFUSAL_FLAGS = {"investment_only", "prompt_injection", "too_long", "empty"}
FAMILIES = ("tool_choice", "answer", "guardrail")
NOT_CHECKABLE = ("get_my_claims_args_without",)  # tool arguments aren't exposed by the API


def _norm(text: str) -> str:
    return text.replace("’", "'").lower()  # curly apostrophes from the model


def _has(text: str, needle: str) -> bool:
    return _norm(needle) in _norm(text)


def score_turn(expect: dict[str, Any], out: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """{family: {check: passed}} for one turn."""
    tools, flags, reply = set(out.get("tools_used", [])), set(out.get("flags", [])), out["reply"]
    checks: dict[str, dict[str, bool]] = {f: {} for f in FAMILIES}
    tc = checks["tool_choice"]
    if "tools_any" in expect:
        tc["tools_any"] = bool(tools & set(expect["tools_any"]))
    if "tools_all" in expect:
        tc["tools_all"] = set(expect["tools_all"]) <= tools
    if expect.get("tools_none"):
        tc["tools_none"] = not tools
    if "tools_not" in expect:
        tc["tools_not"] = not (tools & set(expect["tools_not"]))
    ans = checks["answer"]
    if "reply_contains_any" in expect:
        ans["reply_contains_any"] = any(_has(reply, s) for s in expect["reply_contains_any"])
    if "reply_contains_all" in expect:
        ans["reply_contains_all"] = all(_has(reply, s) for s in expect["reply_contains_all"])
    if "reply_not_contains" in expect:
        ans["reply_not_contains"] = not any(_has(reply, s) for s in expect["reply_not_contains"])
    if "cites_any" in expect:
        ans["cites_any"] = any(c in reply for c in expect["cites_any"])
    if "flags_any" in expect:
        ans["flags_any"] = bool(flags & set(expect["flags_any"]))
    refused = bool(flags & REFUSAL_FLAGS)
    if not refused:
        ans["no_invented_money"] = "fallback" not in flags
    if "refused" in expect:
        checks["guardrail"]["refused"] = refused == bool(expect["refused"])
    return {f: c for f, c in checks.items() if c}


def turn_passed(scores: dict[str, dict[str, bool]]) -> bool:
    return all(all(c.values()) for c in scores.values())


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    fam = {f: [0, 0] for f in FAMILIES}
    memory = [0, 0]
    for case in results:
        for turn in case["turns"]:
            for f, checks in turn["scores"].items():
                fam[f][0] += all(checks.values())
                fam[f][1] += 1
        if case.get("memory"):
            memory[0] += all(turn_passed(t["scores"]) for t in case["turns"][1:])
            memory[1] += 1
    rates = {f: {"passed": p, "total": n} for f, (p, n) in fam.items()}
    rates["memory"] = {"passed": memory[0], "total": memory[1]}
    cases_ok = sum(all(turn_passed(t["scores"]) for t in c["turns"]) for c in results)
    rates["cases"] = {"passed": cases_ok, "total": len(results)}
    return rates


def run(api: str) -> list[dict[str, Any]]:
    import httpx

    results = []
    for case in load_conversations():
        thread = str(uuid.uuid4())
        headers = {"X-Demo-Employee-Id": str(case["employee_id"])}
        turns = []
        for turn in case["turns"]:
            started = time.monotonic()
            r = httpx.post(
                f"{api.rstrip('/')}/chat",
                headers=headers,
                json={"thread_id": thread, "message": turn["user"]},
                timeout=300,
            )
            r.raise_for_status()
            out = r.json()
            scores = score_turn(turn["expect"], out)
            turns.append(
                {
                    "user": turn["user"],
                    "reply": out["reply"],
                    "tools_used": out["tools_used"],
                    "flags": out["flags"],
                    "latency_s": round(time.monotonic() - started, 1),
                    "scores": scores,
                    "not_checked": [k for k in NOT_CHECKABLE if k in turn["expect"]],
                }
            )
            print(
                f"[{'PASS' if turn_passed(scores) else 'FAIL'}] {case['id']}: "
                f"tools={out['tools_used']} flags={out['flags']}",
                flush=True,
            )
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "memory": case.get("memory", False),
                "turns": turns,
            }
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--out", default="evaluation-results.json")
    args = parser.parse_args(argv)
    results = run(args.api)
    summary = summarise(results)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2, ensure_ascii=False)
    for name, s in summary.items():
        print(f"{name}: {s['passed']}/{s['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
