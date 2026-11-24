"""Set passwords for app_readonly / app_writer from the Secrets Manager secret.

    uv run python -m expense_agent.db.app_roles --profile aero [--create-secret]

- If the secret ``akinsds1126-expense-db-app`` exists, its passwords are reused (no surprise
  rotation) and applied to the roles.
- If it does not exist, the script stops unless ``--create-secret`` is given; then it generates
  strong random passwords, creates the secret FIRST, and only then sets the role passwords.
Passwords are never printed or written to disk.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from typing import Any

from expense_agent.db.connection import (
    APP_ROLE_NAMES,
    DbSettings,
    SecretNotFoundError,
    add_profile_argument,
    connect_kwargs,
    get_master_credentials,
    get_secret_json,
    make_session,
)

TAGS = [
    {"Key": "Project", "Value": "akinsds1126-expense"},
    {"Key": "ManagedBy", "Value": "expense_agent.db.app_roles"},
]


def new_secret_payload(host: str, port: int, dbname: str) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        **{
            name: {"username": name, "password": secrets.token_urlsafe(32)}
            for name in APP_ROLE_NAMES.values()
        },
    }


def alter_role_password(conn: Any, role: str, password: str) -> None:
    """DDL can't take bind parameters, so psycopg composes the statement with safe quoting."""
    from psycopg import sql

    conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument(
        "--create-secret", action="store_true", help="create the app secret if it does not exist"
    )
    args = parser.parse_args(argv)

    settings = DbSettings.from_env()
    session = make_session(args.profile, settings.region)
    master = get_master_credentials(session, settings)

    try:
        payload = get_secret_json(session, settings.app_secret_id)
        print(f"using existing secret {settings.app_secret_id}")
    except SecretNotFoundError:
        if not args.create_secret:
            print(
                f"secret {settings.app_secret_id} does not exist; re-run with --create-secret",
                file=sys.stderr,
            )
            return 1
        payload = new_secret_payload(master.host, master.port, settings.dbname)
        session.client("secretsmanager").create_secret(
            Name=settings.app_secret_id,
            Description="Expense agent DB app roles (app_readonly, app_writer)",
            SecretString=json.dumps(payload),
            Tags=TAGS,
        )
        print(f"created secret {settings.app_secret_id}")

    with psycopg.connect(**connect_kwargs(master, settings), autocommit=True) as conn:
        for role in APP_ROLE_NAMES.values():
            alter_role_password(conn, role, payload[role]["password"])
            print(f"password set for {role}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
