"""Evaluation metrics, the selection rule, and the labelled question set."""

from __future__ import annotations

from collections import Counter

import pytest

from expense_agent.rag import evaluate as E
from expense_agent.rag.chunking import chunk_handbook
from expense_agent.rag.handbook import LATEST, PolicyData, render


def q(qid: str, expected: tuple[str, ...], t: str = "paraphrase") -> E.Question:
    return E.Question(qid, "?", t, expected)


def test_matches_section_and_subsection() -> None:
    assert E.matches("EXP-7.1", "EXP-7") and E.matches("EXP-7", "EXP-7")
    assert not E.matches("EXP-17.1", "EXP-1") and not E.matches("EXP-7", "EXP-7.1")


def test_recall_and_mrr_hand_worked() -> None:
    qs = [
        q("a", ("EXP-1.1",)),
        q("b", ("EXP-2.1",)),
        q("c", ("EXP-3.1",)),
        q("z", (), "out_of_scope"),
    ]
    retrieved = {
        "a": ["EXP-1.1", "EXP-9.1"],  # rank 1
        "b": ["EXP-9.1", "EXP-9.2", "EXP-9.3", "EXP-2.1"],  # rank 4
        "c": ["EXP-9.1"],  # miss
        "z": ["EXP-1.1"],  # ignored (out of scope)
    }
    m = E.score_rankings(qs, retrieved)
    assert m["n"] == 3
    assert m["recall@3"] == pytest.approx(1 / 3)
    assert m["recall@5"] == pytest.approx(2 / 3)
    assert m["mrr"] == pytest.approx((1 + 1 / 4 + 0) / 3)
    assert m["misses@5"] == ["c"]


def test_multi_section_question_counts_first_hit() -> None:
    m = E.score_rankings([q("a", ("EXP-11.2", "EXP-7.1"))], {"a": ["EXP-5.1", "EXP-7.1"]})
    assert m["mrr"] == pytest.approx(0.5)


def combo(r5: float, mrr: float) -> dict:
    return {"recall@5": r5, "mrr": mrr}


def test_winner_rule_prefers_recall_then_mrr() -> None:
    combos = {
        "keyword": combo(0.60, 0.50),
        "vector/text-embedding-3-small": combo(0.80, 0.60),
        "hybrid/text-embedding-3-small": combo(0.92, 0.70),
    }
    assert E.choose_winner(combos, 25) == "hybrid/text-embedding-3-small"


def test_winner_rule_breaks_ties_by_cost_then_simplicity() -> None:
    combos = {
        "vector/text-embedding-3-large": combo(0.92, 0.80),
        "vector/text-embedding-3-small": combo(0.88, 0.795),  # within one question / 0.01
        "hybrid/text-embedding-3-small": combo(0.92, 0.80),
    }
    assert E.choose_winner(combos, 25) == "vector/text-embedding-3-small"


def test_question_set_shape() -> None:
    qs = E.load_questions()
    assert len(qs) >= 30
    assert len({x.id for x in qs}) == len(qs)
    types = Counter(x.type for x in qs)
    assert set(types) == set(E.QUESTION_TYPES)
    assert types["out_of_scope"] >= 4 and types["exact"] >= 4
    assert all(not x.expected_sections for x in qs if not x.in_scope)
    assert all(x.expected_sections for x in qs if x.in_scope)


def test_every_expected_section_exists_in_latest_handbook(policy_data: PolicyData) -> None:
    sids = {c.section_id for c in chunk_handbook(render(policy_data, LATEST))}
    for x in E.load_questions():
        for sid in x.expected_sections:
            assert sid in sids, f"{x.id}: {sid}"


def test_top_similarities() -> None:
    qs = [q("a", ("EXP-1",)), q("b", ("EXP-2",)), q("z", (), "out_of_scope")]
    out = E.top_similarities(qs, lambda x: {"a": 0.6, "b": 0.5, "z": 0.3}[x.id])
    assert out["separable"] and out["in_scope_min"] == 0.5 and out["out_of_scope_max"] == 0.3
