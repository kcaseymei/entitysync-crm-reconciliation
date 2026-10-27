"""In-memory stand-in for the CRM API with switches to simulate failures."""
import copy

from crm_client import CRMError


class FakeCRM:
    allow_writes = True

    def __init__(self, accounts):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.creates = 0
        self.updates = 0
        self.lose_next_create_response = False  # the account is created, but the caller sees an error
        self.fail_updates_to = set()            # account ids whose PATCH fails
        self.ignored_fields = set()             # fields the server silently drops on PATCH

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise CRMError(f"GET /accounts/{account_id} -> HTTP 404", status=404)
        return copy.deepcopy(self.accounts[account_id])

    def list_accounts(self, **filters):
        return [copy.deepcopy(a) for a in self.accounts.values()
                if all(not v or a.get(f"billing_{k}", a.get(k)) == v for k, v in filters.items())]

    def create_account(self, fields):
        self.creates += 1
        new_id = f"NEW{self.creates}"
        self.accounts[new_id] = {
            "account_id": new_id, "parent_name": "", "lifetime_revenue": 0, "outstanding_ar": 0,
            "chow_current_account": "", "duplicate_of_account": "", "note": "", "created_by_candidate": True,
            **copy.deepcopy(fields),
        }
        if self.lose_next_create_response:
            self.lose_next_create_response = False
            raise CRMError("POST /accounts failed: ReadTimeout")
        return copy.deepcopy(self.accounts[new_id])

    def update_account(self, account_id, fields):
        if account_id in self.fail_updates_to:
            raise CRMError(f"PATCH /accounts/{account_id} -> HTTP 500", status=500)
        self.updates += 1
        self.accounts[account_id].update({k: v for k, v in fields.items() if k not in self.ignored_fields})
        return copy.deepcopy(self.accounts[account_id])
