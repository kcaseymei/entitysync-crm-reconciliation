"""Review app tests: run the real Streamlit script headlessly against a temp DB and a fake CRM."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from factories import OTHER_PARENT_ID, PARENT_ID, PARENT_NAME, account, location
from fake_crm import FakeCRM
from matcher import match_all
from proposal_store import ProposalStore
from rules import build_plan

APP = Path(__file__).resolve().parent.parent / "app.py"
LOC = location("Bellhaven of Lima", "3115 N Cole St", "Lima", zip="45801")
ACCOUNTS = [account("A1", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", zip="45801",
                    parent_id=OTHER_PARENT_ID, revenue=47000, ar=0)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    store = ProposalStore(db)
    store.save_plan(build_plan(*match_all([LOC], ACCOUNTS, PARENT_ID), PARENT_ID, PARENT_NAME), ACCOUNTS)
    fake = FakeCRM(ACCOUNTS)
    monkeypatch.setenv("ENTITYSYNC_DB", str(db))
    monkeypatch.setenv("CRM_API_TOKEN", "secret-token-should-never-render")
    monkeypatch.setattr("crm_client.CRMClient", lambda **kwargs: fake)
    fp = store.list()[0]["fingerprint"]
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    return at, store, fake, fp


def page_text(at):
    return " ".join(str(e.value) for e in [*at.markdown, *at.caption, *at.warning, *at.success, *at.error])


def test_pending_proposal_is_shown_with_evidence(setup):
    at, _, fake, _ = setup
    text = page_text(at)
    assert "Re-parent · Bellhaven of Lima" in text
    assert "Move from Cedar Trail Communities to Bellhaven Senior Living" in text
    assert "street + ZIP match" in text
    assert "secret-token" not in text
    assert fake.updates == 0 and fake.creates == 0  # viewing never writes


def test_reject_records_decision_and_writes_nothing(setup):
    at, store, fake, fp = setup
    at.text_input(key=f"reason-{fp}").input("not sure about the parent").run()
    at.button(key=f"reject-{fp}").click().run()
    assert store.get(fp)["status"] == "rejected"
    assert store.get(fp)["decision_reason"] == "not sure about the parent"
    assert fake.updates == 0 and fake.creates == 0


def test_approve_applies_verifies_and_moves_out_of_pending(setup):
    at, store, fake, fp = setup
    at.button(key=f"approve-{fp}").click().run()
    assert not at.exception
    assert store.get(fp)["status"] == "applied"
    assert fake.accounts["A1"]["parent_id"] == PARENT_ID
    assert at.metric[0].value == "0"  # pending count


def render(tmp_path, monkeypatch, locations, accounts):
    db = tmp_path / "cards.db"
    ProposalStore(db).save_plan(build_plan(*match_all(locations, accounts, PARENT_ID), PARENT_ID, PARENT_NAME), accounts)
    monkeypatch.setenv("ENTITYSYNC_DB", str(db))
    monkeypatch.setenv("CRM_API_TOKEN", "unused")
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    return page_text(at)


def test_chow_card_shows_old_and_new_account_in_one_table(tmp_path, monkeypatch):
    loc = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883")
    old = account("OLD9", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883",
                  parent_id=OTHER_PARENT_ID, revenue=84000, ar=12400)
    text = render(tmp_path, monkeypatch, [loc], [old])
    assert "CHOW · Bellhaven of Tiffin" in text
    assert "Old account · stays as it is" in text and "New account · will be created" in text
    assert "| OLD9 | assigned by the CRM on create |" in text
    assert f"| parent_id | {OTHER_PARENT_ID} | **{PARENT_ID}** ✏️ |" in text
    assert "$84,000" in text and "(unchanged)" not in text


def test_duplicate_card_shows_the_kept_copy(tmp_path, monkeypatch):
    loc = location("Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867")
    accounts = [account("KEEP1", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867"),
                account("DUP2", "Bellhaven of Owosso", "1120 West Main Street", "Owosso", "MI", "48867")]
    text = render(tmp_path, monkeypatch, [loc], accounts)
    assert "Duplicate · Bellhaven of Owosso" in text
    assert "Kept copy (survivor)" in text
    assert "| account_id | DUP2 | DUP2 | KEEP1 |" in text
    assert "**KEEP1** ✏️" in text  # duplicate_of_account after


@pytest.fixture
def chow_app(tmp_path, monkeypatch):
    loc = location("Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883")
    accounts = [account("OLD9", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", zip="44883",
                        parent_id=OTHER_PARENT_ID, revenue=84000, ar=12400)]
    db = tmp_path / "chow.db"
    store = ProposalStore(db)
    store.save_plan(build_plan(*match_all([loc], accounts, PARENT_ID), PARENT_ID, PARENT_NAME), accounts)
    fake = FakeCRM(accounts)
    monkeypatch.setenv("ENTITYSYNC_DB", str(db))
    monkeypatch.setenv("CRM_API_TOKEN", "unused")
    monkeypatch.setattr("crm_client.CRMClient", lambda **kwargs: fake)
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    return at, store, store.list()[0]["fingerprint"]


def test_status_changes_in_the_same_redraw_as_the_click(chow_app):
    at, store, fp = chow_app
    at.button(key=f"approve-{fp}").click().run()
    assert not at.exception
    assert "now **applied**" in page_text(at)  # confirmation at the top of the redrawn page
    at.radio(key="status-filter").set_value("all").run()
    assert ":green-badge[applied]" in page_text(at)


def test_applied_chow_shows_real_new_account_id(chow_app):
    at, _, fp = chow_app
    at.button(key=f"approve-{fp}").click().run()
    at.radio(key="status-filter").set_value("applied").run()
    text = page_text(at)
    assert "New account · created" in text
    assert "| account_id | OLD9 | **NEW1** ✏️ |" in text
    assert "→ **NEW1** ✏️" in text


def test_reject_then_reopen_returns_to_pending(chow_app):
    at, store, fp = chow_app
    at.button(key=f"reject-{fp}").click().run()
    assert store.get(fp)["status"] == "rejected"
    at.radio(key="status-filter").set_value("rejected").run()
    at.button(key=f"reopen-{fp}").click().run()
    assert store.get(fp)["status"] == "pending"


def test_decided_proposal_in_older_payload_format_still_renders(chow_app):
    # Decided proposals keep the payload they were approved with; an older CHOW format nested the new account.
    at, store, fp = chow_app
    payload = store.get(fp)["payload"]
    payload["after"] = {"old account": {}, "new account": {"parent_name": PARENT_NAME}}
    store.db.execute("UPDATE proposals SET payload = ?, status = 'applied', result = ? WHERE fingerprint = ?",
                     (json.dumps(payload), json.dumps({"created_account_id": "NEW7", "verified": True}), fp))
    store.db.commit()
    at.radio(key="status-filter").set_value("all").run()
    assert not at.exception
    assert "CHOW · Bellhaven of Tiffin" in page_text(at)


# ---- threading: Streamlit runs each rerun and its callbacks in a new thread ----------------

def test_a_store_connection_cannot_cross_threads(tmp_path):
    """The failure mode the callbacks must avoid (AppTest runs single-threaded, so it can't show it)."""
    import sqlite3
    import threading

    store = ProposalStore(tmp_path / "t.db")
    errors = []
    worker = threading.Thread(target=lambda: errors.append(_try(store.list, sqlite3.ProgrammingError)))
    worker.start()
    worker.join()
    assert errors == [True]


def test_button_callbacks_open_their_own_store():
    """Callbacks run in the next rerun's thread; they must not use the module-level `store` of the previous run."""
    import ast

    tree = ast.parse(APP.read_text())
    callbacks = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name.startswith("on_")]
    assert {f.name for f in callbacks} >= {"on_approve", "on_reject", "on_retry", "on_reopen"}
    for f in callbacks:
        names = {n.id for n in ast.walk(f) if isinstance(n, ast.Name)}
        assert "store" not in names, f"{f.name} uses the previous run's store"
        assert "open_store" in names, f"{f.name} should open its own store"


def _try(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


def test_create_card_shows_one_table_without_an_empty_crm_column(tmp_path, monkeypatch):
    loc = location("Bellhaven at Union Square", "118 Union Square Dr", "New Albany", zip="43054")
    text = render(tmp_path, monkeypatch, [loc], [])
    assert "Create · Bellhaven at Union Square" in text
    assert "New account · will be created" in text
    assert f"| parent_name | {PARENT_NAME} |" in text
    assert f"| parent_id | {PARENT_ID} |" in text
    assert "CRM account(s) as reviewed" not in text
