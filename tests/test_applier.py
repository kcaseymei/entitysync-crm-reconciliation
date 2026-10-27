"""Applying approved proposals: preconditions, idempotent creates, crash recovery, verification."""
import pytest

from applier import KEY_TAG, ApplyError, apply_proposal
from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from fake_crm import FakeCRM
from matcher import match_all
from proposal_store import ProposalStore, op_key
from rules import build_plan

TIFFIN = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883",
                  care=("Short-Term Rehabilitation & Nursing",))


def tiffin(revenue, ar):
    return account("OLD", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883", parent_id=OTHER_PARENT_ID,
                   revenue=revenue, ar=ar, care_type="Skilled Nursing")


def approved(accounts, kind):
    """Plan against `accounts`, store it, approve the one proposal of `kind`, return (store, fingerprint)."""
    store = ProposalStore(":memory:")
    store.save_plan(build_plan(*match_all([TIFFIN], accounts, PARENT_ID), PARENT_ID, PARENT_NAME), accounts)
    fp = next(p["fingerprint"] for p in store.list() if p["kind"] == kind)
    store.decide(fp, "approved")
    return store, fp


def test_unapproved_proposal_is_refused():
    accounts = [tiffin(0, 0)]
    store = ProposalStore(":memory:")
    store.save_plan(build_plan(*match_all([TIFFIN], accounts, PARENT_ID), PARENT_ID, PARENT_NAME), accounts)
    with pytest.raises(ApplyError):
        apply_proposal(store, FakeCRM(accounts), store.list()[0]["fingerprint"])


def test_read_only_client_is_refused():
    accounts = [tiffin(0, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    crm.allow_writes = False
    with pytest.raises(ApplyError):
        apply_proposal(store, crm, fp)


def test_reparent_is_applied_and_verified():
    accounts = [tiffin(47000, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    result = apply_proposal(store, crm, fp)
    assert result["verified"] and store.get(fp)["status"] == "applied"
    assert crm.accounts["OLD"]["parent_id"] == PARENT_ID
    assert crm.creates == 0


def test_chow_creates_one_account_and_leaves_old_one_untouched():
    accounts = [tiffin(84000, 12400)]
    store, fp = approved(accounts, "CHOW")
    crm = FakeCRM(accounts)
    result = apply_proposal(store, crm, fp)
    new_id = result["created_account_id"]
    assert store.get(fp)["status"] == "applied" and crm.creates == 1
    old, new = crm.accounts["OLD"], crm.accounts[new_id]
    assert old == {**accounts[0], "chow_current_account": new_id}  # nothing else changed
    assert new["parent_id"] == PARENT_ID and new["name"] == "Bellhaven of Tiffin"
    assert KEY_TAG.format(op_key(fp, 0)) in new["note"]


def test_chow_link_failure_then_retry_does_not_create_twice():
    accounts = [tiffin(84000, 12400)]
    store, fp = approved(accounts, "CHOW")
    crm = FakeCRM(accounts)
    crm.fail_updates_to = {"OLD"}
    first = apply_proposal(store, crm, fp)
    assert "error" in first and store.get(fp)["status"] == "failed" and crm.creates == 1

    crm.fail_updates_to = set()
    second = apply_proposal(store, crm, fp)
    assert second["verified"] and crm.creates == 1
    assert crm.accounts["OLD"]["chow_current_account"] == second["created_account_id"]


def test_lost_create_response_is_recovered_by_idempotency_key():
    # The POST succeeded on the server but the client never saw the new id.
    accounts = [tiffin(84000, 12400)]
    store, fp = approved(accounts, "CHOW")
    crm = FakeCRM(accounts)
    crm.lose_next_create_response = True
    assert "error" in apply_proposal(store, crm, fp)
    assert store.get_operation(op_key(fp, 0))["status"] == "sent"  # intent logged, no id

    result = apply_proposal(store, crm, fp)
    assert result["verified"] and crm.creates == 1
    assert crm.accounts["OLD"]["chow_current_account"] == "NEW1"


def test_changed_crm_state_makes_proposal_stale_and_writes_nothing():
    accounts = [tiffin(47000, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    crm.accounts["OLD"]["outstanding_ar"] = 900  # AR appeared after review: CHOW rule may now apply
    result = apply_proposal(store, crm, fp)
    assert store.get(fp)["status"] == "stale"
    assert any("outstanding_ar" in p for p in result["stale"])
    assert crm.updates == 0 and crm.creates == 0


def test_write_that_does_not_stick_fails_verification():
    accounts = [tiffin(0, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    crm.ignored_fields = {"parent_id"}
    result = apply_proposal(store, crm, fp)
    assert store.get(fp)["status"] == "failed"
    assert any("parent_id" in m for m in result["mismatches"])


def test_note_edited_after_review_is_kept():
    accounts = [tiffin(0, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    crm.accounts["OLD"]["note"] = "Rep: spoke to admin 10/20"
    apply_proposal(store, crm, fp)
    note = crm.accounts["OLD"]["note"]
    assert note.startswith("Rep: spoke to admin 10/20") and "EntitySync" in note


def test_applied_proposal_cannot_be_applied_again():
    accounts = [tiffin(0, 0)]
    store, fp = approved(accounts, "REPARENT")
    crm = FakeCRM(accounts)
    apply_proposal(store, crm, fp)
    with pytest.raises(ApplyError):
        apply_proposal(store, crm, fp)


def test_full_cycle_rerun_after_apply_proposes_nothing_new():
    accounts = [tiffin(84000, 12400)]
    store, fp = approved(accounts, "CHOW")
    crm = FakeCRM(accounts)
    apply_proposal(store, crm, fp)
    after = list(crm.accounts.values())
    counts = store.save_plan(build_plan(*match_all([TIFFIN], after, PARENT_ID), PARENT_ID, PARENT_NAME), after)
    assert counts["new"] == 0
