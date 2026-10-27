# EntitySync — CRM Entity Resolution & Reconciliation Engine

*Automated CRM entity resolution with human-in-the-loop reconciliation.*

> Developed as part of a technical take-home assessment for Clipboard Health, using a fictional senior living operator and a sandbox CRM environment.

EntitySync keeps facility-to-parent-company links in a CRM accurate. It scrapes an operator's public community directory, matches each location to CRM accounts, and turns differences into **proposals**: renames, re-parents, change-of-ownership (CHOW) splits, duplicate cleanup, and missing accounts. A reviewer approves or rejects each one. Only approved proposals are written back to the CRM, and every write is verified.

## Status
🚧 Work in progress. See [docs/GUIDE.md](docs/GUIDE.md) for the design and build plan.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set CRM_API_TOKEN
```
