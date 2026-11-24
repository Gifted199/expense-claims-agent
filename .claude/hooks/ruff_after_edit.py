"""Claude Code PostToolUse hook: format and lint a Python file after Claude edits it.

Reads the hook payload (JSON) from stdin, takes tool_input.file_path, and runs
`ruff format` then `ruff check --fix` on it. If lint errors remain, prints them to
stderr and exits 2 so Claude sees them and fixes them. Non-Python files are skipped.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - runs the fixed `ruff` executable only, no shell
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or Path(file_path).suffix != ".py" or not Path(file_path).exists():
        return 0

    # Use the ruff from the same environment this script runs in (uv run).
    ruff = [sys.executable, "-m", "ruff"]
    subprocess.run([*ruff, "format", "--quiet", file_path], check=False)  # noqa: S603
    result = subprocess.run(  # noqa: S603
        [*ruff, "check", "--fix", "--quiet", file_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"ruff found issues in {file_path}:\n{result.stdout}{result.stderr}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
