"""
Seed the OPEN fallout queue into ServiceNow
-------------------------------------------
Creates the demo open tickets (data/fake_tickets.json) as real incidents in the
instance configured in backend/.env, so ServiceNow — not the local JSON — becomes
the source of the queue.

Each incident is created with:
  * the real short_description (no "Fallout - " prefix; the app finds tickets by
    assignment group, not by wording — see servicenow_client.FALLOUT_QUERY),
  * the labelled block posted into `comments`, which is where this app reads every
    structured field from (native incident fields are not used), and
  * assignment_group set to SERVICENOW_QUEUE_GROUP, which is what puts the ticket
    in this app's queue in the first place.

`state` is left at the ServiceNow default (New). This script never closes anything.

Safe to re-run: an incident whose short_description already exists is skipped, so
it will not create duplicates.

Usage (from backend/):
    ../rca/bin/python seed_open_queue.py            # create the tickets
    ../rca/bin/python seed_open_queue.py --dry-run  # show what would be created
    ../rca/bin/python seed_open_queue.py --from ../data/fake_tickets.json
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

# Reuse the app's own client so seeding authenticates exactly the way the running
# app does (OAuth when configured, Basic otherwise) — one auth path, not two.
import servicenow_client as snc

load_dotenv()

INSTANCE = snc.INSTANCE
HEADERS = {"Content-Type": "application/json"}

QUEUE_GROUP = os.getenv("SERVICENOW_QUEUE_GROUP", "BOSS OM IT Support").strip()
# Which file supplies the open tickets. Defaults to the synthetic demo set; pass
# --from <path> to seed a different one (e.g. data/fake_tickets.json).
_DEFAULT_TICKETS = os.path.join(os.path.dirname(__file__), "..", "data",
                                "synthetic_open_queue.json")


def _tickets_path() -> str:
    if "--from" in sys.argv:
        return sys.argv[sys.argv.index("--from") + 1]
    return _DEFAULT_TICKETS


TICKETS = _tickets_path()
ROUTING = os.path.join(os.path.dirname(__file__), "..", "data", "routing_rules.json")


def _groups_needed() -> list:
    """The queue's own group, plus every group the redirect rules can hand off to.

    A destination group that does not exist is the classic silent failure here:
    ServiceNow DROPS an unresolvable assignment_group instead of rejecting it, so a
    reassignment would look successful while the ticket never moved.
    """
    names = [QUEUE_GROUP] if QUEUE_GROUP else []
    try:
        with open(ROUTING, "r", encoding="utf-8") as f:
            for rule in json.load(f).get("rules", []):
                g = (rule.get("assignment_group") or "").strip()
                if g and g not in names:
                    names.append(g)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  ! could not read routing rules ({e}) — only ensuring {QUEUE_GROUP!r}")
    return names


def _find_group(name: str) -> str:
    r = snc._request("GET", f"{INSTANCE}/api/now/table/sys_user_group",
                     params={"sysparm_query": f"name={name}",
                             "sysparm_fields": "sys_id", "sysparm_limit": 1}, timeout=30)
    r.raise_for_status()
    rows = r.json().get("result", [])
    return rows[0]["sys_id"] if rows else ""


def ensure_groups(dry_run: bool = False) -> dict:
    out = {}
    for name in _groups_needed():
        sys_id = _find_group(name)
        if sys_id:
            print(f"  group {name!r}: exists")
        elif dry_run:
            print(f"  group {name!r}: WOULD CREATE")
        else:
            r = snc._request("POST", f"{INSTANCE}/api/now/table/sys_user_group",
                             headers=HEADERS, json={"name": name}, timeout=30)
            r.raise_for_status()
            sys_id = r.json()["result"]["sys_id"]
            print(f"  group {name!r}: created")
        out[name] = sys_id
    return out


def _existing_short_descriptions() -> set:
    """Every incident summary already in the instance, so re-runs are idempotent."""
    r = snc._request("GET", f"{INSTANCE}/api/now/table/incident",
                     params={"sysparm_fields": "short_description",
                             "sysparm_limit": 2000}, timeout=60)
    r.raise_for_status()
    return {row.get("short_description", "").strip() for row in r.json().get("result", [])}


def open_tickets() -> list:
    with open(TICKETS, "r", encoding="utf-8") as f:
        return [t for t in json.load(f).get("tickets", []) if not t.get("is_closed")]


def main():
    dry_run = "--dry-run" in sys.argv
    if not INSTANCE:
        raise SystemExit("SERVICENOW_INSTANCE not set in backend/.env")

    tickets = open_tickets()
    print(f"Seeding {len(tickets)} open tickets into {INSTANCE}")
    print(f"Queue group: {QUEUE_GROUP!r}\n")

    print("Groups:")
    ensure_groups(dry_run)

    have = _existing_short_descriptions()
    print(f"\nIncidents ({len(have)} already in the instance):")

    created = skipped = failed = 0
    for t in tickets:
        sd = t["short_description"].strip()
        if sd in have:
            print(f"  skip   {t['number']}  (summary already present)")
            skipped += 1
            continue
        if dry_run:
            print(f"  WOULD CREATE  {t['number']}  {sd[:56]}")
            created += 1
            continue
        payload = {
            "short_description": sd,
            "comments": t.get("block", ""),
            "assignment_group": QUEUE_GROUP,
        }
        try:
            r = snc._request("POST", f"{INSTANCE}/api/now/table/incident",
                             headers=HEADERS, json=payload, timeout=30)
            r.raise_for_status()
            rec = r.json()["result"]
            # ServiceNow drops an assignment_group it cannot resolve, so confirm it landed.
            grp = rec.get("assignment_group")
            landed = bool(grp.get("value") if isinstance(grp, dict) else grp)
            flag = "" if landed else "   ! assignment_group did NOT stick"
            print(f"  create {rec['number']}  (was {t['number']}){flag}")
            have.add(sd)
            created += 1
        except requests.HTTPError as e:
            body = e.response.text[:200] if e.response is not None else ""
            print(f"  ERROR  {t['number']}: {e}\n         {body}")
            failed += 1

    verb = "would create" if dry_run else "created"
    print(f"\nDone. {verb}={created} skipped={skipped} failed={failed}")
    if created and not dry_run:
        print("\nNext: set \"enabled\": false in data/fake_tickets.json so the queue "
              "comes from ServiceNow only, then restart the backend.")


if __name__ == "__main__":
    main()
