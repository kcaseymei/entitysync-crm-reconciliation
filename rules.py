"""Business rules: turn matches into proposed CRM changes.

Nothing here writes to the CRM. Each Proposal is a plan (ordered operations,
before/after values and evidence) that a reviewer approves or rejects.

Rules encoded here:
  * CHOW SOP: an account that must change parent AND has lifetime_revenue > 0
    AND outstanding_ar > 0 is left untouched; a new account is created under
    the correct parent and the old account's chow_current_account points to it.
    Otherwise the existing account is re-parented directly.
  * Duplicates: one survivor per facility; losers get duplicate_of_account =
    survivor and status Inactive. If more than one copy has billing history,
    the choice is left to a human (Needs Review).
  * Orphans (under the parent but not on the website) are flagged Needs Review,
    never inactivated or re-parented on absence alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from matcher import Candidate, LocationMatch, normalize_street
from scraper import Location

NEW_ACCOUNT_REF = "@new"  # placeholder for the id a create operation will return
WEAK_SURVIVOR_FLAG = "survivor chosen on weak signals only (no billing-history or parent difference); reviewer should confirm which copy to keep"

CARE_TYPE_FOR_OFFERING = {
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Independent Living": "Independent Living",
}


@dataclass
class Operation:
    op: str  # "create" | "update"
    fields: dict
    account_id: str | None = None  # target of an update; None for create


@dataclass
class Proposal:
    kind: str  # UPDATE | REPARENT | CHOW | DUPLICATE | CREATE | NEEDS_REVIEW
    title: str
    subject: str  # stable identity: "account:<id>" or "location:<slug>"
    operations: list[Operation]
    before: dict
    after: dict
    evidence: list[str]
    confidence: str  # high | medium | low
    flags: list[str] = field(default_factory=list)
    location: Location | None = None
    account: dict | None = None
    summary: str = ""  # one plain sentence for the reviewer
    related: list[str] = field(default_factory=list)  # other account ids shown for context (e.g. the survivor)


@dataclass
class ReconciliationPlan:
    proposals: list[Proposal]
    unchanged: list[LocationMatch]


def short_parent(name: str) -> str:
    return name.removesuffix(" (Parent Account)") or "no parent"


def money(value) -> float:
    return float(value) if value not in (None, "") else 0.0


def requires_chow(account: dict) -> bool:
    return money(account["lifetime_revenue"]) > 0 and money(account["outstanding_ar"]) > 0


def care_type_for(location: Location) -> str:
    mapped = [CARE_TYPE_FOR_OFFERING[o] for o in location.care_offerings if o in CARE_TYPE_FOR_OFFERING]
    return mapped[0] if mapped else ""


def is_superseded(account: dict) -> bool:
    """Already resolved earlier: a duplicate, or a historical account replaced via CHOW."""
    return bool(account["duplicate_of_account"] or account["chow_current_account"])


def choose_survivor(candidates: list[Candidate], location: Location, parent_id: str) -> tuple[Candidate | None, str, bool]:
    """Return (survivor, reason, weak). weak=True when only soft signals separated the top two copies."""
    live = [c for c in candidates if not is_superseded(c.account)]
    if not live:
        return None, "every matching account is already marked duplicate or CHOW-superseded", False
    with_history = [c for c in live if money(c.account["lifetime_revenue"]) > 0]
    if len(with_history) > 1:
        return None, "more than one copy has billing history; a human must pick the survivor", False

    def hard_evidence(c: Candidate) -> tuple[bool, bool]:
        return money(c.account["lifetime_revenue"]) > 0, c.account["parent_id"] == parent_id

    ranked = sorted(live, key=lambda c: (
        tuple(-x for x in hard_evidence(c)),              # 1. billing history, 2. already under the right parent
        -c.name_score,                                    # 3. name closest to the website
        -(c.account["billing_street"] == location.street),  # 4. address formatted like the website
        -(c.account["status"] == "Active"),
        c.account["account_id"],                          # 5. deterministic tie-break
    ))
    winner = ranked[0]
    weak = len(ranked) > 1 and hard_evidence(ranked[0]) == hard_evidence(ranked[1])
    reasons = []
    if money(winner.account["lifetime_revenue"]) > 0:
        reasons.append("only copy with billing history")
    if winner.account["parent_id"] == parent_id:
        reasons.append("already under the correct parent")
    reasons.append(f"name similarity {winner.name_score}")
    return winner, ", ".join(reasons), weak


def build_plan(matches: list[LocationMatch], orphans: list[dict], parent_id: str, parent_name: str) -> ReconciliationPlan:
    proposals: list[Proposal] = []
    unchanged: list[LocationMatch] = []
    for m in matches:
        if not m.candidates:
            proposals.append(_create_proposal(m.location, parent_id, parent_name))
            continue
        found = _proposals_for_match(m, parent_id, parent_name)
        proposals.extend(found) if found else unchanged.append(m)
    proposals.extend(_orphan_proposal(a, parent_name) for a in orphans if a["status"] == "Active" and not is_superseded(a))
    return ReconciliationPlan(proposals, unchanged)


# ---- per-match logic -------------------------------------------------------

def _proposals_for_match(m: LocationMatch, parent_id: str, parent_name: str) -> list[Proposal]:
    loc = m.location
    survivor, why, weak = choose_survivor(m.candidates, loc, parent_id)
    live = [c for c in m.candidates if not is_superseded(c.account)]
    evidence = [e for c in m.candidates for e in (f"[{c.account['account_id']}] {e}" for e in c.evidence)]

    if survivor is None:
        if all(c.account["status"] == "Needs Review" for c in live):
            return []  # nothing live, or already flagged for a human on an earlier run
        return [_ambiguous_proposal(loc, live, why, evidence)]

    confidence = "low" if weak else m.confidence
    out = [_duplicate_proposal(loser, survivor, loc, weak, why) for loser in live if loser is not survivor]
    loser_ids = [c.account["account_id"] for c in live if c is not survivor]
    acct = survivor.account
    flags = [f"survivor chosen among {len(live)} copies: {why}"] if len(live) > 1 else []
    if weak:
        flags.append(WEAK_SURVIVOR_FLAG)

    if acct["parent_id"] != parent_id and requires_chow(acct):
        out.append(_chow_proposal(loc, acct, parent_id, parent_name, evidence, confidence, flags, loser_ids))
        return out

    changes = _field_changes(loc, acct, parent_id)
    if changes:
        kind = "REPARENT" if "parent_id" in changes else "UPDATE"
        if "billing_zip" in changes or "billing_street" in changes:
            flags.append("address fields differ from website; verify before approving")
        if acct["status"] != "Active":
            flags.append(f"account is {acct['status']} but the facility is listed on the website")
        note = _append_note(acct["note"], f"EntitySync: synced to website listing {loc.source_url}")
        fields = {**changes, "note": note}
        out.append(Proposal(
            kind=kind,
            title=f"{'Re-parent' if kind == 'REPARENT' else 'Update'} · {loc.name}",
            summary=_update_summary(acct, changes, parent_name),
            subject=f"account:{acct['account_id']}",
            operations=[Operation("update", fields, acct["account_id"])],
            before={k: acct[k] for k in changes} | ({"parent_name": acct["parent_name"]} if "parent_id" in changes else {}),
            after=changes | ({"parent_name": parent_name} if "parent_id" in changes else {}),
            evidence=evidence + (_money_evidence(acct, "direct re-parent allowed") if "parent_id" in changes else []),
            confidence=confidence,
            flags=flags,
            location=loc,
            account=acct,
            related=loser_ids,
        ))
    return out


def _field_changes(loc: Location, acct: dict, parent_id: str) -> dict:
    changes = {}
    if acct["name"] != loc.name:
        changes["name"] = loc.name
    if normalize_street(acct["billing_street"]) != normalize_street(loc.street):
        changes["billing_street"] = loc.street
    if acct["billing_city"].strip().lower() != loc.city.strip().lower():
        changes["billing_city"] = loc.city.strip()
    if acct["billing_state"].strip() != loc.state:
        changes["billing_state"] = loc.state
    if acct["billing_zip"] != loc.zip:
        changes["billing_zip"] = loc.zip
    if acct["parent_id"] != parent_id:
        changes["parent_id"] = parent_id
    if acct["status"] != "Active":
        changes["status"] = "Active"
    offered = {CARE_TYPE_FOR_OFFERING.get(o) for o in loc.care_offerings}
    if acct["care_type"] not in offered and care_type_for(loc):
        changes["care_type"] = care_type_for(loc)
    return changes


def _new_account_fields(loc: Location, parent_id: str, note: str) -> dict:
    return {
        "name": loc.name,
        "parent_id": parent_id,
        "billing_street": loc.street,
        "billing_city": loc.city,
        "billing_state": loc.state,
        "billing_zip": loc.zip,
        "care_type": care_type_for(loc),
        "phone": loc.phone,
        "status": "Active",
        "note": note,
    }


def _update_summary(acct: dict, changes: dict, parent_name: str) -> str:
    parts = []
    if "parent_id" in changes:
        parts.append(f"move from {short_parent(acct['parent_name'])} to {short_parent(parent_name)}")
    if "name" in changes:
        parts.append(f"rename '{acct['name']}' to '{changes['name']}'")
    if address := [k.removeprefix("billing_") for k in changes if k.startswith("billing_")]:
        parts.append(f"fix {', '.join(address)}")
    parts += [f"set {k} to {changes[k]}" for k in ("status", "care_type") if k in changes]
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] + "."


def _chow_proposal(loc, acct, parent_id, parent_name, evidence, confidence, flags, related) -> Proposal:
    note = f"EntitySync: created by change of ownership from account {acct['account_id']} ({acct['parent_name']}); source {loc.source_url}"
    new_fields = _new_account_fields(loc, parent_id, note)
    return Proposal(
        kind="CHOW",
        title=f"CHOW · {loc.name}",
        summary=(f"Owner changed from {short_parent(acct['parent_name'])} to {short_parent(parent_name)}. "
                 "The old account has billing history and open AR, so it stays exactly as it is; a new account is "
                 "created under the new owner and the old one points to it."),
        subject=f"account:{acct['account_id']}",
        operations=[
            Operation("create", new_fields),
            Operation("update", {"chow_current_account": NEW_ACCOUNT_REF}, acct["account_id"]),
        ],
        before={"chow_current_account": acct["chow_current_account"]},
        after={"chow_current_account": "<new account id>", "new account parent": parent_name},
        evidence=evidence + _money_evidence(acct, "CHOW required: billing history AND open AR"),
        confidence=confidence,
        flags=flags,
        location=loc,
        account=acct,
        related=related,
    )


def _duplicate_proposal(loser: Candidate, survivor: Candidate, loc: Location, weak: bool, why: str) -> Proposal:
    acct, keep = loser.account, survivor.account
    fields = {
        "duplicate_of_account": keep["account_id"],
        "status": "Inactive",
        "note": _append_note(acct["note"], f"EntitySync: duplicate of {keep['account_id']} ({loc.name}, {loc.street}); source {loc.source_url}"),
    }
    return Proposal(
        kind="DUPLICATE",
        title=f"Duplicate · {acct['name']}",
        summary=(f"Same building as '{keep['name']}' [{keep['account_id']}], which is kept ({why}). "
                 f"This copy [{acct['account_id']}] becomes Inactive and points to the kept one. "
                 f"The kept account is brought in line with the website ('{loc.name}') by its own proposal if needed."),
        subject=f"account:{acct['account_id']}",
        operations=[Operation("update", fields, acct["account_id"])],
        before={k: acct[k] for k in ("duplicate_of_account", "status", "parent_name")},
        after={"duplicate_of_account": keep["account_id"], "status": "Inactive"},
        evidence=[f"same facility as survivor {keep['account_id']} '{keep['name']}'", *loser.evidence,
                  *_money_evidence(acct, "no billing history on the losing copy")],
        confidence="low" if weak else ("high" if loser.basis == "street+zip" else "medium"),
        flags=[WEAK_SURVIVOR_FLAG] if weak else [],
        location=loc,
        account=acct,
        related=[keep["account_id"]],
    )


def _create_proposal(loc: Location, parent_id: str, parent_name: str) -> Proposal:
    fields = _new_account_fields(loc, parent_id, f"EntitySync: created from website listing {loc.source_url}")
    return Proposal(
        kind="CREATE",
        title=f"Create · {loc.name}",
        summary=f"Listed on the website in {loc.city}, {loc.state} but has no CRM account. Create it under {short_parent(parent_name)}.",
        subject=f"location:{loc.slug}",
        operations=[Operation("create", fields)],
        before={},
        after=fields | {"parent_name": parent_name},
        evidence=[f"listed on website: {loc.source_url}",
                  "no CRM account at this street + ZIP, street + city, or with this exact name in this city"],
        confidence="high",
        location=loc,
    )


def _orphan_proposal(acct: dict, parent_name: str) -> Proposal:
    fields = {
        "status": "Needs Review",
        "note": _append_note(acct["note"], f"EntitySync: under {parent_name} but not listed on its website; parent left unchanged pending verification"),
    }
    return Proposal(
        kind="NEEDS_REVIEW",
        title=f"Not on website · {acct['name']}",
        summary=(f"Under {short_parent(parent_name)} in the CRM but not listed on its website. Set status to "
                 "Needs Review; the parent is not changed and the account is not inactivated on absence alone."),
        subject=f"account:{acct['account_id']}",
        operations=[Operation("update", fields, acct["account_id"])],
        before={"status": acct["status"]},
        after={"status": "Needs Review"},
        evidence=["no website location matches this account's address or name",
                  "absence from the website alone does not prove a sale or closure",
                  *_money_evidence(acct, "flag only; no parent change")],
        confidence="medium",
        account=acct,
    )


def _ambiguous_proposal(loc: Location, live: list[Candidate], why: str, evidence: list[str]) -> Proposal:
    ops = [Operation("update", {"status": "Needs Review", "note": _append_note(
        c.account["note"], f"EntitySync: possible duplicate at {loc.street}, {loc.city}; {why}")}, c.account["account_id"])
        for c in live]
    return Proposal(
        kind="NEEDS_REVIEW",
        title=f"Ambiguous · {loc.name}",
        summary=f"{len(live)} CRM accounts match this facility and the survivor can't be chosen safely ({why}). Flag all for a human.",
        subject=f"location:{loc.slug}",
        operations=ops,
        before={c.account["account_id"]: c.account["status"] for c in live},
        after={c.account["account_id"]: "Needs Review" for c in live},
        evidence=evidence,
        confidence="low",
        flags=[why],
        location=loc,
    )


def _money_evidence(acct: dict, conclusion: str) -> list[str]:
    return [f"lifetime_revenue={money(acct['lifetime_revenue']):,.0f}, outstanding_ar={money(acct['outstanding_ar']):,.0f} → {conclusion}"]


def _append_note(existing: str, addition: str) -> str:
    existing = (existing or "").strip()
    if addition in existing:
        return existing
    return f"{existing}\n{addition}" if existing else addition
