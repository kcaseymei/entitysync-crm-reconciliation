"""Match scraped website locations to CRM accounts (entity resolution).

Address is the primary key: names change when facilities are sold or
rebranded, but buildings don't move. Candidates are retrieved in tiers:

  1. street + ZIP       (high confidence)
  2. street + city      (medium; catches ZIP typos in the CRM)
  3. exact name + city  (medium; catches a changed or placeholder address)

Name similarity is recorded as supporting evidence but is never enough on its
own: a facility with the same name in a different city is a different
facility. Every candidate found is returned, so duplicates surface instead of
being hidden behind a single "best" match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from scraper import Location

STREET_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "pk": "pike", "terrace": "ter", "circle": "cir",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
NAME_SYNONYMS = {"rehab": "rehabilitation", "centre": "center", "the": ""}
EXACT_NAME_SCORE = 95  # for tier 3, names must be near-identical after normalization


def normalize_street(street: str) -> str:
    tokens = re.sub(r"[.,#]", " ", street.lower()).split()
    return " ".join(STREET_ABBREVIATIONS.get(t, t) for t in tokens)


def normalize_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ").replace("health care", "healthcare"))
    return " ".join(w for w in (NAME_SYNONYMS.get(t, t) for t in text.split()) if w)


def name_similarity(a: str, b: str) -> int:
    return round(fuzz.token_sort_ratio(normalize_name(a), normalize_name(b)))


def is_parent_account(account: dict) -> bool:
    return account["name"].endswith("(Parent Account)")


@dataclass(frozen=True)
class Candidate:
    account: dict
    basis: str  # "street+zip" | "street+city" | "name+city"
    name_score: int
    evidence: tuple[str, ...]


@dataclass
class LocationMatch:
    location: Location
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if not self.candidates:
            return "none"
        return "high" if any(c.basis == "street+zip" for c in self.candidates) else "medium"


def _same_city(loc: Location, acct: dict) -> bool:
    return loc.city.lower() == acct["billing_city"].strip().lower() and loc.state == acct["billing_state"].strip().upper()


def match_location(loc: Location, accounts: list[dict]) -> LocationMatch:
    loc_street = normalize_street(loc.street)
    match = LocationMatch(loc)
    for acct in accounts:
        if is_parent_account(acct):
            continue
        acct_street = normalize_street(acct["billing_street"])
        score = name_similarity(loc.name, acct["name"])
        name_ev = f"name similarity {score}/100: website '{loc.name}' vs CRM '{acct['name']}'"

        if acct_street == loc_street and acct["billing_zip"] == loc.zip:
            basis, ev = "street+zip", [f"street + ZIP match: '{loc_street}', {loc.zip}"]
        elif acct_street == loc_street and _same_city(loc, acct):
            basis, ev = "street+city", [
                f"street + city match: '{loc_street}', {loc.city}, {loc.state}",
                f"ZIP differs: CRM {acct['billing_zip']!r} vs website {loc.zip!r}",
            ]
        elif score >= EXACT_NAME_SCORE and _same_city(loc, acct):
            basis, ev = "name+city", [
                f"name + city match in {loc.city}, {loc.state}",
                f"address differs: CRM '{acct['billing_street']}' vs website '{loc.street}'",
            ]
        else:
            continue
        match.candidates.append(Candidate(acct, basis, score, tuple(ev + [name_ev])))
    return match


def match_all(locations: list[Location], accounts: list[dict], parent_id: str) -> tuple[list[LocationMatch], list[dict]]:
    """Match every location. Also return orphans: accounts under the parent that no location matched."""
    matches = [match_location(loc, accounts) for loc in locations]
    matched_ids = {c.account["account_id"] for m in matches for c in m.candidates}
    orphans = [
        a for a in accounts
        if a["parent_id"] == parent_id and a["account_id"] not in matched_ids and not is_parent_account(a)
    ]
    return matches, orphans


def find_parent_id(accounts: list[dict], parent_name: str) -> str:
    hits = [a["account_id"] for a in accounts if a["name"] == parent_name]
    if len(hits) != 1:
        raise ValueError(f"expected exactly one account named {parent_name!r}, found {len(hits)}")
    return hits[0]
