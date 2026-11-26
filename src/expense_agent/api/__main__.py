"""Run the agent API: ``uv run python -m expense_agent.api --profile aero --demo-auth``.

Locally it binds to 127.0.0.1:8000 and talks to the MCP server on 127.0.0.1:8080, RDS, S3 and
Azure OpenAI. Nothing runs locally except this process: no local database, model or LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

DEFAULT_MCP_URL = "http://127.0.0.1:8080/mcp"


def build(profile: str | None, mcp_url: str, demo_auth: bool) -> Any:
    from fastapi import FastAPI

    from expense_agent.agent.graph import AgentRunner, mcp_tool_loader
    from expense_agent.agent.llm import chat_model_from_session
    from expense_agent.agent.memory import postgres_saver
    from expense_agent.api.app import create_app
    from expense_agent.api.backend import CloudBackend
    from expense_agent.mcp_server.auth import load_token
    from expense_agent.mcp_server.services import Services

    services = Services(profile=profile)
    token = load_token(services.session)
    holder: dict[str, Any] = {}

    class LateBackend:
        """Forwards to the CloudBackend created in the lifespan (it needs the async saver)."""

        def __getattr__(self, name: str) -> Any:
            return getattr(holder["backend"], name)

    app = create_app(LateBackend(), demo_auth=demo_auth)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with postgres_saver(services.session) as saver:
            runner = AgentRunner(
                chat_model_from_session(services.session), mcp_tool_loader(mcp_url, token), saver
            )
            holder["backend"] = CloudBackend(services, runner, mcp_url, token)
            yield

    app.router.lifespan_context = lifespan
    return app


def main(argv: Sequence[str] | None = None) -> int:
    from expense_agent.db.connection import add_profile_argument

    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mcp-url", default=os.environ.get("EXPENSE_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument(
        "--demo-auth",
        action="store_true",
        default=os.environ.get("EXPENSE_DEMO_AUTH", "false").lower() == "true",
        help="accept the UI's demo user switcher header (never in production)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if sys.platform == "win32":  # async psycopg can't use the default Proactor loop (dev PC only)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

    import uvicorn

    app = build(args.profile or None, args.mcp_url, args.demo_auth)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
