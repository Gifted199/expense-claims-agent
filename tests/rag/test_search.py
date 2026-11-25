"""Retrieval logic with a fake connection: RRF, filters, parameterisation, exact IDs."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from expense_agent.rag import search as S


def row(sid: str, idx: int = 0, version: str = "2026.1", score: float = 0.5) -> tuple:
    return (sid, idx, f"title {sid}", f"text {sid}", version, score)


class FakeConn:
    """Answers each SQL constant with canned rows and records (sql, params)."""

    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Any]] = []

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def execute(self, sql: str, params: Any = None) -> FakeConn:
        self.calls.append((sql, params))
        self._rows = next((rows for key, rows in self.answers.items() if sql == key), [])
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def P(sid: str, idx: int = 0) -> S.Passage:
    return S.Passage(sid, idx, "t", "x", "2026.1", 0.0, "vector")


def test_rrf_arithmetic_and_dedup() -> None:
    a = [P("EXP-1.1"), P("EXP-2.1"), P("EXP-3.1")]
    b = [P("EXP-2.1"), P("EXP-4.1")]
    fused = S.rrf([a, b], k=3, rrf_k=60)
    assert [p.section_id for p in fused] == ["EXP-2.1", "EXP-1.1", "EXP-4.1"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)
    assert all(p.method == "hybrid" for p in fused)


def test_rrf_keeps_chunks_of_same_section_apart() -> None:
    fused = S.rrf([[P("EXP-4.1", 0), P("EXP-4.1", 1)]], k=5)
    assert [p.key for p in fused] == [("EXP-4.1", 0), ("EXP-4.1", 1)]


def test_every_query_filters_latest_and_model_and_is_parameterised() -> None:
    for sql in (S.VECTOR_SQL, S.KEYWORD_SQL, S.SECTION_ID_SQL):
        assert "is_latest" in sql and "embedding_model = %s" in sql
        assert "{" not in sql  # no Python formatting


def test_vector_search_passes_literal_and_model_as_params() -> None:
    conn = FakeConn({S.VECTOR_SQL: [row("EXP-6.3", score=0.9)]})
    out = S.vector_search(conn, [0.1, 0.2], "m1", 5)
    sql, params = conn.calls[-1]
    assert sql == S.VECTOR_SQL and params == ("[0.1,0.2]", "m1", "[0.1,0.2]", 5)
    assert out[0].section_id == "EXP-6.3" and out[0].method == "vector"
    assert any("hnsw.iterative_scan" in c[0] for c in conn.calls)


def test_keyword_or_query_is_safe() -> None:
    assert S.or_query("Can I claim a TAXI?! drop table; --") == (
        "can | i | claim | a | taxi | drop | table"
    )
    assert S.or_query("!!!") == ""


def test_exact_section_id_is_ranked_first() -> None:
    conn = FakeConn(
        {
            S.SECTION_ID_SQL: [row("EXP-6.3")],
            S.KEYWORD_SQL: [row("EXP-6.1"), row("EXP-6.3")],
        }
    )
    out = S.keyword_search(conn, "what does exp-6.3 say", "m1", 5)
    assert [p.section_id for p in out] == ["EXP-6.3", "EXP-6.1"]  # no duplicate
    sql, params = conn.calls[0]
    assert sql == S.SECTION_ID_SQL and params == ("m1", ["EXP-6.3"], 5)


def test_hybrid_puts_cited_section_first() -> None:
    conn = FakeConn(
        {
            S.VECTOR_SQL: [row("EXP-4.1"), row("EXP-4.2")],
            S.SECTION_ID_SQL: [row("EXP-4.2")],
            S.KEYWORD_SQL: [row("EXP-4.1")],
        }
    )
    out = S.hybrid_search(conn, "Show me EXP-4.2", [0.0], "m1", 3)
    assert out[0].section_id == "EXP-4.2"
    assert len({p.key for p in out}) == len(out)


def test_search_policy_rejects_unknown_method() -> None:
    with pytest.raises(ValueError, match="unknown retrieval method"):
        S.search_policy(FakeConn({}), "q", lambda t: [0.0], "m1", method="magic")  # type: ignore[arg-type]


def test_search_embeds_question_only_for_vector_methods() -> None:
    seen: list[str] = []

    def embed(text: str) -> list[float]:
        seen.append(text)
        return [0.0]

    conn = FakeConn({})
    S.search(conn, "taxi", "keyword", "m1", embed)
    assert seen == []
    S.search(conn, "taxi", "vector", "m1", embed)
    assert seen == ["taxi"]


def test_vector_id_puts_cited_existing_section_first_then_vector() -> None:
    conn = FakeConn(
        {
            S.SECTION_ID_SQL: [row("EXP-9.1")],
            S.VECTOR_SQL: [row("EXP-9.2"), row("EXP-9.1"), row("EXP-9.3")],
        }
    )
    out = S.vector_id_search(conn, "explain section exp-9.1", [0.0], "m1", 3)
    assert [p.section_id for p in out] == ["EXP-9.1", "EXP-9.2", "EXP-9.3"]  # no duplicate
    assert out[0].method == "exact_id"


def test_vector_id_without_id_is_exactly_vector_search() -> None:
    answers = {S.VECTOR_SQL: [row("EXP-6.3"), row("EXP-6.1")]}
    a, b = FakeConn(answers), FakeConn(answers)
    got = S.vector_id_search(a, "hotel cap in Edinburgh", [0.1], "m1", 2)
    want = S.vector_search(b, [0.1], "m1", 2)
    assert got == want
    assert all(sql != S.SECTION_ID_SQL for sql, _ in a.calls)


def test_vector_id_unknown_id_falls_back_to_vector() -> None:
    conn = FakeConn({S.SECTION_ID_SQL: [], S.VECTOR_SQL: [row("EXP-1.1")]})
    out = S.vector_id_search(conn, "what is EXP-99.9", [0.0], "m1", 3)
    assert [p.section_id for p in out] == ["EXP-1.1"]


def test_default_method_is_vector_with_id_shortcut() -> None:
    assert S.DEFAULT_METHOD == "vector_id" and "vector_id" in S.METHODS
