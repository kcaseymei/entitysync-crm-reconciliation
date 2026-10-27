"""CHOW SOP tests. Re-parent directly unless lifetime_revenue > 0 AND outstanding_ar > 0."""
import pytest

from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from matcher import match_all
from rules import NEW_ACCOUNT_REF, requires_chow, build_plan

LOC = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883", care=("Short-Term Rehabilitation & Nursing",))


def old_account(revenue, ar, **extra):
    return account("OLD", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883",
                   parent_id=OTHER_PARENT_ID, revenue=revenue, ar=ar, care_type="Skilled Nursing", **extra)


def plan_for(accounts):
    matches, orphans = match_all([LOC], accounts, PARENT_ID)
    return build_plan(matches, orphans, PARENT_ID, PARENT_NAME)


@pytest.mark.parametrize("revenue, ar, expected", [
    (0, 0, False),
    (84000, 0, False),   # revenue only (e.g. Lima): direct re-parent
    (0, 500, False),     # AR only: direct re-parent
    (84000, 12400, True),  # both: CHOW (e.g. Tiffin)
    (None, None, False),
    ("1000", "1", True),   # numeric strings are handled
])
def test_chow_requires_revenue_and_ar(revenue, ar, expected):
    assert requires_chow({"lifetime_revenue": revenue, "outstanding_ar": ar}) is expected


def test_chow_creates_new_account_and_links_old_without_touching_it():
    [p] = plan_for([old_account(84000, 12400)]).proposals
    assert p.kind == "CHOW"
    create, link = p.operations
    assert create.op == "create" and create.fields["parent_id"] == PARENT_ID
    assert create.fields["name"] == "Bellhaven of Tiffin" and create.fields["billing_zip"] == "44883"
    # the old account only gets the CHOW pointer: parent, name, status, note are untouched
    assert link.op == "update" and link.account_id == "OLD"
    assert link.fields == {"chow_current_account": NEW_ACCOUNT_REF}


@pytest.mark.parametrize("revenue, ar", [(0, 0), (47000, 0), (0, 300)])
def test_without_both_money_conditions_account_is_reparented_directly(revenue, ar):
    [p] = plan_for([old_account(revenue, ar)]).proposals
    assert p.kind == "REPARENT"
    [op] = p.operations
    assert op.op == "update" and op.account_id == "OLD"
    assert op.fields["parent_id"] == PARENT_ID


def test_rerun_after_chow_proposes_nothing():
    # State after the CHOW was applied: old account points to the new one, new one is under Bellhaven.
    old = old_account(84000, 12400, chow_current_account="NEW")
    new = account("NEW", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883",
                  care_type="Skilled Nursing", created_by_candidate=True)
    assert plan_for([old, new]).proposals == []


def test_chow_historical_account_is_never_called_a_duplicate():
    old = old_account(84000, 12400, chow_current_account="NEW")
    new = account("NEW", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883", care_type="Skilled Nursing")
    assert not any(p.kind == "DUPLICATE" for p in plan_for([old, new]).proposals)
