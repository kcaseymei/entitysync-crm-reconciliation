"""Authenticated client for the Bellhaven CRM sandbox API.

Reads are always allowed. Writes (create/update) raise WritesDisabledError
unless the client was built with allow_writes=True, which only the reviewer's
approve-and-apply path does. Exploration, scraping, matching and the daily
pipeline therefore cannot modify the CRM, even by accident.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterator

import requests
from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
# Transient failures worth retrying. Only GETs are retried: re-sending a POST
# after a timeout could create the same account twice.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class CRMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class WritesDisabledError(CRMError):
    pass


class CRMClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        *,
        allow_writes: bool = False,
        session: requests.Session | None = None,
        page_size: int = 50,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
    ):
        load_dotenv()
        token = token or os.environ.get("CRM_API_TOKEN")
        if not token:
            raise CRMError("CRM_API_TOKEN is not set; copy .env.example to .env and fill it in")
        self.base_url = (base_url or os.environ.get("CRM_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.allow_writes = allow_writes
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def __repr__(self) -> str:
        # Deliberately omits the token.
        return f"CRMClient(base_url={self.base_url!r}, allow_writes={self.allow_writes})"

    # ---- reads -------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def get_account(self, account_id: str) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}")

    def iter_accounts(self, **filters: str) -> Iterator[dict[str, Any]]:
        """Yield every account matching the filters (q, city, state, zip, street, parent_id), across all pages."""
        page, seen = 1, 0
        while True:
            body = self._request("GET", "/accounts", params={**filters, "page": page, "page_size": self.page_size})
            rows, total = body["data"], body["total"]
            yield from rows
            seen += len(rows)
            if not rows or seen >= total:
                break
            page += 1
        if seen != total:
            raise CRMError(f"pagination mismatch: API reported total={total} but returned {seen} rows")

    def list_accounts(self, **filters: str) -> list[dict[str, Any]]:
        return list(self.iter_accounts(**filters))

    # ---- writes (approval path only) ---------------------------------------

    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self._require_writes("update_account")
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields: dict[str, Any]) -> dict[str, Any]:
        self._require_writes("create_account")
        return self._request("POST", "/accounts", json=fields)

    def _require_writes(self, operation: str) -> None:
        if not self.allow_writes:
            raise WritesDisabledError(f"{operation} blocked: this client is read-only (allow_writes=False)")

    # ---- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        attempts = self.max_retries + 1 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.request(method, url, params=params, json=json, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise CRMError(f"{method} {path} failed: {type(exc).__name__}") from None
            else:
                if resp.status_code not in RETRYABLE_STATUSES or attempt == attempts:
                    break
            time.sleep(self.backoff_seconds * 2 ** (attempt - 1))
        if not resp.ok:
            raise CRMError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        return resp.json()


if __name__ == "__main__":
    # Read-only smoke check: python crm_client.py
    from collections import Counter

    client = CRMClient()
    print(client)
    print("auth ok:", bool(client.me()))
    accounts = client.list_accounts()
    print("accounts:", len(accounts))
    for parent, n in Counter(a["parent_name"] or "(no parent)" for a in accounts).most_common():
        print(f"  {n:3}  {parent}")
