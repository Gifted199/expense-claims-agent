"""Ingest job plumbing with fake DB, S3 and embeddings (no cloud)."""

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import MagicMock

from expense_agent.rag import ingest as I
from expense_agent.rag.chunking import chunk_handbook
from expense_agent.rag.handbook import LATEST, PREVIOUS, PolicyData


class FakeCursor:
    def __init__(self, log: list) -> None:
        self.log = log

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append(("execute", sql, params))

    def executemany(self, sql: str, rows: list) -> None:
        self.log.append(("executemany", sql, rows))


class FakeConn:
    def __init__(self) -> None:
        self.log: list = []
        self.depth = 0

    @contextlib.contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        self.log.append(("begin",))
        yield
        self.log.append(("commit",))

    def cursor(self) -> contextlib.AbstractContextManager[FakeCursor]:
        return contextlib.nullcontext(FakeCursor(self.log))

    def execute(self, sql: str, params: Any = None) -> MagicMock:
        self.log.append(("execute", sql, params))
        result = MagicMock()
        result.rowcount = 7
        return result


def fake_embed(texts: list[str], model: str) -> list[list[float]]:
    return [[float(len(t) % 7)] * 1536 for t in texts]


def test_publish_writes_handbook_and_manifest_per_version(policy_data: PolicyData) -> None:
    s3 = MagicMock()
    docs = I.render_versions(policy_data)
    out = I.publish_handbooks(s3, "bucket", docs)
    keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert keys == [
        "policy/2025.1/handbook.md",
        "policy/2025.1/manifest.json",
        "policy/2026.1/handbook.md",
        "policy/2026.1/manifest.json",
    ]
    manifests = [
        json.loads(c.kwargs["Body"])
        for c in s3.put_object.call_args_list
        if "manifest" in c.kwargs["Key"]
    ]
    assert [m["is_latest"] for m in manifests] == [False, True]
    assert (
        out[LATEST.version][0] == "policy/2026.1/handbook.md" and len(out[LATEST.version][1]) == 64
    )


def test_register_documents_sets_exactly_one_latest_after_upserts() -> None:
    conn = FakeConn()
    I.register_documents(conn, {"2025.1": ("k1", "a" * 64), "2026.1": ("k2", "b" * 64)})
    sqls = [e[1] for e in conn.log if e[0] == "execute"]
    assert sqls == [I.UPSERT_DOCUMENT_SQL, I.UPSERT_DOCUMENT_SQL, I.SET_LATEST_SQL]
    assert conn.log[0] == ("begin",) and conn.log[-1] == ("commit",)
    assert conn.log[-2][2] == (LATEST.version,)


def test_replace_chunks_deletes_then_inserts_in_one_transaction(policy_data: PolicyData) -> None:
    conn = FakeConn()
    docs = I.render_versions(policy_data)
    chunks = chunk_handbook(docs[PREVIOUS.version])
    n = I.replace_chunks(
        conn, PREVIOUS.version, "m1", chunks, fake_embed([c.text for c in chunks], "m1")
    )
    kinds = [e[0] for e in conn.log]
    assert kinds == ["begin", "execute", "executemany", "commit"]
    assert conn.log[1][1] == I.DELETE_CHUNKS_SQL and conn.log[1][2] == (PREVIOUS.version, "m1")
    rows = conn.log[2][2]
    assert n == len(rows) == len(chunks)
    assert all(r[0] == PREVIOUS.version and r[1] is False and r[7] == "m1" for r in rows)
    assert rows[0][8].startswith("[") and rows[0][8].count(",") == 1535


def test_ingest_embeds_every_version_with_every_model(policy_data: PolicyData) -> None:
    conn = FakeConn()
    calls: list[str] = []

    def embed(texts: list[str], model: str) -> list[list[float]]:
        calls.append(model)
        return fake_embed(texts, model)

    counts = I.ingest(conn, I.render_versions(policy_data), ["small", "large"], embed)
    assert sorted(counts) == ["2025.1/large", "2025.1/small", "2026.1/large", "2026.1/small"]
    assert calls == ["small", "large", "small", "large"]
    last = [e for e in conn.log if e[0] == "execute"][-1]
    assert last[1] == I.SYNC_CHUNK_LATEST_SQL and last[2] == (LATEST.version,)
    inserted = [r for e in conn.log if e[0] == "executemany" for r in e[2]]
    assert all(r[1] == (r[0] == LATEST.version) for r in inserted)  # is_latest only on 2026.1


def test_prune_keeps_only_listed_models() -> None:
    conn = FakeConn()
    assert I.prune(conn, ["winner"]) == 7
    sql, params = [e for e in conn.log if e[0] == "execute"][0][1:]
    assert sql == I.PRUNE_SQL and params == (["winner"],)


def test_run_id_format() -> None:
    from datetime import UTC, datetime

    assert (
        I.run_id(datetime(2026, 11, 25, 9, 0, 1, tzinfo=UTC), "abcdef123")
        == "20261125T090001Z-abcdef1"
    )


def test_results_markdown_lists_winner() -> None:
    res = {
        "n_in_scope": 25,
        "n_questions": 30,
        "combos": {"keyword": {"recall@3": 0.5, "recall@5": 0.6, "mrr": 0.4, "misses@5": ["q1"]}},
        "winner": "keyword",
        "latest_only_ok": True,
        "deadline_uses_latest_text": True,
    }
    md = I.results_markdown(res)
    assert "| keyword | 0.500 | 0.600 | 0.400 | q1 |" in md and "**keyword**" in md


def test_prune_refuses_empty_model_list() -> None:
    import pytest

    conn = FakeConn()
    with pytest.raises(ValueError, match="empty model list"):
        I.prune(conn, [])
    assert conn.log == []  # nothing executed
