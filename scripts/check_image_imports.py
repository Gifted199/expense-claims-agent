"""Import every module an image runs, inside an env that has ONLY that image's dependency groups.

CI runs, for each image (see .github/workflows/ci.yml):
  uv run --isolated --no-dev --no-default-groups --group G python scripts/check_image_imports.py IMG
so a missing dependency (e.g. httpx missing from the ui group) fails CI instead of the deploy.
"""

from __future__ import annotations

import importlib
import sys

MODULES: dict[str, tuple[str, ...]] = {
    "ui": (
        "streamlit",
        "httpx",
        "expense_agent.ui.client",
        "expense_agent.ui.helpers",
        "expense_agent.healthcheck",
    ),
    "agent": (
        "httpx",
        "uvicorn",
        "fastapi",
        "multipart",
        "langchain.agents",
        "langchain_openai",
        "langchain_mcp_adapters.client",
        "langgraph.checkpoint.postgres.aio",
        "psycopg_pool",
        "mcp.client.streamable_http",
        "expense_agent.api.app",
        "expense_agent.api.backend",
        "expense_agent.api.__main__",
        "expense_agent.agent.graph",
        "expense_agent.agent.llm",
        "expense_agent.agent.memory",
        "expense_agent.mcp_server.auth",
        "expense_agent.mcp_server.services",
        "expense_agent.healthcheck",
    ),
    "mcp": (
        "uvicorn",
        "openai",
        "joblib",
        "sklearn",
        "numpy",
        "mcp.server.fastmcp",
        "expense_agent.mcp_server.server",
        "expense_agent.mcp_server.__main__",
        "expense_agent.ml.models",
        "expense_agent.ml.serving",
        "expense_agent.rag.azure",
        "expense_agent.ocr.textract",
        "expense_agent.healthcheck",
    ),
}


def main(image: str) -> int:
    failed = []
    for name in MODULES[image]:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report every failure
            failed.append(f"{name}: {exc!r}")
    for line in failed:
        print(f"[FAIL] {image}: {line}")
    print(f"{image}: {len(MODULES[image]) - len(failed)}/{len(MODULES[image])} imports OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
