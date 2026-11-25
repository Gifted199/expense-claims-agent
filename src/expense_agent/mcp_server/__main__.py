"""Run the MCP server: ``uv run python -m expense_agent.mcp_server --profile aero``.

Locally it binds to 127.0.0.1:8080 and uses cloud RDS, S3, Textract and Azure through the AWS
profile. In the container (Phase 6) it binds to 0.0.0.0 on an internal-only port.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from typing import Any

from expense_agent.db.connection import add_profile_argument
from expense_agent.mcp_server.auth import InternalAuthMiddleware, load_token
from expense_agent.mcp_server.server import build_server
from expense_agent.mcp_server.services import Services


def build_app(services: Services, host: str, port: int) -> Any:
    mcp = build_server(services, host=host, port=port)
    return InternalAuthMiddleware(mcp.streamable_http_app(), load_token(services.session))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    import uvicorn

    services = Services(profile=args.profile or None)
    app = build_app(services, args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
