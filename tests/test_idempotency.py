"""Re-run safety: the same situation never comes back to the reviewer once decided."""
import pytest

from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from matcher import match_all
from proposal_store import ProposalStore, fingerprint
from rules import build_plan

LOCS = [
    location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883"),
    location("Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", zip="45103"),
]


def crm():
    return [account("A1", "Old Tiffin Name", "45 St Lawrence Dr", "Tiffin", zip="44883", parent_id=OTHER_PARENT_ID)]


def plan_for(locations, accounts):
    return build_plan(*match_all(locations, accounts, PARENT_ID), PARENT_ID, PARENT_NAME)


@pytest.fixture
def store():
    return ProposalStore(":memory:")


def test_fingerprint_is_stable_and_ignores_note():
    a, b = plan_for(LOCS, crm()).proposals, plan_for(list(reversed(LOCS)), crm()).proposals
    assert sorted(map(fingerprint, a)) == sorted(map(fingerprint, b))
    p = a[0]
    p.operations[0].fields["note"] = "different wording"
    assert fingerprint(p) == fingerprint(plan_for(LOCS, crm()).proposals[0])


def test_fingerprint_changes_when_the_target_changes():
    before = {fingerprint(p) for p in plan_for(LOCS, crm()).proposals}
    renamed = [location("Bellhaven Tiffin Commons", "45 St Lawrence Dr", "Tiffin", zip="44883", slug="bellhaven-of-tiffin"), LOCS[1]]
    after = {fingerprint(p) for p in plan_for(renamed, crm()).proposals}
    assert before != after


def test_second_identical_run_adds_nothing(store):
    first = store.save_plan(plan_for(LOCS, crm()), crm())
    second = store.save_plan(plan_for(LOCS, crm()), crm())
    assert first["new"] == 2
    assert second["new"] == 0 and second["pending_refreshed"] == 2
    assert len(store.list()) == 2


def test_rejected_proposal_is_not_proposed_again(store):
    store.save_plan(plan_for(LOCS, crm()), crm())
    fp = store.list()[0]["fingerprint"]
    store.decide(fp, "rejected", "not convinced")
    counts = store.save_plan(plan_for(LOCS, crm()), crm())
    assert counts["new"] == 0 and counts["already_decided"] == 1
    assert store.get(fp)["status"] == "rejected"
    assert fp not in {p["fingerprint"] for p in store.list("pending")}


def test_pending_proposal_no_longer_generated_is_superseded_then_revived(store):
    store.save_plan(plan_for(LOCS, crm()), crm())
    create_fp = next(p["fingerprint"] for p in store.list() if p["kind"] == "CREATE")
    store.save_plan(plan_for(LOCS[:1], crm()), crm())  # Batavia no longer listed
    assert store.get(create_fp)["status"] == "superseded"
    store.save_plan(plan_for(LOCS, crm()), crm())
    assert store.get(create_fp)["status"] == "pending"


def test_only_pending_proposals_can_be_decided(store):
    store.save_plan(plan_for(LOCS, crm()), crm())
    fp = store.list()[0]["fingerprint"]
    store.decide(fp, "approved")
    with pytest.raises(ValueError):
        store.decide(fp, "rejected")


def test_payload_keeps_snapshot_of_touched_accounts(store):
    store.save_plan(plan_for(LOCS, crm()), crm())
    reparent = next(p for p in store.list() if p["kind"] == "REPARENT")
    assert reparent["payload"]["snapshots"]["A1"]["parent_id"] == OTHER_PARENT_ID
