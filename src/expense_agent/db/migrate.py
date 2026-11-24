"""Idempotent SQL migration runner.

Applies ``sql/NNN_name.sql`` files in order, each in its own transaction, and records them in
``schema_migrations`` with a sha256 checksum. Re-running applies nothing. If an applied file
has been edited, it fails loudly: add a new migration instead of editing an old one.

    uv run python -m expense_agent.db.migrate --profile aero [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expense_agent.db.connection import add_profile_argument, connect

DEFAULT_SQL_DIR = Path(__file__).resolve().parents[3] / "sql"
_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    filename    text NOT NULL,
    sha256      char(64) NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """Migrations on disk and in the database disagree."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str
    sha256: str


def discover_migrations(sql_dir: Path) -> list[Migration]:
    """Read ``NNN_name.sql`` files, sorted by version. Rejects bad names and duplicate versions."""
    found: dict[int, Migration] = {}
    for path in sorted(sql_dir.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration filename {path.name!r} (expected NNN_name.sql)")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:03d}: {path.name}")
        raw = path.read_bytes()
        found[version] = Migration(
            version=version,
            filename=path.name,
            sql=raw.decode("utf-8"),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    return [found[v] for v in sorted(found)]


def pending_migrations(
    migrations: Sequence[Migration], applied: Mapping[int, str]
) -> list[Migration]:
    """Return migrations not yet applied. ``applied`` maps version -> sha256 from the DB."""
    on_disk = {m.version for m in migrations}
    missing = sorted(set(applied) - on_disk)
    if missing:
        raise MigrationError(f"applied migrations missing on disk: {missing}")
    for m in migrations:
        if m.version in applied and applied[m.version] != m.sha256:
            raise MigrationError(
                f"{m.filename} was edited after being applied (checksum mismatch). "
                "Never edit an applied migration; add a new one."
            )
    return [m for m in migrations if m.version not in applied]


def fetch_applied(conn: Any) -> dict[int, str]:
    with conn.transaction():
        conn.execute(CREATE_TRACKING_TABLE)
    rows = conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall()
    return {int(v): str(s) for v, s in rows}


def apply_migration(conn: Any, migration: Migration) -> None:
    """Run one migration and record it, atomically."""
    with conn.transaction():
        conn.execute(migration.sql)  # trusted file from the repo; no user input
        conn.execute(
            "INSERT INTO schema_migrations (version, filename, sha256) VALUES (%s, %s, %s)",
            (migration.version, migration.filename, migration.sha256),
        )


def run(conn: Any, sql_dir: Path, *, dry_run: bool = False) -> list[Migration]:
    migrations = discover_migrations(sql_dir)
    todo = pending_migrations(migrations, fetch_applied(conn))
    if not dry_run:
        for m in todo:
            apply_migration(conn, m)
            print(f"applied {m.filename}")
    return todo


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument("--sql-dir", type=Path, default=DEFAULT_SQL_DIR)
    parser.add_argument("--dry-run", action="store_true", help="list pending, apply nothing")
    args = parser.parse_args(argv)

    with connect("master", args.profile, autocommit=True) as conn:
        todo = run(conn, args.sql_dir, dry_run=args.dry_run)
    verb = "would apply" if args.dry_run else "applied"
    print(f"{len(todo)} pending migration(s) {verb}" if todo else "0 pending: database up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
