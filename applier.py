"""Apply one approved proposal to the CRM, safely and restartably.

For one proposal:
  1. Preconditions: re-fetch every account it will update and compare with the
     snapshot the reviewer saw. If parent, revenue, AR, status or the
     duplicate/CHOW links changed, or a field being changed now holds some
     third value, stop with status 'stale' and write nothing.
  2. Operations, in order, each under an idempotency key (proposal + step):
       * already done in the operations ledger -> skip
       * create -> check before create (ledger, then a CRM search for the key
         stamped in the note); only if not found, POST with the key in the note
         and record the new id immediately
       * update -> resolve the @new placeholder, merge the note with the live
         note, PATCH
  3. Verify: re-read every account and check the target values, and that an
     updated account's other guarded fields did not change (a CHOW keeps the
     old account exactly as it was apart from chow_current_account).
Any error leaves status 'failed' with the message; applying again resumes from
the ledger, so a create is never repeated.
"""
from __future__ import annotations

from typing import Any

from crm_client import CRMClient
from proposal_store import ProposalStore, op_key
from rules import NEW_ACCOUNT_REF

GUARDED_FIELDS = ("parent_id", "lifetime_revenue", "outstanding_ar", "status", "duplicate_of_account", "chow_current_account")
KEY_TAG = "[entitysync-op:{}]"


class ApplyError(RuntimeError):
    pass


def apply_proposal(store: ProposalStore, client: CRMClient, fp: str) -> dict[str, Any]:
    proposal = store.get(fp)
    if proposal["status"] not in ("approved", "failed"):
        raise ApplyError(f"proposal {fp} is {proposal['status']}; only approved (or failed, to retry) proposals are applied")
    if not client.allow_writes:
        raise ApplyError("client is read-only; build it with allow_writes=True on the approval path")

    payload = proposal["payload"]
    ops, snapshots = payload["operations"], payload["snapshots"]
    store.set_status(fp, "applying")
    try:
        problems = _check_preconditions(store, client, fp, ops, snapshots)
        if problems:
            result = {"stale": problems}
            store.set_status(fp, "stale", result)
            return result
        created_id = _execute(store, client, fp, ops)
        mismatches = _verify(client, ops, snapshots, created_id)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
        store.set_status(fp, "failed", result)
        return result

    result = {"created_account_id": created_id, "verified": not mismatches, "mismatches": mismatches}
    store.set_status(fp, "failed" if mismatches else "applied", result)
    return result


def _check_preconditions(store, client, fp, ops, snapshots) -> list[str]:
    problems = []
    for step, op in enumerate(ops):
        aid = op["account_id"]
        done = store.get_operation(op_key(fp, step))
        if op["op"] != "update" or (done and done["status"] == "done"):
            continue  # creates have no prior state; finished steps are expected to differ from the snapshot
        current, snap = client.get_account(aid), snapshots.get(aid)
        if snap is None:
            problems.append(f"{aid}: no snapshot recorded")
            continue
        targets = op["fields"]
        for f in set(GUARDED_FIELDS) | (set(targets) - {"note"}):
            target = targets.get(f)
            if current.get(f) != snap.get(f) and (target == NEW_ACCOUNT_REF or current.get(f) != target):
                problems.append(f"{aid}.{f} changed since review: was {snap.get(f)!r}, now {current.get(f)!r}")
    return problems


def _execute(store, client, fp, ops) -> str | None:
    created_id = None
    for step, op in enumerate(ops):
        key = op_key(fp, step)
        done = store.get_operation(key)
        if done and done["status"] == "done":
            created_id = done["account_id"] if op["op"] == "create" else created_id
            continue

        if op["op"] == "create":
            existing = (done or {}).get("account_id") or _find_created(client, op["fields"], key)
            if existing:
                store.record_operation(key, fp, step, "done", existing, {"recovered": True})
                created_id = existing
                continue
            fields = {**op["fields"], "note": f"{op['fields'].get('note', '')} {KEY_TAG.format(key)}".strip()}
            store.record_operation(key, fp, step, "sent")  # intent is logged before the request
            response = client.create_account(fields)
            new_id = response.get("account_id")
            if not new_id:
                raise ApplyError(f"create returned no account_id: {response!r}")
            store.record_operation(key, fp, step, "done", new_id, response)
            created_id = new_id
        else:
            fields = _resolve(op["fields"], created_id)
            if "note" in fields:
                fields["note"] = _merge_note(client.get_account(op["account_id"])["note"], fields["note"])
            response = client.update_account(op["account_id"], fields)
            store.record_operation(key, fp, step, "done", op["account_id"], response)
    return created_id


def _verify(client, ops, snapshots, created_id) -> list[str]:
    mismatches = []
    for op in ops:
        aid = created_id if op["op"] == "create" else op["account_id"]
        current = client.get_account(aid)
        targets = _resolve(op["fields"], created_id)
        for f, v in targets.items():
            ok = _note_addition(v) in (current.get(f) or "") if f == "note" else current.get(f) == v
            if not ok:
                mismatches.append(f"{aid}.{f}: expected {v!r}, CRM has {current.get(f)!r}")
        if op["op"] == "update" and aid in snapshots:
            for f in set(GUARDED_FIELDS) - set(targets):
                if current.get(f) != snapshots[aid].get(f):
                    mismatches.append(f"{aid}.{f} changed unexpectedly: {snapshots[aid].get(f)!r} -> {current.get(f)!r}")
    return mismatches


def _find_created(client, fields, key) -> str | None:
    """Recover an account created earlier whose response was lost: search near its address for our key."""
    tag = KEY_TAG.format(key)
    for acct in client.list_accounts(zip=fields.get("billing_zip", "")):
        if tag in (acct.get("note") or ""):
            return acct["account_id"]
    return None


def _resolve(fields: dict, created_id: str | None) -> dict:
    if NEW_ACCOUNT_REF in fields.values() and not created_id:
        raise ApplyError("an update refers to the new account, but no account was created")
    return {k: (created_id if v == NEW_ACCOUNT_REF else v) for k, v in fields.items()}


def _note_addition(planned_note: str) -> str:
    """The line EntitySync adds: the last line of the planned note."""
    return planned_note.strip().splitlines()[-1] if planned_note.strip() else ""


def _merge_note(live_note: str, planned_note: str) -> str:
    """Append our line to whatever the note says now, so edits made after review are kept."""
    live, addition = (live_note or "").strip(), _note_addition(planned_note)
    if addition in live:
        return live
    return f"{live}\n{addition}" if live else addition
