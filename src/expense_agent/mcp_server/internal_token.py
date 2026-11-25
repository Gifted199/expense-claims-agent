"""Create the shared internal token secret for agent API -> MCP server calls (D33).

``uv run python -m expense_agent.mcp_server.internal_token --profile aero --create-secret``

Creates ``akinsds1126-expense-mcp-internal`` with a random 32-byte token. It refuses to overwrite
an existing secret, and never prints the value.
"""

from __future__ import annotations

import argparse
import json
import secrets
from collections.abc import Sequence
from typing import Any

from expense_agent.db.connection import DbSettings, add_profile_argument, make_session
from expense_agent.mcp_server.auth import TOKEN_KEY, token_secret_id


def new_token() -> str:
    return secrets.token_urlsafe(32)


def create_secret(sm: Any, secret_id: str) -> str:
    """Create the secret; raise if it already exists. Returns its ARN (never the value)."""
    try:
        sm.describe_secret(SecretId=secret_id)
    except sm.exceptions.ResourceNotFoundException:
        pass
    else:
        raise SystemExit(f"secret {secret_id!r} already exists; not overwriting it")
    response = sm.create_secret(
        Name=secret_id,
        Description="Shared internal token: expense agent API -> MCP server (decision D33)",
        SecretString=json.dumps({TOKEN_KEY: new_token()}),
    )
    return str(response["ARN"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--create-secret", action="store_true", required=True)
    args = parser.parse_args(argv)
    session = make_session(args.profile or None, DbSettings.from_env().region)
    arn = create_secret(session.client("secretsmanager"), token_secret_id())
    print(f"created {arn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
