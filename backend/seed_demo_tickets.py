"""
Seed the demo support tickets into ServiceNow
---------------------------------------------
Creates the TICKETGENIE DEMO group and the resolved customer-support tickets from
data/demo_support_tickets.json as real incidents inside it, so the demo knowledge
base is visible in the ServiceNow UI and can be read back live.

Two things about this script are deliberate:

  * It writes a LABELLED BLOCK into the incident's `comments` journal, not just the
    native fields. That is where this app reads every structured field from
    (servicenow_client._parse_block), so seeding this way means a seeded ticket
    parses through exactly the same path as any other ticket, with no special case.

  * It CLOSES the incidents it creates, which the application itself never does.
    That rule constrains the app, whose job is to recommend; a seeding script has to
    produce closed tickets because the knowledge base is made of resolved ones.
    The closing happens here and nowhere in the request path.

Everything lands in its own group, so nothing mixes with an existing queue.

Safe to re-run: an incident whose short_description already exists in the group is
skipped rather than duplicated.

Usage (from backend/):
    ../rca/bin/python seed_demo_tickets.py --dry-run   # print what would happen
    ../rca/bin/python seed_demo_tickets.py             # create the group and tickets
"""

import os
import sys
import json

from dotenv import load_dotenv
load_dotenv()

import servicenow_client as sn

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "demo_support_tickets.json")
CLOSED_STATE = "7"          # ServiceNow incident state 7 = Closed

# The close code MUST be a value in the instance's own choice list for
# incident.close_code, because a data policy makes it mandatory when closing and
# ServiceNow rejects a value that is not a valid choice with a 403 "Data Policy
# Exception: the following fields are mandatory: Resolution code", which reads as
# though the field was omitted rather than invalid. Older instances had
# "Solved (Permanently)"; current releases do not. Verify with:
#   sys_choice?sysparm_query=name=incident^element=close_code^inactive=false
CLOSE_CODE = "Solution provided"


def _pretty(task_name: str) -> str:
    return " ".join(w.capitalize() for w in str(task_name or "").split("_"))


def build_block(t: dict) -> str:
    """The labelled block this app parses its structured fields out of.

    Field order mirrors the BOSS OM tickets: single-line 'Label: value' pairs first,
    then the free-text sections, because that is what _parse_block expects.
    """
    task = _pretty(t.get("task_name", ""))
    return "\n".join([
        f"Subcategory: {task}",
        f"Category: {t.get('error_code', '')}",
        f"Service Type: {t.get('order_type', '')}",
        f"Resolution Code: {t.get('error_code', '')} - {task}",
        f"Resolved Team: {t.get('resolved_team', '')}",
        "",
        "Description:",
        t.get("root_cause", ""),
        "",
        "Resolution Notes:",
        t.get("resolution", ""),
    ])


def existing_by_summary(group: str) -> dict:
    """Map short_description -> {number, sys_id, state} for the group.

    Keyed on the summary rather than on a counter so a re-run recognises what is
    already there, whatever numbers ServiceNow assigned.
    """
    try:
        rows = sn._table_get("incident", {
            "sysparm_query": f"assignment_group.name={group}",
            "sysparm_fields": "number,sys_id,state,short_description",
            "sysparm_limit": 500,
        })
    except Exception as e:
        print(f"[WARN] could not list existing incidents ({e}); duplicates are possible.")
        return {}
    return {(r.get("short_description") or "").strip():
            {"number": r.get("number"), "sys_id": r.get("sys_id"), "state": r.get("state")}
            for r in rows}


def has_block(sys_id: str) -> bool:
    """Is the labelled block already in this incident's journal?

    Checked rather than assumed, because a partially failed run can leave an
    incident created but without the block, and an incident with no block parses
    into a ticket with no subcategory and no resolution, which silently degrades
    the knowledge base.
    """
    rows = sn._table_get("incident", {
        "sysparm_query": f"sys_id={sys_id}", "sysparm_fields": "comments",
        "sysparm_display_value": "all", "sysparm_limit": 1})
    if not rows:
        return False
    return "Resolution Notes:" in sn._dv(rows[0].get("comments"))


