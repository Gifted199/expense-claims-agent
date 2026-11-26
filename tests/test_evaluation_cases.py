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
