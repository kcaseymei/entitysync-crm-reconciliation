"""End-state checker tests: a corrected CRM passes; each kind of leftover problem is caught."""
from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from matcher import match_all
from proposal_store import fingerprint
from rules import build_plan
from verify_crm import verify

TIFFIN = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883")
OWOSSO = location("Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867")


def corrected_crm():
    return [
        # CHOW done: old account kept under its old parent, pointing to the new one
        account("OLD", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883", parent_id=OTHER_PARENT_ID,
                revenue=84000, ar=12400, chow_current_account="NEW"),
        account("NEW", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883"),
        # duplicate resolved
        account("KEEP", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867"),
        account("DUP", "Bellhaven of Owosso", "1120 West Main Street", "Owosso", "MI", "48867",
                status="Inactive", duplicate_of_account="KEEP"),
        # orphan flagged
        account("ORPH", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", zip="44870", status="Needs Review"),
    ]


def failed(checks):
    return {c.name.split(" ")[0] + "…": c.issues for c in checks if not c.passed}


def run(accounts, decisions=None, locations=(TIFFIN, OWOSSO)):
    return verify(list(locations), accounts, PARENT_ID, PARENT_NAME, decisions or {})


def test_corrected_crm_passes_every_check():
    checks = run(corrected_crm())
    assert all(c.passed for c in checks), failed(checks)


def test_location_without_a_live_account_under_the_parent_fails():
    accounts = [a for a in corrected_crm() if a["account_id"] != "NEW"]
    accounts[0] = {**accounts[0], "chow_current_account": ""}  # CHOW not done yet
    one_each = run(accounts)[0]
    assert not one_each.passed and "Bellhaven of Tiffin" in one_each.issues[0]


def test_chow_that_moved_the_old_account_fails():
    accounts = corrected_crm()
    accounts[0] = {**accounts[0], "parent_id": PARENT_ID}
    chow = run(accounts)[1]
    assert not chow.passed and "keeps the old account's parent" in chow.issues[0]


def test_duplicate_still_active_fails():
    accounts = corrected_crm()
    accounts[3] = {**accounts[3], "status": "Active"}
    dupes = run(accounts)[2]
    assert not dupes.passed and "marked duplicate but is Active" in dupes.issues[0]


def test_unflagged_orphan_fails():
    accounts = corrected_crm()
    accounts[4] = {**accounts[4], "status": "Active"}
    assert not run(accounts)[3].passed


def test_leftover_proposal_fails_unless_the_reviewer_rejected_it():
    loc = location("Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", zip="44004")
    accounts = [account("ASH", "Bellhaven of Ashtabula", "PO Box 517", "Ashtabula", zip="44004")]
    [p] = build_plan(*match_all([loc], accounts, PARENT_ID), PARENT_ID, PARENT_NAME).proposals

    pending = run(accounts, {fingerprint(p): "pending"}, [loc])[4]
    assert not pending.passed

    rejected = run(accounts, {fingerprint(p): "rejected"}, [loc])[4]
    assert rejected.passed and "rejected" in rejected.notes[0]
