"""Smoke test: proves the package installs and CI is wired up."""

import expense_agent


def test_package_has_version() -> None:
    assert isinstance(expense_agent.__version__, str)
    assert expense_agent.__version__
