"""Shared fixtures: policy data built from the synthetic generator plus the 008 city caps."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from expense_agent.db import synthetic as s
from expense_agent.rag.handbook import Category, CityCap, Limit, PolicyData

SQL_008 = Path(__file__).resolve().parents[2] / "sql" / "008_policy_rag.sql"


def city_caps_from_migration() -> tuple[CityCap, ...]:
    """Read the seeded caps straight from migration 008, so tests use the real reference data."""
    sql = SQL_008.read_text(encoding="utf-8")
    rows = re.findall(r"\('([A-Za-z ]+)',\s*'(associate|senior|director)',\s*([\d.]+)\)", sql)
    return tuple(CityCap(city, grade, Decimal(v)) for city, grade, v in rows)


@pytest.fixture(scope="session")
def policy_data() -> PolicyData:
    return PolicyData(
        categories=tuple(
            Category(c.code, c.name, c.description, c.is_travel, c.receipt_required_over)
            for c in s.CATEGORIES
        ),
        limits=tuple(
            Limit(p.category_code, p.grade, p.per_claim_limit, p.monthly_limit)
            for p in s.policy_limits()
        ),
        city_caps=city_caps_from_migration(),
    )
