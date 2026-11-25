"""Retrieval evaluation (decisions D26, D27).

- Recall@k: share of in-scope questions with a correct section in the top k passages.
- MRR: mean of 1 / (rank of the first correct section); 0 if none in the list.
A retrieved passage matches an expected ID if it is that section or one of its subsections
(``EXP-7`` matches ``EXP-7.1``).

The winner is chosen by a rule fixed BEFORE the run: highest Recall@5 (a difference of at most
one question is a tie), then MRR (differences up to 0.01 are a tie), then lower cost
(keyword < small < large), then simpler (keyword < vector < hybrid).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from statistics import mean
from typing import Any

KS = (3, 5)
QUESTION_TYPES = ("paraphrase", "exact", "multi", "out_of_scope")
MODEL_COST = {"": 0, "text-embedding-3-small": 1, "text-embedding-3-large": 2}
METHOD_COMPLEXITY = {"keyword": 0, "vector": 1, "hybrid": 2}


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    type: str
    expected_sections: tuple[str, ...]

    @property
    def in_scope(self) -> bool:
        return self.type != "out_of_scope"


def load_questions(filename: str = "eval_questions.json") -> list[Question]:
    raw = json.loads(resources.files("expense_agent.rag").joinpath(filename).read_text("utf-8"))
    return [Question(q["id"], q["question"], q["type"], tuple(q["expected_sections"])) for q in raw]


def matches(retrieved: str, expected: str) -> bool:
    return retrieved == expected or retrieved.startswith(expected + ".")


def first_hit_rank(retrieved: Sequence[str], expected: Sequence[str]) -> int | None:
    for rank, sid in enumerate(retrieved, start=1):
        if any(matches(sid, e) for e in expected):
            return rank
    return None


def score_rankings(
    questions: Sequence[Question], retrieved: dict[str, list[str]]
) -> dict[str, Any]:
    """Metrics over in-scope questions.

    ``retrieved`` maps question id -> retrieved section ids, best first."""
    in_scope = [q for q in questions if q.in_scope]
    ranks = {q.id: first_hit_rank(retrieved.get(q.id, []), q.expected_sections) for q in in_scope}
    out: dict[str, Any] = {"n": len(in_scope)}
    for k in KS:
        out[f"recall@{k}"] = mean(1.0 if (r and r <= k) else 0.0 for r in ranks.values())
    out["mrr"] = mean((1.0 / r) if r else 0.0 for r in ranks.values())
    out["misses@5"] = sorted(qid for qid, r in ranks.items() if not r or r > 5)
    by_type: dict[str, float] = {}
    for t in QUESTION_TYPES[:-1]:
        typed = [ranks[q.id] for q in in_scope if q.type == t]
        if typed:
            by_type[t] = mean(1.0 if (r and r <= 5) else 0.0 for r in typed)
    out["recall@5_by_type"] = by_type
    return out


def choose_winner(combos: dict[str, dict[str, Any]], n_questions: int) -> str:
    """Apply the pre-declared selection rule. Keys look like 'hybrid/text-embedding-3-small'
    or 'keyword' (keyword search needs no embedding model)."""
    one_question = 1.0 / n_questions if n_questions else 0.0
    best_recall = max(m["recall@5"] for m in combos.values())
    tied = {k: m for k, m in combos.items() if m["recall@5"] >= best_recall - one_question - 1e-9}
    best_mrr = max(m["mrr"] for m in tied.values())
    tied = {k: m for k, m in tied.items() if m["mrr"] >= best_mrr - 0.01 - 1e-9}

    def cost(key: str) -> tuple[int, int, str]:
        method, _, model = key.partition("/")
        return (MODEL_COST.get(model, 99), METHOD_COMPLEXITY[method], key)

    return min(tied, key=cost)


def top_similarities(
    questions: Sequence[Question], top_score: Callable[[Question], float]
) -> dict[str, Any]:
    """Top vector similarity for in-scope vs out-of-scope questions (for a later abstain rule)."""
    ins = [top_score(q) for q in questions if q.in_scope]
    oos = [top_score(q) for q in questions if not q.in_scope]
    return {
        "in_scope_min": min(ins),
        "in_scope_mean": mean(ins),
        "out_of_scope_max": max(oos),
        "out_of_scope_mean": mean(oos),
        "separable": max(oos) < min(ins),
    }
