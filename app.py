"""EntitySync review app: approve or reject proposed CRM changes.

    streamlit run app.py

Nothing is written to the CRM until a reviewer clicks Approve. Approve
re-checks the proposal against live CRM data, applies it, and verifies the
result by reading the accounts back. Reject is recorded so the same proposal
is not shown again on later runs.
"""
from __future__ import annotations

import os

import streamlit as st

from applier import apply_proposal, approve_and_apply, reject
from crm_client import CRMClient
from proposal_store import DEFAULT_DB_PATH, ProposalStore

STATUSES = ["pending", "applied", "rejected", "failed", "stale", "superseded", "approved", "applying"]
KIND_HELP = {
    "UPDATE": "Fix name or address on an account that is already under the right parent",
    "REPARENT": "Move the account to the correct parent (no billing history with open AR)",
    "CHOW": "Change of ownership: keep the old account as it is, create a new one under the correct parent, link them",
    "DUPLICATE": "Mark a second copy of the same facility as a duplicate of the survivor and set it Inactive",
    "CREATE": "Facility is on the website but has no CRM account",
    "NEEDS_REVIEW": "Flag for a human: not on the website, or the survivor can't be chosen safely",
}
ACCOUNT_FIELDS = ["account_id", "name", "parent_name", "status", "billing_street", "billing_city", "billing_state",
                  "billing_zip", "care_type", "lifetime_revenue", "outstanding_ar", "chow_current_account",
                  "duplicate_of_account"]

st.set_page_config(page_title="EntitySync Review", page_icon="🔗", layout="wide")
store = ProposalStore(os.environ.get("ENTITYSYNC_DB", str(DEFAULT_DB_PATH)))


def write_client() -> CRMClient:
    return CRMClient(allow_writes=True)


def money(value) -> str:
    return f"${float(value or 0):,.0f}"


def cell(field: str, value) -> str:
    if field in ("lifetime_revenue", "outstanding_ar"):
        return money(value)
    text = str(value).strip() if value not in (None, "") else "—"
    return text.replace("|", "\\|")


def changed(text: str) -> str:
    return f"**{text}** ✏️"


def md_table(header: list[str], rows: list[list[str]]) -> None:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    st.markdown("\n".join(lines))


def show_account(acct: dict) -> None:
    md_table(["field", "value"], [[f, cell(f, acct.get(f))] for f in ACCOUNT_FIELDS])


def show_before_after(before: dict, after: dict) -> None:
    keys = list(dict.fromkeys([*before, *after]))
    md_table(["field", "before (CRM now)", "after (proposed)"],
             [[k, cell(k, before.get(k)), changed(cell(k, after.get(k)))] for k in keys])


def show_chow(payload: dict) -> None:
    """One table: the old account (kept for billing) next to the new account (to be created)."""
    create, link = payload["operations"]
    old, new = payload["snapshots"][link["account_id"]], create["fields"]
    new_values = {**new, "account_id": "assigned by the CRM", "parent_name": payload["after"]["new account parent"],
                  "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": ""}
    rows = []
    for f in ACCOUNT_FIELDS:
        old_cell = cell(f, old.get(f))
        if f == "chow_current_account":
            old_cell = f"{old_cell} → " + changed("new account's id")
        new_cell = cell(f, new_values.get(f))
        if f == "parent_name":
            new_cell = changed(new_cell)
        rows.append([f, old_cell, new_cell])
    md_table(["field", "Old account · stays as it is (billing history)", "New account · will be created"], rows)
    st.caption("✏️ marks what changes. On the old account only chow_current_account is set; "
               "its parent, name, status, revenue and AR stay exactly as they are.")


def show_duplicate(payload: dict) -> None:
    """This copy (now / after) next to the copy that is kept."""
    [op] = payload["operations"]
    this = payload["snapshots"][op["account_id"]]
    kept = next(iter(payload["context"].values()), {})
    rows = []
    for f in ACCOUNT_FIELDS:
        now = cell(f, this.get(f))
        after = changed(cell(f, op["fields"][f])) if f in op["fields"] else now
        rows.append([f, now, after, cell(f, kept.get(f))])
    md_table(["field", "This copy · now", "This copy · after", "Kept copy (survivor) · unchanged here"], rows)
    st.caption("✏️ marks what changes. Only this copy is updated by this proposal.")


