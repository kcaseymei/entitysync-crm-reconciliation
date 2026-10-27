"""Scraper tests against a tiny fake site whose HTML mirrors the real one's structure."""
import pytest

from scraper import ParseError, parse_detail, scrape

BASE = "https://site.test"


def detail_html(name, street, city_line, badges=("Assisted Living",), phone="(555) 000-0000"):
    badge_html = "".join(f'<span class="badge">{b}</span>' for b in badges)
    return f"""<html><body><div class="wrap"><h1>{name}</h1><dl class="detail">
      <dt>Address</dt><dd>{street}<br>{city_line}</dd>
      <dt>Care Offerings</dt><dd>{badge_html}</dd>
      <dt>Administrator</dt><dd>Pat Lee</dd>
      <dt>Phone</dt><dd>{phone}</dd>
    </dl><a href="/communities">Back</a></div></body></html>"""


def page(body):
    return f'<html><body><nav><a href="/">Home</a><a href="/communities">Communities</a></nav>{body}</body></html>'


def fake_site(overrides=None):
    """Directory: 2 pages, 2 communities listed. Homepage: links a 3rd 'hidden' community."""
    pages = {
        f"{BASE}/": page('<p>We serve 3 communities.</p><a href="/communities/hidden-one">New!</a>'
                         '<a href="https://external.example/x">partner</a>'),
        f"{BASE}/communities": page('<p>Page 1 of 2 · 2 communities listed</p>'
                                    '<a href="/communities/alpha">Alpha</a><a href="/communities?page=2">Next</a>'),
        f"{BASE}/communities?page=2": page('<a href="/communities/beta">Beta</a><a href="/communities/alpha">Alpha again</a>'),
        f"{BASE}/communities/alpha": detail_html("Alpha House", "1 Main St", "Akron, OH 44313"),
        f"{BASE}/communities/beta": detail_html("Beta Place", "2 Oak Ave", "Erie, PA 16505", ("Memory Support",)),
        f"{BASE}/communities/hidden-one": detail_html("Hidden One", "3 Elm Rd", "Findlay, OH 45840"),
    }
    pages.update(overrides or {})
    requested = []

    def fetch(url):
        requested.append(url)
        if url not in pages:
            raise ConnectionError(url)
        return pages[url]

    return fetch, requested


# ---- detail parsing --------------------------------------------------------

def test_parses_all_fields_including_multiple_care_offerings():
    html = detail_html("Bellhaven Rehabilitation &amp; Nursing of Grove City", "3985 Broadway",
                       "New Carlisle, OH 45344", ("Assisted Living", "Memory Support"))
    loc = parse_detail(html, f"{BASE}/communities/grove-city")
    assert loc.name == "Bellhaven Rehabilitation & Nursing of Grove City"
    assert (loc.street, loc.city, loc.state, loc.zip) == ("3985 Broadway", "New Carlisle", "OH", "45344")
    assert loc.care_offerings == ("Assisted Living", "Memory Support")
    assert loc.slug == "grove-city"
    assert loc.phone == "(555) 000-0000"


def test_zip_plus_four_is_reduced_to_five_digits():
    loc = parse_detail(detail_html("X", "1 A St", "Akron, OH 44313-1234"), f"{BASE}/communities/x")
    assert loc.zip == "44313"


def test_multi_line_street_is_kept():
    loc = parse_detail(detail_html("X", "1 A St<br>Suite 200", "Akron, OH 44313"), f"{BASE}/communities/x")
    assert loc.street == "1 A St, Suite 200"


@pytest.mark.parametrize("html", [
    "<html><body><h1>No address</h1></body></html>",
    detail_html("Bad city line", "1 A St", "Akron Ohio"),
])
def test_unparseable_pages_raise(html):
    with pytest.raises(ParseError):
        parse_detail(html, f"{BASE}/communities/bad")


# ---- crawling --------------------------------------------------------------

def test_finds_community_linked_only_from_homepage():
    fetch, _ = fake_site()
    result = scrape(BASE, fetch)
    assert [l.slug for l in result.locations] == ["alpha", "beta", "hidden-one"]
    assert result.problems() == []


def test_each_page_fetched_once_and_external_links_ignored():
    fetch, requested = fake_site()
    scrape(BASE, fetch)
    assert len(requested) == len(set(requested))
    assert not any("external.example" in u for u in requested)


def test_stated_counts_are_captured():
    fetch, _ = fake_site()
    assert scrape(BASE, fetch).stated_counts == {"homepage": 3, "directory": 2}


def test_broken_detail_page_is_reported_not_dropped():
    fetch, _ = fake_site({f"{BASE}/communities/beta": "<html><body><h1>Beta</h1></body></html>"})
    result = scrape(BASE, fetch)
    assert [l.slug for l in result.locations] == ["alpha", "hidden-one"]
    assert any("beta" in issue for issue in result.problems())
    # 2 scraped < 3 stated on homepage, so the shortfall is flagged too
    assert any("homepage states 3" in issue for issue in result.problems())


def test_unreachable_page_is_reported():
    fetch, _ = fake_site()

    def flaky_fetch(url):
        if url.endswith("/beta"):
            raise ConnectionError(url)
        return fetch(url)

    result = scrape(BASE, flaky_fetch)
    assert any("beta" in url and "fetch failed" in reason for url, reason in result.failures)


def test_page_limit_is_reported_not_silent():
    fetch, _ = fake_site()
    result = scrape(BASE, fetch, max_pages=2)
    assert any("max_pages" in issue for issue in result.problems())
