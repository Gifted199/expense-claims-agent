"""The handbook is generated from the tables: no money figure may come from anywhere else."""

from __future__ import annotations

import re
from decimal import Decimal

from expense_agent.rag.chunking import chunk_handbook
from expense_agent.rag.handbook import (
    GRADES,
    LATEST,
    PREVIOUS,
    VERSIONS,
    PolicyData,
    money,
    policy_data_from_rows,
    render,
)

MONEY_RE = re.compile(r"£([\d,]+(?:\.\d{2})?)")


def amounts(text: str) -> set[Decimal]:
    return {Decimal(m.replace(",", "")) for m in MONEY_RE.findall(text)}


def section(markdown: str, sid: str) -> str:
    return next(c.text for c in chunk_handbook(markdown) if c.section_id == sid)


def test_city_caps_seeded_in_migration(policy_data: PolicyData) -> None:
    assert len(policy_data.city_caps) == 9
    assert {c.city for c in policy_data.city_caps} == {"London", "Edinburgh", "Manchester"}


def test_every_money_figure_comes_from_the_tables(policy_data: PolicyData) -> None:
    allowed = policy_data.money_values()
    for spec in VERSIONS:
        found = amounts(render(policy_data, spec))
        assert found, spec.version
        assert found <= allowed, f"{spec.version}: {sorted(found - allowed)} not from the DB"


def test_changing_a_limit_in_the_table_changes_the_handbook(policy_data: PolicyData) -> None:
    rows_cat = [
        (c.code, c.name, c.description, c.is_travel, c.receipt_required_over)
        for c in policy_data.categories
    ]
    rows_lim = [
        (x.category_code, x.grade, Decimal("33") if x.category_code == "meals" else x.per_claim,
         x.monthly)
        for x in policy_data.limits
    ]  # fmt: skip
    rows_caps = [(c.city, c.grade, c.nightly_cap) for c in policy_data.city_caps]
    changed = policy_data_from_rows(rows_cat, rows_lim, rows_caps)
    assert "£33" in section(render(changed, LATEST), "EXP-5.1")


def test_limit_tables_match_policy_limits(policy_data: PolicyData) -> None:
    md = render(policy_data, LATEST)
    per_claim, monthly = section(md, "EXP-4.1"), section(md, "EXP-4.2")
    for lim in policy_data.limits:
        name = policy_data.category(lim.category_code).name
        row_pc = next(r for r in per_claim.splitlines() if r.startswith(f"| {name} |"))
        row_m = next(r for r in monthly.splitlines() if r.startswith(f"| {name} |"))
        col = GRADES.index(lim.grade) + 2
        assert row_pc.split("|")[col].strip() == money(lim.per_claim)
        assert row_m.split("|")[col].strip() == money(lim.monthly)


def test_city_caps_and_elsewhere_row_in_hotel_section(policy_data: PolicyData) -> None:
    hotels = section(render(policy_data, LATEST), "EXP-6.3")
    for cap in policy_data.city_caps:
        row = next(r for r in hotels.splitlines() if r.startswith(f"| {cap.city} |"))
        assert row.split("|")[GRADES.index(cap.grade) + 2].strip() == money(cap.nightly_cap)
    elsewhere = next(r for r in hotels.splitlines() if r.startswith("| Elsewhere in the UK |"))
    for g in GRADES:
        assert money(policy_data.limit("hotel", g).per_claim) in elsewhere


def test_section_ids_unique_and_identical_across_versions(policy_data: PolicyData) -> None:
    ids = {}
    for spec in VERSIONS:
        sids = [c.section_id for c in chunk_handbook(render(policy_data, spec))]
        assert len(sids) == len(set(sids)), spec.version
        assert 8 <= len({s for s in sids if "." not in s}) <= 12
        ids[spec.version] = sids
    assert ids[PREVIOUS.version] == ids[LATEST.version]


def test_versions_differ_in_rules_not_ids(policy_data: PolicyData) -> None:
    old, new = render(policy_data, PREVIOUS), render(policy_data, LATEST)
    assert "60 days" in section(old, "EXP-2.1") and "30 days" in section(new, "EXP-2.1")
    assert "60 days" not in new  # the latest never mentions the old deadline
    assert "policy breach" in section(new, "EXP-11.2")
    assert "policy breach" not in section(old, "EXP-11.2")
    assert "| London |" in new and "| London |" not in old


def test_header_has_version_and_effective_date(policy_data: PolicyData) -> None:
    new = render(policy_data, LATEST)
    assert "**Version:** 2026.1" in new and "2026-04-01" in new and "Supersedes:** 2025.1" in new


def test_key_rules_present(policy_data: PolicyData) -> None:
    md = render(policy_data, LATEST)
    prohibited = section(md, "EXP-10.1")
    assert "personal investments or financial products" in prohibited
    assert "shares" in prohibited and "cryptocurrency" in prohibited
    manager = section(md, "EXP-9.2")
    assert "never approved automatically" in manager and "only" in manager
    assert "duplicate" in section(md, "EXP-11.1").lower()


def test_money_formatting() -> None:
    assert money(Decimal("30")) == "£30"
    assert money(Decimal("12.5")) == "£12.50"
    assert money(Decimal("1500")) == "£1,500"
