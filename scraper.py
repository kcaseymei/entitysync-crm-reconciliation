"""Scrape every Bellhaven community from the public website.

Crawls the whole (small) site instead of trusting the paginated directory:
at least one community is linked only from the homepage. Each detail page is
parsed into a Location; pages that fail to parse are recorded, never silently
dropped, and the found count is checked against the counts the site states.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

DEFAULT_SITE_BASE = "https://analyst-assessment-production.up.railway.app"
DETAIL_PATH = re.compile(r"^/communities/[^/?#]+$")
CITY_LINE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
STATED_COUNT = re.compile(r"(\d+)\s+communities", re.IGNORECASE)

Fetch = Callable[[str], str]


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: tuple[str, ...]
    phone: str
    administrator: str
    source_url: str


@dataclass
class ScrapeResult:
    locations: list[Location]
    failures: list[tuple[str, str]]  # (url, reason)
    pages_crawled: int
    stated_counts: dict[str, int]  # where the site states a total -> that number
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def problems(self) -> list[str]:
        issues = [f"failed to parse {url}: {reason}" for url, reason in self.failures]
        for where, n in self.stated_counts.items():
            if len(self.locations) < n:
                issues.append(f"{where} states {n} communities but only {len(self.locations)} were scraped")
        return issues


def parse_detail(html: str, url: str) -> Location:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    fields = {dt.get_text(strip=True): dt.find_next_sibling("dd") for dt in soup.select("dl.detail dt")}
    if not h1 or "Address" not in fields:
        raise ParseError("missing name or address block")

    address_lines = [line.strip() for line in fields["Address"].get_text("\n").split("\n") if line.strip()]
    if len(address_lines) < 2:
        raise ParseError(f"unexpected address format: {address_lines!r}")
    city_match = CITY_LINE.match(address_lines[-1])
    if not city_match:
        raise ParseError(f"unparseable city/state/zip line: {address_lines[-1]!r}")

    care = fields.get("Care Offerings")
    text = lambda key: fields[key].get_text(" ", strip=True) if key in fields else ""
    return Location(
        slug=urlparse(url).path.rsplit("/", 1)[-1],
        name=h1.get_text(" ", strip=True),
        street=", ".join(address_lines[:-1]),
        city=city_match["city"],
        state=city_match["state"],
        zip=city_match["zip"],
        care_offerings=tuple(b.get_text(strip=True) for b in care.select(".badge")) if care else (),
        phone=text("Phone"),
        administrator=text("Administrator"),
        source_url=url,
    )


def scrape(base_url: str | None = None, fetch: Fetch | None = None, max_pages: int = 300) -> ScrapeResult:
    """Breadth-first crawl of every same-site page, parsing each community detail page found."""
    load_dotenv()
    base_url = (base_url or os.environ.get("SITE_BASE") or DEFAULT_SITE_BASE).rstrip("/")
    fetch = fetch or _http_fetch
    host = urlparse(base_url).netloc

    queue = deque([f"{base_url}/", f"{base_url}/communities"])
    seen = set(queue)
    locations: dict[str, Location] = {}
    failures: list[tuple[str, str]] = []
    stated: dict[str, int] = {}
    fetched = 0

    while queue and fetched < max_pages:
        url = queue.popleft()
        fetched += 1
        try:
            html = fetch(url)
        except Exception as exc:  # network or HTTP error: record and keep crawling
            failures.append((url, f"fetch failed: {type(exc).__name__}"))
            continue

        path = urlparse(url).path
        if DETAIL_PATH.match(path):
            try:
                loc = parse_detail(html, url)
                locations[loc.slug] = loc
            except ParseError as exc:
                failures.append((url, str(exc)))

        soup = BeautifulSoup(html, "html.parser")
        if path in ("/", "/communities") and (m := STATED_COUNT.search(soup.get_text(" "))):
            stated.setdefault("homepage" if path == "/" else "directory", int(m[1]))

        for a in soup.find_all("a", href=True):
            link = urldefrag(urljoin(url, a["href"]))[0]
            if urlparse(link).netloc == host and link not in seen:
                seen.add(link)
                queue.append(link)

    if queue:
        failures.append(("crawl", f"stopped at max_pages={max_pages} with {len(queue)} pages unvisited"))
    return ScrapeResult(
        locations=sorted(locations.values(), key=lambda l: l.slug),
        failures=failures,
        pages_crawled=fetched,
        stated_counts=stated,
    )


def _http_fetch(url: str) -> str:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def save_snapshot(result: ScrapeResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(result), "problems": result.problems()}
    path.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    result = scrape()
    out = Path("data/locations.json")
    save_snapshot(result, out)
    print(f"pages crawled: {result.pages_crawled}")
    print(f"stated counts: {result.stated_counts}")
    print(f"locations scraped: {len(result.locations)}  (saved to {out})")
    for issue in result.problems():
        print("PROBLEM:", issue)
    sys.exit(1 if result.problems() else 0)
