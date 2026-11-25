"""CI self-test for .gitleaks.toml (decision D23).

Writes two throwaway files into a temp directory at runtime (so no fake key is ever committed)
and runs ``gitleaks dir --config .gitleaks.toml`` on each:

* positive: a fixed fake AWS access key ID must be FLAGGED (exit 1) by the default
  ``aws-access-token`` rule. It is deterministic (no random value can slip under an entropy
  threshold or contain a stopword) and assembled from parts at run time, so the full literal
  never appears in the repo and the history scan can't trip on it;
* negative: a line naming the ``akinsds1126-expense-azure-openai`` entry must PASS (exit 0).

Usage: python scripts/gitleaks_selftest.py ./gitleaks [--repeat N]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"
# AKIA + 16 chars of [A-Z2-7]; must not end in EXAMPLE (the rule's own allowlist).
_FAKE_AWS_KEY_PARTS = ("AK", "IA", "Q7RZ2M4K", "XW5TBN6P")


def fake_aws_key() -> str:
    return "".join(_FAKE_AWS_KEY_PARTS)


def cases() -> dict[str, tuple[str, int]]:
    """name -> (file content, expected gitleaks exit code)."""
    return {
        "positive": (f'aws_access_key_id = "{fake_aws_key()}"\n', 1),
        "negative": ('DEFAULT_AZURE_SECRET_ID = "akinsds1126-expense-azure-openai"\n', 0),
    }


def run_case(gitleaks: str, content: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sample.py").write_text(content, encoding="utf-8")
        cmd = [gitleaks, "dir", "--config", str(CONFIG), "--no-banner", "--redact", tmp]
        return subprocess.run(cmd, check=False).returncode  # noqa: S603 - fixed argv


def main(argv: list[str]) -> int:
    args = argv[1:]
    repeat = 1
    if len(args) == 3 and args[1] == "--repeat":
        repeat = int(args[2])
    elif len(args) != 1:
        print(__doc__)
        return 2
    failed = False
    for i in range(1, repeat + 1):
        for name, (content, expected) in cases().items():
            code = run_case(args[0], content)
            ok = code == expected
            failed |= not ok
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] run {i}/{repeat} {name}: exit {code}, expected {expected}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
