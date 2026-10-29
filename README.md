# EntitySync — CRM Entity Resolution & Reconciliation Engine

*Automated CRM entity resolution with human-in-the-loop reconciliation.*

> Developed as part of a technical take-home assessment for Clipboard Health, using a fictional senior living operator and a sandbox CRM environment.

**Demo (2.5 min):** reviewing proposals, approving a change of ownership, rejecting one, and checking the result in the CRM.

[![EntitySync demo](https://img.youtube.com/vi/8TruP7GUghI/hqdefault.jpg)](https://youtu.be/8TruP7GUghI)

Senior-care facilities are bought, sold and rebranded all the time, and each change can quietly break the link between a facility and its parent company in the CRM. EntitySync treats the operator's website as the source of truth. It finds every place the CRM disagrees with the website and turns each one into a **proposal**: a planned change with before/after values and evidence. A reviewer approves or rejects each proposal in a small web app. Only approved proposals are written to the CRM, and every write is read back and verified.

## How it works

```
website ──scraper.py──▶ 35 locations ─┐
                                      ├─▶ matcher.py ─▶ rules.py ─▶ proposals ─▶ SQLite ledger
CRM API ──crm_client.py──▶ accounts ──┘   (who is who)  (what to change)            │
                                                                                     ▼
                       CRM ◀── applier.py (re-check → write → verify) ◀── Approve ── app.py (review)
```

| Step | File | What it does |
|---|---|---|
| Scrape | `scraper.py` | Crawls the **whole site**, not just the paginated directory. The directory lists 34 communities, but the homepage says 35 and links the 35th (Findlay) only from a "new this year" notice. Found counts are checked against both stated totals; any parse or fetch failure fails the run. |
| Read CRM | `crm_client.py` | Paginated reads; fails if fewer rows come back than `total`. **Read-only by default**: writes raise unless the client is built with `allow_writes=True`, which only the Approve path does. GETs are retried; POSTs never are (a retry could create a duplicate). |
| Match | `matcher.py` | Address first, because names change on a sale and buildings don't move. Candidate tiers: street + ZIP (high), street + city (medium; catches a ZIP typo), exact name in the same city (medium; catches a placeholder PO Box). The same name in a different city is never a match. Every candidate is kept, so duplicates surface. |
| Decide | `rules.py` | Turns matches into proposals: `UPDATE`, `REPARENT`, `CHOW`, `DUPLICATE`, `CREATE`, `NEEDS_REVIEW`. Encodes the CHOW SOP, survivor selection and orphan handling (below). |
| Store | `proposal_store.py` | SQLite ledger of proposals, reviewer decisions and executed operations. |
| Review | `app.py` | Streamlit queue. Each card shows the website listing, before/after (✏️ = written, gray = display only), the CRM record including revenue and AR, evidence, confidence and flags. Approve, Reject (with reason), Retry, Reopen. |
| Apply | `applier.py` | Re-fetches and re-checks the accounts, writes, then reads back and verifies. |
| Schedule | `pipeline.py` | Daily job: scrape → read → match → propose → store. **Never approves and never writes to the CRM.** Aborts without proposing if the scrape is incomplete. |
| Check | `verify_crm.py` | Read-only end-state check of the CRM against the website. |

## Business rules

**Change of ownership (CHOW).** When an account must move to a different parent: if `lifetime_revenue > 0` **and** `outstanding_ar > 0`, the old account is left exactly as it is, a new account is created under the correct parent, and the old account's `chow_current_account` points to it. Otherwise the existing account is re-parented directly. Example: Tiffin (84k revenue, 12.4k AR) goes through CHOW; Lima (47k revenue, $0 AR) is re-parented directly. All four boundary cases are tested, and flipping AND to OR makes 4 tests fail.

**Duplicates.** One survivor per facility; each losing copy gets `duplicate_of_account = survivor` and status `Inactive`. The survivor is chosen rule by rule: billing history → already under the correct parent → name similarity to the website → address written as on the website → Active. The card names the rule that decided, and the rules that tied, and marks the deciding rows ⚖️. If only soft rules decided, the proposal is **low confidence**. If more than one copy has billing history, nothing is chosen automatically and all copies go to Needs Review.

**Not on the website.** An account under the parent that the website no longer lists is set to `Needs Review` with a note. Its parent is not changed and it is not inactivated: absence alone is not proof of a sale or closure.

**Match confidence is not change safety.** An exact match still goes through the CHOW check and human approval.

## Safety and re-run behaviour

- **Nothing writes without approval.** Only the Approve button builds a write-enabled client; tests check that viewing makes zero CRM calls.
- **Decisions are remembered (proposal fingerprints).** A proposal's identity is a hash of what it would change (kind, subject, operations and target values; the descriptive note is excluded). Tomorrow's run gets the same fingerprint for the same situation, so approved or rejected proposals never come back. A genuinely different change gets a new one. A pending proposal that stops being generated becomes `superseded`. Rejected proposals can be reopened.
- **Creates are never duplicated (operation idempotency keys).** The API has no `Idempotency-Key` support, so each operation gets a deterministic key, stamped into the new account's note. Before any POST the applier checks the ledger, then searches the CRM for the key. Intent is logged before the request and the new id right after. A lost response or a crash between "create" and "link old account" resumes without a second create.
- **Re-check before writing.** If the parent, revenue, AR, status or links changed since review, the proposal becomes `stale` and nothing is written (e.g. AR appearing could turn a re-parent into a CHOW).
- **Verify after writing.** Every target value is read back, and fields that should not change are checked unchanged. This is how the CHOW old account is proven untouched apart from `chow_current_account`.
- **Data-level idempotency.** Even without the ledger, applied changes are not re-proposed, because the CRM now matches the website, and accounts already marked duplicate or CHOW-superseded are excluded from matching.

## Results on the sandbox

- Website: **35** locations from 41 pages (directory says 34, homepage says 35).
- CRM: **121** accounts at the start, under 6 parent companies (Bellhaven had 29 child accounts).
- First run: **29 proposals** (9 update, 4 re-parent, 2 CHOW, 7 duplicate, 4 create, 3 not on website) and **12** locations already correct.
- Second run with no changes: **0 new** proposals.
- Review: **28 approved** (8 update, 4 re-parent, 2 CHOW, 7 duplicate, 4 create, 3 needs-review) and **1 rejected** (a website street address that would have overwritten a PO Box billing address). Every approved write was read back and verified. The CRM went from 121 to 127 accounts (4 new facilities, 2 CHOW successor accounts).
- Final run: **0 new** proposals. 34 of 35 locations already match; the remaining one is the rejected proposal, which is remembered and not re-proposed.
- `verify_crm.py` on the final CRM: **all 5 checks pass**. Each of the 35 locations has exactly one live account under Bellhaven; both CHOW links point to live successors, and the old accounts kept their parent; all 7 duplicates are Inactive and point to a live account; no unflagged accounts are left under Bellhaven; nothing remains to propose except the rejected one.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set CRM_API_TOKEN; .env is gitignored
```

## Run

```bash
python pipeline.py         # scrape, match, store proposals (read-only against the CRM)
```
```bash
streamlit run app.py       # review queue at http://localhost:8501
```
```bash
python verify_crm.py       # read-only end-state check
```
```bash
python -m pytest -q        # tests: fake CRM and fake website, no network
```

## Schedule

- **`deploy/crontab` (primary):** daily at 06:00 on the same machine as the review app, so the scheduler and the reviewer share one decision ledger.
- **`.github/workflows/daily.yml` (alternative):** same job on GitHub Actions, using the `CRM_API_TOKEN` repository secret (it skips if the secret is not set). Runners are ephemeral, so the ledger is carried between runs with `actions/cache`. Decisions made in a local review app don't reach that cache; in production both would use shared storage such as Postgres. `.github/workflows/tests.yml` runs the tests on every push.

## Limitations and next steps

- Address normalization is rule-based; a geocoding or USPS address-standardization step would catch more variants.
- Ownership is inferred from the operator's own website only; corroborating sources (state licensing data, CMS ownership records) would strengthen CHOW and orphan decisions.
- The reviewer can't pick a different survivor in the app; today that means rejecting and editing by hand.
- Single reviewer, local SQLite. Multi-user review needs shared storage, reviewer identity on decisions, and alerting on failed or stale proposals.
