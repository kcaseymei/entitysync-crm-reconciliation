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
ACCOUNT_FIELDS = ["name", "account_id", "parent_name", "status", "billing_street", "billing_city", "billing_state",
                  "billing_zip", "care_type", "lifetime_revenue", "outstanding_ar", "chow_current_account",
                  "duplicate_of_account"]

st.set_page_config(page_title="EntitySync Review", page_icon="🔗", layout="wide")
store = ProposalStore(os.environ.get("ENTITYSYNC_DB", str(DEFAULT_DB_PATH)))


def write_client() -> CRMClient:
    return CRMClient(allow_writes=True)


def money(value) -> str:
    return f"${float(value or 0):,.0f}"


def show_account(acct: dict) -> None:
    rows = {f: acct.get(f, "") for f in ACCOUNT_FIELDS}
    rows["lifetime_revenue"], rows["outstanding_ar"] = money(rows["lifetime_revenue"]), money(rows["outstanding_ar"])
    st.table({"field": list(rows), "value": [str(v) for v in rows.values()]})


def show_before_after(before: dict, after: dict) -> None:
    sections = [k for k in after if isinstance(after[k], dict)]
    if sections:  # CHOW: one table per account
        for section in sections:
            st.markdown(f"**{section}**")
            show_before_after(before.get(section, {}), after[section])
        return
    keys = list(dict.fromkeys([*before, *after]))
    st.table({
        "field": keys,
        "before (CRM now)": [str(before.get(k, "—")) for k in keys],
        "after (proposed)": [str(after.get(k, "—")) for k in keys],
    })


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
        st.caption(KIND_HELP.get(p["kind"], ""))
        for flag in payload["flags"]:
            st.warning(flag)

        left, right = st.columns(2)
        with left:
            st.markdown("**Website (source of truth)**")
            loc = payload["location"]
            if loc:
                st.markdown(f"{loc['name']}  \n{loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}  \n"
                            f"Care: {', '.join(loc['care_offerings'])}  \n[{loc['source_url']}]({loc['source_url']})")
            else:
                st.markdown("_Not listed on the website._")
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
