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
STATUS_COLOR = {"pending": "orange", "applied": "green", "rejected": "gray", "failed": "red", "stale": "red",
                "superseded": "gray", "approved": "blue", "applying": "blue"}
KIND_LABEL = {"UPDATE": "Update", "REPARENT": "Re-parent", "CHOW": "CHOW", "DUPLICATE": "Duplicate",
              "CREATE": "Create", "NEEDS_REVIEW": "Needs review"}
KIND_HELP = {
    "UPDATE": "Fix name or address on an account that is already under the right parent",
    "REPARENT": "Move the account to the correct parent (no billing history with open AR)",
    "CHOW": "Change of ownership: keep the old account as it is, create a new one under the correct parent, link them",
    "DUPLICATE": "Mark a second copy of the same facility as a duplicate of the survivor and set it Inactive",
    "CREATE": "Facility is on the website but has no CRM account",
    "NEEDS_REVIEW": "Flag for a human: not on the website, or the survivor can't be chosen safely",
}
ACCOUNT_FIELDS = ["account_id", "name", "parent_name", "parent_id", "status", "billing_street", "billing_city", "billing_state",
                  "billing_zip", "care_type", "lifetime_revenue", "outstanding_ar", "chow_current_account",
                  "duplicate_of_account"]

st.set_page_config(page_title="EntitySync Review", page_icon="🔗", layout="wide")
DB_PATH = os.environ.get("ENTITYSYNC_DB", str(DEFAULT_DB_PATH))


def open_store() -> ProposalStore:
    """A connection for the current thread. Streamlit runs every rerun (and the callbacks before it) in a new
    thread, and a SQLite connection can't be used from a thread other than the one that opened it."""
    return ProposalStore(DB_PATH)


store = open_store()  # for drawing this run only; callbacks open their own


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


LEGEND = "✏️ written to CRM · gray = shown only, not written"


def shown_only(text: str) -> str:
    return f":gray[{text}]"


def note_line(planned_note: str) -> str:
    """The line EntitySync appends: the last line of the planned note."""
    lines = (planned_note or "").strip().splitlines()
    return "+ " + lines[-1] if lines else "—"


def show_before_after(payload: dict) -> None:
    before, after, ops = payload["before"], payload["after"], payload["operations"]
    keys = list(dict.fromkeys([*before, *after]))
    if len(ops) != 1:  # several accounts flagged at once: rows are account ids, not fields
        md_table(["account", "before (CRM now)", "after (proposed)"],
                 [[k, cell(k, before.get(k)), changed(cell(k, after.get(k)))] for k in keys])
        return
    [op] = ops
    written = op["fields"]
    rows = [[k, cell(k, before.get(k)), changed(cell(k, after.get(k))) if k in written else shown_only(cell(k, after.get(k)))]
            for k in keys if k != "note"]
    if "note" in written:
        current = payload["snapshots"].get(op["account_id"], {}).get("note")
        rows.append(["note", cell("note", current), changed(cell("note", note_line(written["note"])))])
    md_table(["field", "before (CRM now)", "after (proposed)"], rows)
    st.caption(LEGEND)


def show_chow(payload: dict, result: dict | None) -> None:
    """One table: the old account (kept for billing) next to the new account (to be created, or created)."""
    create, link = payload["operations"]
    old, new = payload["snapshots"][link["account_id"]], create["fields"]
    after = payload["after"]  # older payloads nest the new account under "new account"
    parent_name = after.get("new account parent") or after.get("new account", {}).get("parent_name") or new["parent_id"]
    created_id = (result or {}).get("created_account_id")
    new_values = {**new, "account_id": created_id or "assigned by the CRM on create", "parent_name": parent_name,
                  "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": ""}
    rows = []
    for f in ACCOUNT_FIELDS:
        old_cell = cell(f, old.get(f))
        if f == "chow_current_account":
            old_cell = f"{old_cell} → " + changed(created_id or "new account's id")
        new_cell = cell(f, new_values.get(f))
        rows.append([f, old_cell, changed(new_cell) if f in new else shown_only(new_cell)])
    rows.append(["note", cell("note", old.get("note")), changed(cell("note", note_line(new.get("note", ""))))])
    md_table(["field", "Old account · stays as it is (billing history)",
              "New account · created" if created_id else "New account · will be created"], rows)
    st.caption(f"{LEGEND}. On the old account only chow_current_account is written; "
               "its parent, name, status, revenue and AR stay exactly as they are.")


