"""
Seed fallout tickets  (run against the TARGET instance — your colleague's)
--------------------------------------------------------------------------
Reads data/servicenow_seed.json and CREATES the same fallout incidents in the
ServiceNow instance configured in backend/.env. Each ticket becomes an incident
whose `short_description` marks it as fallout and whose labelled block is posted
into the `comments` journal — exactly the shape the app reads back.

Safe to re-run: a ticket whose short_description already exists as a fallout
incident is skipped, so it will not create duplicates.

Usage (from backend/, with the TARGET instance creds in backend/.env):
    ..\\rca\\Scripts\\python.exe seed_servicenow.py
"""

import os
import json
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

import truststore
truststore.inject_into_ssl()

load_dotenv()

INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
USER = os.getenv("SERVICENOW_USER", "")
PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")
AUTH = HTTPBasicAuth(USER, PASSWORD)

SEED = os.path.join(os.path.dirname(__file__), "..", "data", "servicenow_seed.json")
TABLE = "incident"


def _existing_fallout_short_descriptions() -> set:
    url = f"{INSTANCE}/api/now/table/{TABLE}"
    resp = requests.get(
        url, auth=AUTH, headers={"Accept": "application/json"},
        params={
            "sysparm_query": "short_descriptionLIKEfallout",
            "sysparm_fields": "short_description",
            "sysparm_limit": 1000,
        }, timeout=30,
    )
    resp.raise_for_status()
    return {row.get("short_description", "").strip() for row in resp.json().get("result", [])}


def _create(t: dict) -> str:
    url = f"{INSTANCE}/api/now/table/{TABLE}"
    # 1) create the incident with the labelled block in the comments journal
    resp = requests.post(
        url, auth=AUTH,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"short_description": t["short_description"], "comments": t["block"]},
        timeout=30,
    )
    resp.raise_for_status()
    rec = resp.json()["result"]
    sys_id, number = rec["sys_id"], rec["number"]

    # 2) if it was closed/resolved in the source, transition it here too
    if t.get("is_closed"):
        state = t.get("state_value") or ("7" if t.get("state_display", "").lower() == "closed" else "6")
        requests.patch(
            f"{url}/{sys_id}", auth=AUTH,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "state": state,
                "close_code": "Solved (Permanently)",
                "close_notes": "Seeded from source instance.",
            }, timeout=30,
        ).raise_for_status()
    return number


def main():
    if not INSTANCE:
        raise SystemExit("SERVICENOW_INSTANCE not set in backend/.env")
    with open(SEED, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickets = data.get("tickets", [])
    print(f"Seeding {len(tickets)} tickets into {INSTANCE}")

    have = _existing_fallout_short_descriptions()
    created = skipped = failed = 0
    for t in tickets:
        sd = t["short_description"].strip()
        if sd in have:
            print(f"  skip (already exists): {sd}")
            skipped += 1
            continue
        try:
            number = _create(t)
            have.add(sd)
            state = "closed" if t.get("is_closed") else "open"
            print(f"  created {number} [{state}]: {sd}")
            created += 1
        except requests.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            print(f"  ERROR: {sd}\n         {e}\n         {body}")
            failed += 1

    print(f"\nDone. created={created} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
