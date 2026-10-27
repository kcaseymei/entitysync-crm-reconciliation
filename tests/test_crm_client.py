"""CRM client tests. A fake HTTP session stands in for the real API, so these never touch the CRM."""
import pytest

from crm_client import CRMClient, CRMError, WritesDisabledError

FAKE_TOKEN = "test-token-not-real"
BASE = "https://crm.test/api/v1"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.ok = status_code < 400
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Records every request and answers from a scripted list of responses."""

    def __init__(self, responses):
        self.headers = {}
        self.calls = []
        self._responses = list(responses)

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        return self._responses.pop(0)


def make_client(responses, **kwargs):
    session = FakeSession(responses)
    client = CRMClient(token=FAKE_TOKEN, base_url=BASE, session=session, backoff_seconds=0, **kwargs)
    return client, session


def page(rows, total, page_no=1):
    return FakeResponse(200, {"data": rows, "page": page_no, "page_size": 50, "total": total})


def rows(start, n):
    return [{"account_id": f"A{i}"} for i in range(start, start + n)]


# ---- pagination ------------------------------------------------------------

def test_reads_every_page_until_total_reached():
    client, session = make_client([page(rows(0, 50), 121), page(rows(50, 50), 121), page(rows(100, 21), 121)])
    accounts = client.list_accounts()
    assert len(accounts) == 121
    assert len({a["account_id"] for a in accounts}) == 121
    assert [c["params"]["page"] for c in session.calls] == [1, 2, 3]


def test_filters_are_passed_through():
    client, session = make_client([page(rows(0, 2), 2)])
    client.list_accounts(parent_id="P1", zip="45840")
    assert session.calls[0]["params"]["parent_id"] == "P1"
    assert session.calls[0]["params"]["zip"] == "45840"


def test_short_read_raises_instead_of_silently_returning_partial_data():
    # API claims 121 rows but the second page comes back empty.
    client, _ = make_client([page(rows(0, 50), 121), page([], 121)])
    with pytest.raises(CRMError, match="pagination mismatch"):
        client.list_accounts()


# ---- write guard -----------------------------------------------------------

def test_writes_blocked_by_default_and_send_nothing():
    client, session = make_client([])
    with pytest.raises(WritesDisabledError):
        client.update_account("A1", {"parent_id": "P2"})
    with pytest.raises(WritesDisabledError):
        client.create_account({"name": "New"})
    assert session.calls == []


def test_writes_allowed_when_enabled():
    client, session = make_client([FakeResponse(200, {"account_id": "A1", "parent_id": "P2"})], allow_writes=True)
    result = client.update_account("A1", {"parent_id": "P2"})
    assert result["parent_id"] == "P2"
    assert session.calls[0]["method"] == "PATCH"
    assert session.calls[0]["url"] == f"{BASE}/accounts/A1"
    assert session.calls[0]["json"] == {"parent_id": "P2"}


# ---- retries ---------------------------------------------------------------

def test_get_retries_transient_errors():
    client, session = make_client([FakeResponse(503), FakeResponse(200, {"account_id": "A1"})])
    assert client.get_account("A1")["account_id"] == "A1"
    assert len(session.calls) == 2


def test_post_is_never_retried():
    # A retried POST could create a duplicate account, so one failure must surface immediately.
    client, session = make_client([FakeResponse(503), FakeResponse(200)], allow_writes=True)
    with pytest.raises(CRMError) as exc:
        client.create_account({"name": "New"})
    assert exc.value.status == 503
    assert len(session.calls) == 1


def test_client_error_is_not_retried():
    client, session = make_client([FakeResponse(404, {"detail": "not found"})])
    with pytest.raises(CRMError) as exc:
        client.get_account("missing")
    assert exc.value.status == 404
    assert len(session.calls) == 1


# ---- secrets ---------------------------------------------------------------

def test_token_sent_in_header_but_never_shown():
    client, session = make_client([FakeResponse(500), FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    assert session.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert FAKE_TOKEN not in repr(client)
    with pytest.raises(CRMError) as exc:
        client.me()
    assert FAKE_TOKEN not in str(exc.value)


def test_missing_token_fails_fast(monkeypatch):
    monkeypatch.delenv("CRM_API_TOKEN", raising=False)
    monkeypatch.setattr("crm_client.load_dotenv", lambda: None)
    with pytest.raises(CRMError, match="CRM_API_TOKEN"):
        CRMClient(base_url=BASE)