def show_create(payload: dict, result: dict | None) -> None:
    """No CRM account exists yet, so there is nothing to compare against: show the account that will be created."""
    [op] = payload["operations"]
    created_id = (result or {}).get("created_account_id")
    written = op["fields"]
    values = {**written, "account_id": created_id or "assigned by the CRM on create",
              "parent_name": payload["after"].get("parent_name") or written["parent_id"]}
    fields = ["account_id", "name", "parent_name", "parent_id", "status", "billing_street", "billing_city", "billing_state",
              "billing_zip", "care_type", "phone"]
    rows = [[f, changed(cell(f, values.get(f))) if f in written else shown_only(cell(f, values.get(f)))] for f in fields]
    rows.append(["note", changed(cell("note", note_line(written.get("note", ""))))])
    md_table(["field", "New account · created" if created_id else "New account · will be created"], rows)
    st.caption(f"{LEGEND}. No CRM account matches this listing by street + ZIP, street + city, "
               "or exact name in the same city.")


def show_duplicate(payload: dict) -> None:
    """This copy (now / after) next to the copy that is kept."""
    [op] = payload["operations"]
    this = payload["snapshots"][op["account_id"]]
    kept = next(iter(payload["context"].values()), {})
    deciding = set(payload.get("highlight", []))
    kept_plan = payload.get("context_changes", {})
    written = op["fields"]
    rows = []
    for f in ACCOUNT_FIELDS:
        now, kept_value = cell(f, this.get(f)), cell(f, kept.get(f))
        after = changed(cell(f, written[f])) if f in written else now
        label = f
        if f in deciding:  # the rows that decided which copy is kept: bold the compared values
            label, now, kept_value = f"**{f}** ⚖️", f"**{now}**", f"**{kept_value}**"
        if f in kept_plan:  # written by the kept copy's own proposal, not by this one
            kept_value = f"{kept_value} → {shown_only(cell(f, kept_plan[f]))}"
        rows.append([label, now, after, kept_value])
    if "note" in written:
        rows.append(["note", cell("note", this.get("note")), changed(cell("note", note_line(written["note"]))),
                     cell("note", kept.get("note"))])
    md_table(["field", "This copy · now", "This copy · after",
              "Kept copy (survivor) · now → after its own proposal" if kept_plan else "Kept copy (survivor) · unchanged"],
             rows)
    st.caption(f"{LEGEND}. ⚖️ marks the deciding factor for which copy is kept. Gray values in the kept column "
               "are written by the kept copy's own proposal, not this one.")


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


def display_title(p: dict) -> str:
    """Built at render time, so proposals stored in an older format still read the same way."""
    loc, snaps = p["payload"]["location"], list(p["payload"]["snapshots"].values())
    if p["kind"] == "DUPLICATE" and snaps:  # name the copy being retired; several cards share one location
        name = snaps[0]["name"]
    else:
        name = loc["name"] if loc else (snaps[0]["name"] if snaps else p["title"])
    return f"{KIND_LABEL.get(p['kind'], p['kind'])} · {name}"


# Button callbacks run before the page is redrawn, so the card already shows its new status.
def on_approve(fp: str) -> None:
    result = approve_and_apply(open_store(), write_client(), fp, st.session_state.get(f"reason-{fp}", ""))
    st.session_state["flash"] = (fp, "approved", result)


def on_reject(fp: str) -> None:
    reject(open_store(), fp, st.session_state.get(f"reason-{fp}", ""))
    st.session_state["flash"] = (fp, "rejected", None)


def on_retry(fp: str) -> None:
    st.session_state["flash"] = (fp, "retried", apply_proposal(open_store(), write_client(), fp))


