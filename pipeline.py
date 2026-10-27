"""Daily run: scrape -> read CRM -> match -> propose -> store.

This job never writes to the CRM and never approves anything. Writes happen
only from the review app, one approved proposal at a time. If the scrape is
incomplete, no proposals are generated: a missing website page would otherwise
look like "facility no longer listed" and flag healthy accounts.

    python pipeline.py [--db data/entitysync.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from crm_client import CRMClient
from matcher import find_parent_id, match_all
from proposal_store import DEFAULT_DB_PATH, ProposalStore
from rules import build_plan
from scraper import save_snapshot, scrape

DEFAULT_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"


def run(db_path: Path, parent_name: str) -> int:
    scraped = scrape()
    save_snapshot(scraped, Path("data/locations.json"))
    print(f"website: {len(scraped.locations)} locations from {scraped.pages_crawled} pages; stated {scraped.stated_counts}")
    if scraped.problems():
        for issue in scraped.problems():
            print("PROBLEM:", issue)
        print("aborting: scrape incomplete, no proposals generated")
        return 1

    accounts = CRMClient().list_accounts()  # read-only client
    Path("data/accounts.json").write_text(json.dumps(accounts, indent=1))
    parent_id = find_parent_id(accounts, parent_name)
    print(f"crm: {len(accounts)} accounts; parent {parent_name!r} = {parent_id}")

    matches, orphans = match_all(scraped.locations, accounts, parent_id)
    plan = build_plan(matches, orphans, parent_id, parent_name)
    counts = ProposalStore(db_path).save_plan(plan, accounts)
    print(f"plan: {len(plan.proposals)} proposals, {len(plan.unchanged)} locations already correct")
    print(f"store: {counts}")
    return 0


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--parent-name", default=os.environ.get("PARENT_NAME", DEFAULT_PARENT_NAME))
    args = parser.parse_args()
    sys.exit(run(args.db, args.parent_name))