def close_incident(sys_id: str, resolution: str):
    """Close a seeded ticket. Only a seeding script does this; the app never does."""
    r = sn._request("PATCH", f"{sn.INSTANCE}/api/now/table/incident/{sys_id}",
                    headers={"Content-Type": "application/json"},
                    json={"state": CLOSED_STATE, "close_code": CLOSE_CODE,
                          "close_notes": resolution}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r


def main():
    dry = "--dry-run" in sys.argv
    group = sn.DEMO_GROUP

    with open(DATA, "r", encoding="utf-8") as f:
        tickets = json.load(f).get("tickets", [])
    if not tickets:
        raise SystemExit(f"No tickets in {DATA}")

    print(f"instance: {sn.INSTANCE}")
    print(f"group:    {group}")
    print(f"tickets:  {len(tickets)}")
    print(f"mode:     {'DRY RUN, nothing will be written' if dry else 'LIVE, incidents will be created'}")
    print()

    if dry:
        t = tickets[0]
        print(f"Would ensure group {group!r} exists, then create {len(tickets)} incidents.")
        print(f"\nExample, {t['ticket_id']}:")
        print(f"  short_description: {t['error_message']}")
        print(f"  assignment_group:  {group}")
        print(f"  then state -> {CLOSED_STATE} (Closed), close_code {CLOSE_CODE!r}")
        print("  comments block:")
        for line in build_block(t).splitlines():
            print(f"    {line}")
        return 0

    grp = sn.create_group(group, "TicketGenie demo support tickets")
    print(f"group {'created' if grp['created'] else 'already existed'}: "
          f"{grp['name']} ({grp['sys_id']})")

    seen = existing_by_summary(group)
    created = closed = skipped = failed = patched = 0
    for t in tickets:
        summary = (t.get("error_message") or "").strip()
        hit = seen.get(summary)
        try:
            if hit:
                repaired = ""
                if not has_block(hit["sys_id"]):
                    # A journal write to a CLOSED incident is silently dropped by
                    # ServiceNow: the PATCH returns 200 and nothing is stored. So a
                    # closed ticket has to be reopened, commented, and closed again.
                    # Verified against this instance; sys_journal_field held zero rows
                    # after an apparently successful post.
                    was_closed = str(hit.get("state")) == CLOSED_STATE
                    if was_closed:
                        sn._request("PATCH", f"{sn.INSTANCE}/api/now/table/incident/{hit['sys_id']}",
                                    headers={"Content-Type": "application/json"},
                                    json={"state": "2"}, timeout=30).raise_for_status()
                    sn.post_comment(hit["number"], build_block(t))
                    if not has_block(hit["sys_id"]):
                        raise RuntimeError("journal write was dropped even after reopening")
                    if was_closed:
                        close_incident(hit["sys_id"], t.get("resolution", ""))
                    repaired = ", posted missing block"
                    patched += 1
                if str(hit.get("state")) == CLOSED_STATE:
                    print(f"  ok     {t['ticket_id']} -> {hit['number']} (already closed{repaired})")
                    skipped += 1
                    continue
                close_incident(hit["sys_id"], t.get("resolution", ""))
                print(f"  close  {t['ticket_id']} -> {hit['number']} (existed, now closed{repaired})")
                closed += 1
                continue

            inc = sn.create_incident(short_description=summary,
                                     description=t.get("root_cause", ""),
                                     assignment_group=group)
            number, sys_id = inc["number"], inc["sys_id"]
            # The labelled block is where this app reads the structured fields from.
            sn.post_comment(number, build_block(t))
            # Closing is this script's job, never the application's.
            close_incident(sys_id, t.get("resolution", ""))
            print(f"  create {t['ticket_id']} -> {number} (closed)")
            if inc.get("warning"):
                print(f"         WARNING: {inc['warning']}")
            created += 1
        except Exception as e:
            print(f"  FAIL   {t['ticket_id']}: {type(e).__name__} {str(e)[:200]}")
            failed += 1

    print()
    print(f"created {created}, closed-existing {closed}, blocks repaired {patched}, "
          f"already done {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
