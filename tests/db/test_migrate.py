"""Migration runner logic, with no database."""

from __future__ import annotations

import contextlib
import hashlib
import re
from pathlib import Path

import pytest

from expense_agent.db.migrate import (
    DEFAULT_SQL_DIR,
    Migration,
    MigrationError,
    discover_migrations,
    pending_migrations,
    run,
)


def write(tmp: Path, name: str, body: str = "SELECT 1;") -> None:
    (tmp / name).write_text(body, encoding="utf-8")


def test_discovers_in_version_order(tmp_path: Path) -> None:
    write(tmp_path, "002_b.sql")
    write(tmp_path, "010_c.sql")
    write(tmp_path, "001_a.sql", "CREATE TABLE x();")
    found = discover_migrations(tmp_path)
    assert [m.version for m in found] == [1, 2, 10]
    assert found[0].sha256 == hashlib.sha256(b"CREATE TABLE x();").hexdigest()


def test_rejects_bad_filename(tmp_path: Path) -> None:
    write(tmp_path, "1_bad.sql")
    with pytest.raises(MigrationError, match="bad migration filename"):
        discover_migrations(tmp_path)


def test_rejects_duplicate_version(tmp_path: Path) -> None:
    write(tmp_path, "001_a.sql")
    write(tmp_path, "001_b.sql")
    with pytest.raises(MigrationError, match="duplicate"):
        discover_migrations(tmp_path)


def _m(version: int, sha: str = "a" * 64) -> Migration:
    return Migration(version=version, filename=f"{version:03d}_x.sql", sql="", sha256=sha)


def test_pending_skips_applied() -> None:
    migrations = [_m(1), _m(2), _m(3)]
    assert [m.version for m in pending_migrations(migrations, {1: "a" * 64})] == [2, 3]


def test_nothing_pending_when_all_applied() -> None:
    migrations = [_m(1), _m(2)]
    assert pending_migrations(migrations, {1: "a" * 64, 2: "a" * 64}) == []


def test_edited_applied_migration_fails_loudly() -> None:
    with pytest.raises(MigrationError, match="checksum mismatch"):
        pending_migrations([_m(1, "b" * 64)], {1: "a" * 64})


def test_applied_but_missing_on_disk_fails() -> None:
    with pytest.raises(MigrationError, match="missing on disk"):
        pending_migrations([_m(1)], {1: "a" * 64, 2: "a" * 64})


def test_repo_migrations_are_valid_and_contain_no_passwords() -> None:
    found = discover_migrations(DEFAULT_SQL_DIR)
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    password_value = re.compile(r"PASSWORD\s+('|\$|E')", re.IGNORECASE)
    for m in found:
        assert not password_value.search(m.sql), f"{m.filename} must not set passwords"


def _grant_statements() -> list[str]:
    """Every GRANT statement across all migrations, whole (multi-line statements joined)."""
    statements: list[str] = []
    for m in discover_migrations(DEFAULT_SQL_DIR):
        body = "\n".join(ln.split("--")[0] for ln in m.sql.splitlines())
        statements += [" ".join(st.split()) for st in body.split(";")]
    return [st for st in statements if st.upper().startswith("GRANT")]


def test_labels_only_granted_to_app_ml_and_delete_never_granted() -> None:
    grants = _grant_statements()
    assert grants
    label_grants = [st for st in grants if "claim_labels" in st]
    assert label_grants, "app_ml needs claim_labels for evaluation"
    assert all(st.endswith("TO app_ml") and st.startswith("GRANT SELECT ") for st in label_grants)
    deletes = [st for st in grants if re.search(r"\bDELETE\b", st, re.IGNORECASE)]
    # The only DELETE anywhere: app_ingest replacing RAG chunks (008, D28).
    assert deletes == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON policy_documents, policy_chunks TO app_ingest"
    ]
    revoke = (DEFAULT_SQL_DIR / "009_ingest_no_document_delete.sql").read_text(encoding="utf-8")
    assert "REVOKE DELETE ON policy_documents FROM app_ingest" in revoke


def test_approvals_and_claims_update_tightened_in_005() -> None:
    sql005 = (DEFAULT_SQL_DIR / "005_tighten_app_writer.sql").read_text(encoding="utf-8")
    assert "REVOKE UPDATE ON claims, approvals FROM app_writer" in sql005
    assert "GRANT UPDATE (status, anomaly_score, receipt_s3_key) ON claims TO app_writer" in sql005


class FakeConn:
    """Records SQL; answers the schema_migrations query from `applied`."""

    def __init__(self, applied: dict[int, str]) -> None:
        self.applied = applied
        self.executed: list[str] = []

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def execute(self, query: str, params: tuple | None = None) -> FakeConn:
        self.executed.append(query)
        self._rows = list(self.applied.items()) if "FROM schema_migrations" in query else []
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_run_applies_only_pending_in_order(tmp_path: Path) -> None:
    write(tmp_path, "001_a.sql", "SELECT 'a';")
    write(tmp_path, "002_b.sql", "SELECT 'b';")
    write(tmp_path, "003_c.sql", "SELECT 'c';")
    first = discover_migrations(tmp_path)[0]
    conn = FakeConn({1: first.sha256})
    applied = run(conn, tmp_path)
    assert [m.version for m in applied] == [2, 3]
    bodies = [q for q in conn.executed if q.startswith("SELECT '")]
    assert bodies == ["SELECT 'b';", "SELECT 'c';"]
    assert sum("INSERT INTO schema_migrations" in q for q in conn.executed) == 2


def test_dry_run_applies_nothing(tmp_path: Path) -> None:
    write(tmp_path, "001_a.sql", "SELECT 'a';")
    conn = FakeConn({})
    assert [m.version for m in run(conn, tmp_path, dry_run=True)] == [1]
    assert not any(q.startswith("SELECT '") for q in conn.executed)
