"""Container health check without curl: ``python -m expense_agent.healthcheck <url>``.

Exit 0 if the URL answers 2xx within 3 seconds, else 1. Used by the Dockerfile HEALTHCHECK and the
ECS task definition health checks (decision D52).
"""

from __future__ import annotations

import sys
import urllib.request
from collections.abc import Sequence


def healthy(url: str, timeout: float = 3.0) -> bool:
    if not url.startswith("http://127.0.0.1:"):  # only ever probes this container's own port
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed scheme/host
            return 200 <= resp.status < 300
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return 0 if len(args) == 1 and healthy(args[0]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