def show_result(result: dict | None) -> None:
    if not result:
        return
    if result.get("verified"):
        created = f" New account: `{result['created_account_id']}`." if result.get("created_account_id") else ""
        st.success(f"Applied and verified by reading the CRM back.{created}")
    elif result.get("stale"):
        st.error("Not applied: the CRM changed since this was proposed. Re-run the pipeline to refresh.")
        st.write(result["stale"])
    elif result.get("mismatches"):
        st.error("Written, but verification found differences:")
        st.write(result["mismatches"])
    elif result.get("error"):
        st.error(f"Failed: {result['error']}. Retry resumes where it stopped; nothing is created twice.")
    elif result.get("reason"):
        st.info(result["reason"])


def card(p: dict) -> None:
    payload, fp = p["payload"], p["fingerprint"]
    with st.container(border=True):
        st.markdown(f"#### {p['title']}")
        st.caption(f"{p['kind']} · confidence **{p['confidence']}** · status **{p['status']}** · `{fp}`")
        st.markdown(payload.get("summary") or KIND_HELP.get(p["kind"], ""))
        for flag in payload["flags"]:
            st.warning(flag)

        loc = payload["location"]
        st.markdown("**Website (source of truth):** " + (
            f"{loc['name']} · {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']} · "
            f"{', '.join(loc['care_offerings'])} · [listing]({loc['source_url']})" if loc else "_not listed_"))

        if p["kind"] == "CHOW":
            show_chow(payload)
        elif p["kind"] == "DUPLICATE":
            show_duplicate(payload)
        else:
            left, right = st.columns(2)
            with left:
                st.markdown("**Proposed change**")
                show_before_after(payload["before"], payload["after"])
            with right:
                st.markdown("**CRM account(s) as reviewed**")
                for acct in payload["snapshots"].values():
                    show_account(acct)

        st.markdown("**Evidence**")
        st.markdown("\n".join(f"- {e}" for e in payload["evidence"]))
        with st.expander("Exact API operations"):
            st.json(payload["operations"])

        show_result(p["result"])
        if p["status"] == "pending":
            reason = st.text_input("Reason (optional)", key=f"reason-{fp}")
            approve_col, reject_col, _ = st.columns([1, 1, 4])
            if approve_col.button("✅ Approve & apply", key=f"approve-{fp}", type="primary"):
                with st.spinner("Re-checking, writing, verifying…"):
                    approve_and_apply(store, write_client(), fp, reason)
                st.rerun()
            if reject_col.button("❌ Reject", key=f"reject-{fp}"):
                reject(store, fp, reason)
                st.rerun()
        elif p["status"] == "failed":
            if st.button("↻ Retry", key=f"retry-{fp}"):
                with st.spinner("Resuming…"):
                    apply_proposal(store, write_client(), fp)
                st.rerun()


proposals = store.list()
counts = {s: sum(p["status"] == s for p in proposals) for s in STATUSES}

st.title("EntitySync review queue")
st.caption("Bellhaven Senior Living · nothing is written to the CRM until you click Approve")
metrics = st.columns(5)
for col, s in zip(metrics, ["pending", "applied", "rejected", "failed", "stale"]):
    col.metric(s.capitalize(), counts[s])

with st.sidebar:
    st.header("Filter")
    status = st.radio("Status", [s for s in STATUSES if counts[s] or s == "pending"],
                      format_func=lambda s: f"{s} ({counts[s]})", key="status-filter")
    kinds = sorted({p["kind"] for p in proposals})
    chosen_kinds = st.multiselect("Kind", kinds, default=kinds, key="kind-filter")
    chosen_conf = st.multiselect("Confidence", ["high", "medium", "low"], default=["high", "medium", "low"], key="conf-filter")

shown = [p for p in proposals if p["status"] == status and p["kind"] in chosen_kinds and p["confidence"] in chosen_conf]
if not shown:
    st.info("Nothing here.")
for p in shown:
    card(p)
