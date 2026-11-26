"""Short-term memory: LangGraph AsyncPostgresSaver on RDS (decision D40).

* Runs as ``app_memory`` (own secret), whose search_path is the ``agent_memory`` schema, so the
  saver's tables live there and the role can't see any business table.
* The checkpoint key binds the conversation to the signed-in employee: ``e<id>:<uuid>``.
  One employee can't load another's conversation even by guessing a thread ID.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from expense_agent.db.connection import DbSettings, connect_kwargs, get_app_credentials


class BadThreadId(ValueError):
    pass


def thread_key(employee_id: int, thread_id: str) -> str:
    """The checkpointer's thread_id. ``thread_id`` must be a UUID chosen by the client."""
    try:
        canonical = str(uuid.UUID(str(thread_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BadThreadId("thread_id must be a UUID") from exc
    return f"e{int(employee_id)}:{canonical}"


@asynccontextmanager
async def postgres_saver(session: Any, settings: DbSettings | None = None) -> AsyncIterator[Any]:
    """An AsyncPostgresSaver over a small pool, with its tables set up in agent_memory."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    settings = settings or DbSettings.from_env()
    kwargs = connect_kwargs(get_app_credentials(session, settings, "memory"), settings)
    kwargs.update(autocommit=True, prepare_threshold=0, row_factory=dict_row)
    async with AsyncConnectionPool(
        kwargs=kwargs,
        min_size=1,
        max_size=5,
        open=False,
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
