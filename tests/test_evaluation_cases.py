"""The Phase 8 conversation set is well formed and contains the latest-claim regression."""

from __future__ import annotations

from expense_agent.evaluation import load_conversations


def test_cases_are_well_formed() -> None:
    cases = load_conversations()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case["turns"] and all("user" in t and "expect" in t for t in case["turns"])
        assert isinstance(case["employee_id"], int)


def test_latest_claim_regression_is_in_the_set() -> None:
    case = next(c for c in load_conversations() if c["id"].startswith("c001"))
    turn = case["turns"][0]
    assert turn["user"] == "What's the status of my latest claim?"
    assert turn["expect"]["get_my_claims_args_without"] == ["date_from", "date_to"]


def test_scorer_families() -> None:
    from expense_agent.evaluation.run import score_turn, summarise, turn_passed

    expect = {"tools_any": ["search_policy"], "cites_any": ["EXP-6.3"], "refused": False}
    good = {"reply": "Cap £230.00 (EXP-6.3).", "tools_used": ["search_policy"], "flags": []}
    assert turn_passed(score_turn(expect, good))
    fallback = {**good, "flags": ["missing_citation", "fallback"]}
    assert score_turn(expect, fallback)["answer"]["no_invented_money"] is False
    refusal = {"reply": "No.", "tools_used": [], "flags": ["investment_only"]}
    assert score_turn({"tools_none": True, "refused": True}, refusal) == {
        "tool_choice": {"tools_none": True},
        "guardrail": {"refused": True},
    }
    curly = {"reply": "I can\u2019t check that.", "tools_used": [], "flags": []}
    assert turn_passed(score_turn({"reply_contains_any": ["can't check"]}, curly))
    results = [
        {"id": "x", "memory": True, "turns": [{"scores": {}}, {"scores": score_turn(expect, good)}]}
    ]
    assert summarise(results)["memory"] == {"passed": 1, "total": 1}
