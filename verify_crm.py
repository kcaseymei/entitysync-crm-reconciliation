"""Read-only check of the CRM's end state against the website.

    python verify_crm.py

Never writes. For the parent company it checks:
  1. every website location has exactly one live account under the parent
     (live = not Inactive, not a duplicate, not replaced via CHOW)
  2. every CHOW link points to an existing account under the parent, and the
     historical account stayed under its old parent
  3. every duplicate is Inactive and points to an existing live account
  4. every other Active account under the parent is flagged Needs Review
  5. nothing is left to propose, except proposals the reviewer rejected
Exits 0 when all checks pass.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

from crm_client import CRMClient
from matcher import find_parent_id, is_parent_account, match_all, match_location
from proposal_store import DEFAULT_DB_PATH, ProposalStore, fingerprint
from rules import build_plan, is_superseded
from scraper import Location, scrape

DEFAULT_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"


@dataclass
class Check:
    name: str
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def is_live(acct: dict) -> bool:
    return acct["status"] != "Inactive" and not is_superseded(acct)


def verify(locations: list[Location], accounts: list[dict], parent_id: str, parent_name: str,
           decisions: dict[str, str]) -> list[Check]:
    """`decisions` maps proposal fingerprint -> ledger status (pending, applied, rejected, ...)."""
    by_id = {a["account_id"]: a for a in accounts}

    one_each = Check(f"each of the {len(locations)} website locations has exactly one live account under the parent")
    covered: set[str] = set()
    for loc in locations:
        candidates = [c.account for c in match_location(loc, accounts).candidates]
        covered |= {a["account_id"] for a in candidates}
        live = [a for a in candidates if is_live(a) and a["parent_id"] == parent_id]
        if len(live) != 1:
            ids = ", ".join(a["account_id"] for a in live) or "none"
            one_each.issues.append(f"{loc.name} ({loc.city}, {loc.state}): {len(live)} live accounts ({ids})")

    chow = Check("CHOW links point to a live account under the parent; the old account kept its parent")
    for a in accounts:
        if not a["chow_current_account"]:
            continue
        new = by_id.get(a["chow_current_account"])
        if new is None:
            chow.issues.append(f"{a['account_id']} points to missing account {a['chow_current_account']}")
        elif new["parent_id"] != parent_id or not is_live(new):
            chow.issues.append(f"{a['account_id']} -> {new['account_id']} is not a live account under the parent")
        elif a["parent_id"] == parent_id:
            chow.issues.append(f"{a['account_id']} was moved to the parent; a CHOW keeps the old account's parent")
        else:
            chow.notes.append(f"{a['name']}: {a['account_id']} ({a['parent_name']}) -> {new['account_id']}")

    dupes = Check("duplicates are Inactive and point to a live account")
    for a in accounts:
        if not a["duplicate_of_account"]:
            continue
        keep = by_id.get(a["duplicate_of_account"])
        problems = []
        if a["status"] != "Inactive":
            problems.append(f"{a['account_id']} is marked duplicate but is {a['status']}")
        if keep is None or not is_live(keep):
            problems.append(f"{a['account_id']} points to {a['duplicate_of_account']}, which is not a live account")
        dupes.issues += problems
        if not problems:
            dupes.notes.append(f"{a['name']} [{a['account_id']}] -> {a['duplicate_of_account']}")

    orphans = Check("other Active accounts under the parent are flagged Needs Review")
    for a in accounts:
        if (a["parent_id"] == parent_id and not is_parent_account(a) and a["account_id"] not in covered
                and a["status"] == "Active" and not is_superseded(a)):
            orphans.issues.append(f"{a['name']} [{a['account_id']}] is Active under the parent but not on the website")

    remaining = Check("nothing left to propose, except proposals the reviewer rejected")
    plan = build_plan(*match_all(locations, accounts, parent_id), parent_id, parent_name)
    for p in plan.proposals:
        status = decisions.get(fingerprint(p), "not in the ledger")
        line = f"{p.kind} · {p.title} ({status})"
        (remaining.notes if status == "rejected" else remaining.issues).append(line)

    return [one_each, chow, dupes, orphans, remaining]


def main() -> int:
    load_dotenv()
    parent_name = os.environ.get("PARENT_NAME", DEFAULT_PARENT_NAME)
    scraped = scrape()
    if scraped.problems():
        print("cannot verify: the scrape is incomplete:", *scraped.problems(), sep="\n  ")
        return 2
    accounts = CRMClient().list_accounts()  # read-only client
    parent_id = find_parent_id(accounts, parent_name)
    decisions = {p["fingerprint"]: p["status"] for p in ProposalStore(DEFAULT_DB_PATH).list()}

    checks = verify(scraped.locations, accounts, parent_id, parent_name, decisions)
    print(f"website: {len(scraped.locations)} locations · CRM: {len(accounts)} accounts · parent {parent_id}\n")
    for c in checks:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}")
        for line in c.issues:
            print(f"      ✗ {line}")
        for line in c.notes:
            print(f"      · {line}")
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