def on_reopen(fp: str) -> None:
    open_store().reopen(fp)
    st.session_state["flash"] = (fp, "reopened", None)


def card(p: dict) -> None:
    payload, fp = p["payload"], p["fingerprint"]
    with st.container(border=True):
        st.markdown(f"#### {display_title(p)}")
        st.markdown(f":{STATUS_COLOR.get(p['status'], 'gray')}-badge[{p['status']}] · {p['kind']} · "
                    f"confidence **{p['confidence']}** · `{fp}`")
        if p["decision_reason"]:
            st.caption(f"Reviewer note: {p['decision_reason']}")
        st.markdown(payload.get("summary") or KIND_HELP.get(p["kind"], ""))
        for flag in payload["flags"]:
            st.warning(flag)

        loc = payload["location"]
        st.markdown("**Website (source of truth):** " + (
            f"{loc['name']} · {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']} · "
            f"{', '.join(loc['care_offerings'])} · [listing]({loc['source_url']})" if loc else "_not listed_"))

        if p["kind"] == "CHOW":
            show_chow(payload, p["result"])
        elif p["kind"] == "CREATE":
            show_create(payload, p["result"])
        elif p["kind"] == "DUPLICATE" and payload.get("context"):
            show_duplicate(payload)
        else:
            st.markdown("**Proposed change**")
            show_before_after(payload)
            with st.expander("CRM account(s) as reviewed · full record", expanded=True):
                for acct in payload["snapshots"].values():
                    show_account(acct)

        st.markdown("**Evidence**")
        st.markdown("\n".join(f"- {e}" for e in payload["evidence"]))
        with st.expander("Exact API operations"):
            st.json(payload["operations"])

        show_result(p["result"])
        if p["status"] == "pending":
            st.text_input("Reason (optional)", key=f"reason-{fp}")
            approve_col, reject_col, _ = st.columns([1, 1, 4])
            approve_col.button("✅ Approve & apply", key=f"approve-{fp}", type="primary", on_click=on_approve, args=(fp,))
            reject_col.button("❌ Reject", key=f"reject-{fp}", on_click=on_reject, args=(fp,))
        elif p["status"] == "failed":
            st.button("↻ Retry", key=f"retry-{fp}", on_click=on_retry, args=(fp,))
        elif p["status"] == "rejected":
            st.button("↩ Reopen (back to pending; nothing was written)", key=f"reopen-{fp}", on_click=on_reopen, args=(fp,))


proposals = store.list()
counts = {s: sum(p["status"] == s for p in proposals) for s in STATUSES}

st.title("EntitySync review queue")
st.caption("Bellhaven Senior Living · nothing is written to the CRM until you click Approve")
if flash := st.session_state.pop("flash", None):
    fp, action, result = flash
    title = display_title(store.get(fp))
    status = store.get(fp)["status"]
    message = f"{title}: {action} → now **{status}**"
    (st.success if status in ("applied", "rejected", "pending") else st.error)(message)
metrics = st.columns(5)
for col, s in zip(metrics, ["pending", "applied", "rejected", "failed", "stale"]):
    col.metric(s.capitalize(), counts[s])

with st.sidebar:
    st.header("Filter")
    options = ["pending", "all"] + [s for s in STATUSES if counts[s] and s != "pending"]
    status = st.radio("Status", options, key="status-filter",
                      format_func=lambda s: f"all ({len(proposals)})" if s == "all" else f"{s} ({counts[s]})")
    kinds = sorted({p["kind"] for p in proposals})
    chosen_kinds = st.multiselect("Kind", kinds, default=kinds, key="kind-filter")
    chosen_conf = st.multiselect("Confidence", ["high", "medium", "low"], default=["high", "medium", "low"], key="conf-filter")

shown = [p for p in proposals if status in ("all", p["status"]) and p["kind"] in chosen_kinds
         and p["confidence"] in chosen_conf]
if not shown:
    st.info("Nothing here.")
for p in shown:
    card(p)
