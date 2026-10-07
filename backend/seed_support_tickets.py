"""
Seed the Business Hub support knowledge base into ServiceNow
-------------------------------------------------------------
Creates the "BUS Sales Ordering and Digital Support" group and the distinct
support problems from data/support_kb.json as closed incidents inside it.

These live in their OWN group, separate from TICKETGENIE DEMO, so the two demo
domains stay distinguishable in the ServiceNow UI while both remain searchable.

Three things here are instance-specific and were learned the hard way:

* **The resolution codes are not valid choices on a stock instance.** This export
  uses "Resolved - User Education" and two others; a stock incident table has
  "Solution provided", "User error" and so on. ServiceNow rejects a close with an
  unlisted code, reporting it as a MISSING mandatory field rather than an invalid
  one, which is thoroughly misleading. Rather than mapping your codes onto generic
  ones and losing them, this adds them to sys_choice. That is additive and can be
  undone by deleting those three rows.

* **A journal write to a CLOSED incident is silently dropped**, so the labelled
  block is posted BEFORE closing, never after.

* **ServiceNow drops an assignment_group it cannot resolve**, so the group is
  created first and the write is read back.

Safe to re-run: an incident whose short_description already exists in the group is
skipped.

Usage (from backend/):
    ../rca/bin/python seed_support_tickets.py --dry-run
    ../rca/bin/python seed_support_tickets.py --limit 1     # prove one end to end
    ../rca/bin/python seed_support_tickets.py
"""

import os
import sys
import json

from dotenv import load_dotenv
load_dotenv()

import servicenow_client as sn

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "support_kb.json")
GROUP = os.getenv("SERVICENOW_SUPPORT_GROUP", "BUS Sales Ordering and Digital Support").strip()
CLOSED_STATE = "7"

# Taken from the export's own Resolution code column.
DATASET_CODES = [
    "Resolved - User Education",
    "Resolved – Permanent Fix Applied",
    "Resolved - No response from User",
]


def resolution_code_of(entry: dict) -> str:
    """The real code, recovered from the {RCA TAG : ...} marker in the close notes."""
    notes = entry.get("close_notes", "")
    if "{RCA TAG :" in notes:
        return notes.split("{RCA TAG :", 1)[1].rsplit("}", 1)[0].strip()
    return "Solution provided"


def close_notes_of(entry: dict) -> str:
    return entry.get("close_notes", "").split("{RCA TAG :", 1)[0].strip()


def ensure_close_codes(dry: bool) -> None:
    """Add this export's resolution codes to the incident close_code choice list."""
    existing = {c["value"] for c in sn._table_get("sys_choice", {
        "sysparm_query": "name=incident^element=close_code",
        "sysparm_fields": "value", "sysparm_limit": 100})}
    for code in DATASET_CODES:
        if code in existing:
            print(f"  close code already present: {code}")
            continue
        if dry:
            print(f"  would add close code: {code}")
            continue
        r = sn._request("POST", f"{sn.INSTANCE}/api/now/table/sys_choice",
                        headers={"Content-Type": "application/json"},
                        json={"name": "incident", "element": "close_code",
                              "value": code, "label": code, "inactive": "false"},
                        timeout=30)
        r.raise_for_status()
        print(f"  added close code: {code}")


def build_block(e: dict) -> str:
    """The labelled block this app parses its structured fields out of."""
    return "\n".join([
        f"Subcategory: {e.get('u_issue_type', '')}",
        f"Category: {e.get('category', '')}",
        f"Resolution Code: {resolution_code_of(e)}",
        "",
        "Description:",
        e.get("description", ""),
        "",
        "Resolution Notes:",
        close_notes_of(e),
    ])


def existing_by_summary(group: str) -> dict:
    try:
        rows = sn._table_get("incident", {
            "sysparm_query": f"assignment_group.name={group}",
            "sysparm_fields": "number,sys_id,state,short_description,close_notes",
            "sysparm_limit": 1000})
    except Exception as e:
        print(f"[WARN] could not list existing incidents ({e}); duplicates possible.")
        return {}
    # Keyed on the summary AND the start of the close notes, because a dozen of
    # these problems share a short description and differ only in their resolution.
    # Keying on the summary alone silently drops the second variant of each.
    out = {}
    for r in rows:
        out[((r.get("short_description") or "").strip(),
             (r.get("close_notes") or "").strip()[:60])] = r
    return out


def main():
    dry = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    with open(DATA, "r", encoding="utf-8") as f:
        entries = json.load(f)
    if limit:
        entries = entries[:limit]

    print(f"instance: {sn.INSTANCE}")
    print(f"group:    {GROUP}")
    print(f"entries:  {len(entries)}")
    print(f"mode:     {'DRY RUN, nothing written' if dry else 'LIVE'}")
    print()

    print("close codes:")
    ensure_close_codes(dry)
    print()

    if dry:
        e = entries[0]
        print(f"Would ensure group {GROUP!r} exists, then create {len(entries)} closed incidents.")
        print(f"\nExample, {e['number']}:")
        print(f"  short_description: {e['short_description']}")
        print(f"  category:          {e['category']}")
        print(f"  subcategory:       {e['u_issue_type']}")
        print(f"  assignment_group:  {GROUP}")
        print(f"  contact_type:      self-service")
        print(f"  then state -> {CLOSED_STATE} (Closed)")
        print(f"       close_code  {resolution_code_of(e)!r}")
        print(f"       close_notes {close_notes_of(e)[:70]!r}")
        print("  comments block:")
        for line in build_block(e).splitlines():
            print(f"    {line}")
        return 0

    grp = sn.create_group(GROUP, "Business Hub support demo tickets")
    print(f"group {'created' if grp['created'] else 'already existed'}: "
          f"{grp['name']} ({grp['sys_id']})")

    seen = existing_by_summary(GROUP)
    created = skipped = failed = 0
    for e in entries:
        summary = e["short_description"].strip()
        if (summary, close_notes_of(e)[:60]) in seen:
            print(f"  skip   {summary[:52]}")
            skipped += 1
            continue
        try:
            inc = sn.create_incident(
                short_description=summary,
                description=e.get("description", ""),
                assignment_group=GROUP,
                extra={"category": e.get("category", ""),
                       "subcategory": e.get("u_issue_type", ""),
                       "contact_type": "self-service"})
            number, sys_id = inc["number"], inc["sys_id"]
            # Block first: a journal write to a closed incident is discarded.
            sn.post_comment(number, build_block(e))
            r = sn._request("PATCH", f"{sn.INSTANCE}/api/now/table/incident/{sys_id}",
                            headers={"Content-Type": "application/json"},
                            json={"state": CLOSED_STATE,
                                  "close_code": resolution_code_of(e),
                                  "close_notes": close_notes_of(e)}, timeout=30)
            if r.status_code >= 400:
                raise RuntimeError(f"close failed HTTP {r.status_code}: {r.text[:180]}")
            print(f"  create {number}  {summary[:50]}")
            if inc.get("warning"):
                print(f"         WARNING: {inc['warning']}")
            created += 1
        except Exception as ex:
            print(f"  FAIL   {summary[:46]}: {type(ex).__name__} {str(ex)[:140]}")
            failed += 1

    print()
    print(f"created {created}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
