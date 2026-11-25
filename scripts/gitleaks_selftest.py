"""CI self-test for .gitleaks.toml (decision D23).

Writes two throwaway files into a temp directory at runtime (so no fake key is ever committed)
and runs ``gitleaks dir --config .gitleaks.toml`` on each:

* positive: a random 40-character key assigned to ``api_key`` must be FLAGGED (exit 1);
* negative: a line naming the ``akinsds1126-expense-azure-openai`` entry must PASS (exit 0).

Usage: python scripts/gitleaks_selftest.py ./gitleaks
"""

from __future__ import annotations

import secrets
import string
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"
ALPHABET = string.ascii_letters + string.digits


def fake_key(length: int = 40) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def cases() -> dict[str, tuple[str, int]]:
    """name -> (file content, expected gitleaks exit code)."""
    return {
        "positive": (f'api_key = "{fake_key()}"\n', 1),
        "negative": ('DEFAULT_AZURE_SECRET_ID = "akinsds1126-expense-azure-openai"\n', 0),
    }


def run_case(gitleaks: str, content: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sample.py").write_text(content, encoding="utf-8")
        cmd = [gitleaks, "dir", "--config", str(CONFIG), "--no-banner", "--redact", tmp]
        return subprocess.run(cmd, check=False).returncode  # noqa: S603 - fixed argv


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    failed = False
    for name, (content, expected) in cases().items():
        code = run_case(argv[1], content)
        ok = code == expected
        failed |= not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: exit {code}, expected {expected}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
