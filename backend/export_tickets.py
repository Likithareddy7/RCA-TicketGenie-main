"""
Export fallout tickets  (run against the SOURCE instance — yours)
-----------------------------------------------------------------
Reads every fallout `incident` from the ServiceNow instance in backend/.env and
writes them to data/servicenow_seed.json in a portable shape. Read-only: it never
modifies your instance.

Usage (from backend/, with the source instance creds in backend/.env):
    ..\\rca\\Scripts\\python.exe export_tickets.py

Then commit data/servicenow_seed.json and push so your colleague can import it
with seed_servicenow.py against her own instance.
"""

import os
import json

from servicenow_client import (
    _table_get, _canonical_block, _dv,
    INSTANCE, FALLOUT_QUERY, INCIDENT_FIELDS,
)

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "servicenow_seed.json")

# OOB incident state display value -> numeric value (stable across instances).
STATE_TO_VALUE = {
    "new": "1", "in progress": "2", "on hold": "3",
    "resolved": "6", "closed": "7", "cancelled": "8", "canceled": "8",
}


def main():
    if not INSTANCE:
        raise SystemExit("SERVICENOW_INSTANCE not set in backend/.env")

    rows = _table_get("incident", {
        "sysparm_query": FALLOUT_QUERY,
        "sysparm_fields": INCIDENT_FIELDS,
        "sysparm_display_value": "all",
        "sysparm_limit": 200,
    })

    tickets = []
    for r in rows:
        block = _canonical_block(_dv(r.get("comments")))
        state_display = _dv(r.get("state")).strip()
        tickets.append({
            "number": _dv(r.get("number")),               # informational only
            "short_description": _dv(r.get("short_description")),
            "state_display": state_display,
            "state_value": STATE_TO_VALUE.get(state_display.lower(), ""),
            "is_closed": state_display.lower() in ("closed", "resolved"),
            "block": block,
        })

    data = {"exported_from": INSTANCE, "count": len(tickets), "tickets": tickets}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(tickets)} fallout tickets -> {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
