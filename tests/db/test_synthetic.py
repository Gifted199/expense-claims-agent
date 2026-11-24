"""Generator rules. Pure Python: no database, no AWS."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest

from expense_agent.db import synthetic as s


@pytest.fixture(scope="module")
def ds() -> s.Dataset:
    return s.generate(s.DEFAULT_SEED)


@pytest.fixture(scope="module")
def limits() -> dict[tuple[str, s.Grade], s.PolicyLimit]:
    return s.limit_lookup(s.policy_limits())


@pytest.fixture(scope="module")
def claims_by_id(ds: s.Dataset) -> dict[int, s.Claim]:
    return {c.claim_id: c for c in ds.claims}


@pytest.fixture(scope="module")
def grade_of(ds: s.Dataset) -> dict[int, s.Grade]:
    return {e.employee_id: e.grade for e in ds.employees}


def labels_of(ds: s.Dataset, kind: s.AnomalyType) -> list[s.ClaimLabel]:
    return [lb for lb in ds.labels if lb.anomaly_type == kind]


# --------------------------------------------------------------------------- reproducibility


def test_same_seed_gives_identical_data(ds: s.Dataset) -> None:
    assert s.generate(s.DEFAULT_SEED) == ds


def test_different_seed_gives_different_data(ds: s.Dataset) -> None:
    assert s.generate(7).claims != ds.claims


# --------------------------------------------------------------------------- people


def test_org_shape(ds: s.Dataset) -> None:
    assert len(ds.employees) == 40
    assert len(ds.departments) == 5
    assert {e.grade for e in ds.employees} == set(s.GRADES)
    assert len({e.email for e in ds.employees}) == 40
    assert all(e.email.endswith("@example.com") for e in ds.employees)
    assert all(e.home_currency == "GBP" for e in ds.employees)
    assert Counter(e.grade for e in ds.employees)["director"] == 5


def test_manager_tree_has_one_root_and_no_cycles(ds: s.Dataset) -> None:
    by_id = {e.employee_id: e for e in ds.employees}
    roots = [e for e in ds.employees if e.manager_id is None]
    assert len(roots) == 1 and roots[0].grade == "director"
    for e in ds.employees:
        seen: set[int] = set()
        cur: s.Employee | None = e
        while cur is not None:
            assert cur.employee_id not in seen, "cycle in manager chain"
            seen.add(cur.employee_id)
            cur = by_id[cur.manager_id] if cur.manager_id is not None else None


def test_managers_are_inserted_before_reports(ds: s.Dataset) -> None:
    assert all(e.manager_id is None or e.manager_id < e.employee_id for e in ds.employees)


def test_associates_report_to_a_senior_in_their_department(ds: s.Dataset) -> None:
    by_id = {e.employee_id: e for e in ds.employees}
    for e in ds.employees:
        if e.grade == "associate":
            assert e.manager_id is not None
            mgr = by_id[e.manager_id]
            assert mgr.grade == "senior" and mgr.department_id == e.department_id


# --------------------------------------------------------------------------- claims


def test_claim_volume_and_window(ds: s.Dataset) -> None:
    assert 2700 <= len(ds.claims) <= 3300
    for c in ds.claims:
        assert s.WINDOW_START <= c.incurred_at.date() <= s.WINDOW_END
        assert c.incurred_at <= c.submitted_at < s.AS_OF


def test_amounts_are_positive_pennies(ds: s.Dataset) -> None:
    for c in ds.claims:
        assert c.amount > 0
        assert c.amount == c.amount.quantize(Decimal("0.01"))
        assert c.currency == "GBP"


def test_claim_ids_and_refs_unique(ds: s.Dataset) -> None:
    assert [c.claim_id for c in ds.claims] == list(range(1, len(ds.claims) + 1))
    assert len({c.claim_ref for c in ds.claims}) == len(ds.claims)


def test_every_category_and_employee_used(ds: s.Dataset) -> None:
    assert {c.category_code for c in ds.claims} == {c.code for c in ds.categories}
    assert {c.employee_id for c in ds.claims} == {e.employee_id for e in ds.employees}


@pytest.mark.parametrize("seed", [s.DEFAULT_SEED, 1, 7, 15])
def test_seasonality_december_client_entertainment_spike(seed: int) -> None:
    """Christmas client hospitality: December clearly above a typical month."""
    data = s.generate(seed)
    per_month = Counter(
        c.incurred_at.month for c in data.claims if c.category_code == "client_entertainment"
    )
    typical = sum(n for m, n in per_month.items() if m != 12) / 11
    assert per_month[12] > 1.3 * typical


def test_august_is_quieter_than_march(ds: s.Dataset) -> None:
    by_month = Counter(c.incurred_at.month for c in ds.claims)
    assert by_month[8] < by_month[3]


def test_sales_claims_more_than_finance_per_head(ds: s.Dataset) -> None:
    dept = {e.employee_id: e.department_id for e in ds.employees}
    heads = Counter(e.department_id for e in ds.employees)
    claims = Counter(dept[c.employee_id] for c in ds.claims)
    sales, finance = 2, 5
    assert claims[sales] / heads[sales] > 1.5 * claims[finance] / heads[finance]


# --------------------------------------------------------------------------- workflow rules


def test_over_limit_is_never_auto_approved(
    ds: s.Dataset, claims_by_id: dict[int, s.Claim], grade_of: dict[int, s.Grade], limits: dict
) -> None:
    system_approved = {a.claim_id for a in ds.approvals if a.decider_type == "system"}
    for c in ds.claims:
        limit = limits[(c.category_code, grade_of[c.employee_id])].per_claim_limit
        if c.amount > limit:
            assert c.claim_id not in system_approved
            assert c.status in ("pending_manager", "approved", "rejected")
            if c.status != "pending_manager":
                decision = next(a for a in ds.approvals if a.claim_id == c.claim_id)
                assert decision.decider_type == "manager"


def test_statuses_match_approvals(ds: s.Dataset, claims_by_id: dict[int, s.Claim]) -> None:
    decided = {a.claim_id: a for a in ds.approvals}
    assert len(decided) == len(ds.approvals), "one decision per claim"
    for c in ds.claims:
        if c.status in ("submitted", "pending_manager"):
            assert c.claim_id not in decided
        else:
            assert decided[c.claim_id].decision == c.status


def test_no_one_approves_their_own_claim(ds: s.Dataset, claims_by_id: dict[int, s.Claim]) -> None:
    for a in ds.approvals:
        if a.decider_type == "manager":
            assert a.decided_by is not None
            assert a.decided_by != claims_by_id[a.claim_id].employee_id
        else:
            assert a.decided_by is None
        assert a.requested_at <= a.decided_at < s.AS_OF


def test_every_status_present(ds: s.Dataset) -> None:
    assert {c.status for c in ds.claims} == {"submitted", "approved", "pending_manager", "rejected"}


# --------------------------------------------------------------------------- anomalies


def test_anomaly_rate_and_types(ds: s.Dataset) -> None:
    rate = len(ds.labels) / len(ds.claims)
    assert 0.03 <= rate <= 0.05
    assert {lb.anomaly_type for lb in ds.labels} == set(s.ANOMALY_TYPES)
    assert len({lb.claim_id for lb in ds.labels}) == len(ds.labels)


def test_labels_point_at_real_claims(ds: s.Dataset, claims_by_id: dict[int, s.Claim]) -> None:
    for lb in ds.labels:
        assert lb.claim_id in claims_by_id
        if lb.related_claim_id is not None:
            assert lb.related_claim_id in claims_by_id
            assert lb.related_claim_id != lb.claim_id


def test_exact_duplicates_match_original(ds: s.Dataset, claims_by_id: dict[int, s.Claim]) -> None:
    for lb in labels_of(ds, "duplicate_exact"):
        dup, orig = claims_by_id[lb.claim_id], claims_by_id[lb.related_claim_id]  # type: ignore[index]
        assert (dup.employee_id, dup.merchant, dup.amount, dup.incurred_at, dup.description) == (
            orig.employee_id,
            orig.merchant,
            orig.amount,
            orig.incurred_at,
            orig.description,
        )
        assert timedelta(days=1) <= dup.submitted_at - orig.submitted_at <= timedelta(days=11)


def test_near_duplicates_are_close_but_not_identical(
    ds: s.Dataset, claims_by_id: dict[int, s.Claim]
) -> None:
    for lb in labels_of(ds, "duplicate_near"):
        dup, orig = claims_by_id[lb.claim_id], claims_by_id[lb.related_claim_id]  # type: ignore[index]
        assert dup.employee_id == orig.employee_id and dup.merchant == orig.merchant
        assert Decimal("0.01") <= abs(dup.amount - orig.amount) <= Decimal("2.00")
        assert abs(dup.incurred_at - orig.incurred_at) <= timedelta(days=1)
        assert dup.description != orig.description


def test_amount_outliers_far_above_personal_norm(
    ds: s.Dataset, claims_by_id: dict[int, s.Claim]
) -> None:
    medians = s.unlabelled_category_medians(ds)
    for lb in labels_of(ds, "amount_outlier"):
        c = claims_by_id[lb.claim_id]
        assert c.amount >= 4 * medians[(c.employee_id, c.category_code)]


def test_odd_time_claims_are_weekend_or_night(
    ds: s.Dataset, claims_by_id: dict[int, s.Claim]
) -> None:
    for lb in labels_of(ds, "odd_time"):
        c = claims_by_id[lb.claim_id]
        assert s.is_odd_time(c.incurred_at)
        assert c.category_code in s.ODD_TIME_CATEGORIES


def test_normal_non_travel_claims_are_never_odd_time(ds: s.Dataset) -> None:
    labelled = {lb.claim_id for lb in ds.labels}
    non_travel = {c.code for c in ds.categories if not c.is_travel}
    for c in ds.claims:
        if c.claim_id not in labelled and c.category_code in non_travel:
            assert not s.is_odd_time(c.incurred_at)


def test_legit_travel_edge_cases_exist(ds: s.Dataset) -> None:
    """Late taxis and weekend hotels that are NOT anomalies keep detection honest."""
    labelled = {lb.claim_id for lb in ds.labels}
    legit_odd = [
        c
        for c in ds.claims
        if c.claim_id not in labelled and c.category_code in ("taxi", "hotel")
        if s.is_odd_time(c.incurred_at)
    ]
    assert len(legit_odd) >= 10


def test_category_mismatch_merchant_belongs_elsewhere(
    ds: s.Dataset, claims_by_id: dict[int, s.Claim]
) -> None:
    for lb in labels_of(ds, "category_mismatch"):
        c = claims_by_id[lb.claim_id]
        assert s.MERCHANT_CATEGORY[c.merchant] != c.category_code


def test_normal_claims_use_their_own_category_merchants(ds: s.Dataset) -> None:
    labelled = {lb.claim_id for lb in ds.labels}
    for c in ds.claims:
        if c.claim_id not in labelled:
            assert s.MERCHANT_CATEGORY[c.merchant] == c.category_code


def test_split_claims_each_under_limit_but_sum_over(
    ds: s.Dataset,
    claims_by_id: dict[int, s.Claim],
    grade_of: dict[int, s.Grade],
    limits: dict,
) -> None:
    groups: dict[int, list[s.Claim]] = defaultdict(list)
    for lb in labels_of(ds, "split_claim"):
        groups[lb.related_claim_id or lb.claim_id].append(claims_by_id[lb.claim_id])
    assert groups
    for root, parts in groups.items():
        assert any(p.claim_id == root for p in parts), "first part is labelled too"
        assert 2 <= len(parts) <= 3
        limit = limits[(parts[0].category_code, grade_of[parts[0].employee_id])].per_claim_limit
        assert all(p.amount < limit for p in parts)
        assert all(p.amount >= limit * Decimal("0.85") - Decimal("0.01") for p in parts)
        assert sum(p.amount for p in parts) > limit
        assert len({(p.employee_id, p.merchant, p.category_code) for p in parts}) == 1


def test_anomalies_concentrate_in_repeat_offenders(ds: s.Dataset) -> None:
    by_emp = Counter(
        c.employee_id for c in ds.claims if c.claim_id in {lb.claim_id for lb in ds.labels}
    )
    top6 = sum(n for _, n in by_emp.most_common(s.N_REPEAT_OFFENDERS))
    assert top6 / len(ds.labels) >= 0.4


def test_policy_limits_cover_every_category_and_grade(ds: s.Dataset) -> None:
    keys = {(p.category_code, p.grade) for p in ds.policy_limits}
    assert keys == {(c.code, g) for c in ds.categories for g in s.GRADES}
    assert all(p.monthly_limit >= p.per_claim_limit > 0 for p in ds.policy_limits)


def test_monthly_limit_is_never_auto_approved(
    ds: s.Dataset, grade_of: dict[int, s.Grade], limits: dict
) -> None:
    """Running month total (non-rejected, submission order) above the monthly limit -> human."""
    system_approved = {a.claim_id for a in ds.approvals if a.decider_type == "system"}
    totals: dict[tuple[int, str, int, int], Decimal] = defaultdict(Decimal)
    routed_by_month_rule = 0
    for c in sorted(ds.claims, key=lambda c: c.claim_id):
        local = c.incurred_at.astimezone(s.LONDON)
        key = (c.employee_id, c.category_code, local.year, local.month)
        pl = limits[(c.category_code, grade_of[c.employee_id])]
        if totals[key] + c.amount > pl.monthly_limit:
            assert c.claim_id not in system_approved
            routed_by_month_rule += c.amount <= pl.per_claim_limit
        if c.status != "rejected":
            totals[key] += c.amount
    assert routed_by_month_rule > 0, "the monthly rule should route at least one claim"


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_duplicate_and_split_labels_carry_no_odd_time_noise(seed: int) -> None:
    data = s.generate(seed)
    by_id = {c.claim_id: c for c in data.claims}
    for lb in data.labels:
        c = by_id[lb.claim_id]
        assert s.WINDOW_START <= c.incurred_at.date() <= s.WINDOW_END
        if lb.anomaly_type == "split_claim":
            assert not s.is_odd_time(c.incurred_at)
        if lb.anomaly_type == "duplicate_near":
            orig = by_id[lb.related_claim_id]  # type: ignore[index]
            if not s.is_odd_time(orig.incurred_at):
                assert not s.is_odd_time(c.incurred_at)


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_mismatch_never_pairs_overlapping_categories(seed: int) -> None:
    data = s.generate(seed)
    by_id = {c.claim_id: c for c in data.claims}
    for lb in data.labels:
        if lb.anomaly_type == "category_mismatch":
            c = by_id[lb.claim_id]
            pair = frozenset((c.category_code, s.MERCHANT_CATEGORY[c.merchant]))
            assert pair not in s.OVERLAPPING
