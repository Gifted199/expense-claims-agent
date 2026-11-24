# expense-claims-agent

An agentic AI Expense Claims Assistant on AWS. Employees chat with it to submit expense claims, upload receipts and ask about spending and expense policy.
See [docs/SPEC.md](docs/SPEC.md) for the design and [docs/PLAN.md](docs/PLAN.md) for the build plan. The full README (architecture, deploy, cost, teardown) comes in Phase 9.

## Quick start (development)

```bash
uv sync
uv run python -m pre_commit install
uv run python -m pytest
uv run python -m pre_commit run --all-files
```

## Secret scanning

gitleaks runs in CI because Windows Application Control blocks unsigned binaries locally; detect-secrets covers local commits.

- **Local:** the `detect-secrets` pre-commit hook checks staged files against `.secrets.baseline`.
- **CI:** `.github/workflows/ci.yml` runs the same pre-commit hooks, then runs gitleaks over the full git history.
