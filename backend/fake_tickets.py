"""
Fake / Demo Tickets
-------------------
Inject synthetic OPEN fallout tickets into the queue WITHOUT touching the
ServiceNow instance, so the queue can be demoed with no live instance at all.

Two ticket shapes are accepted in data/fake_tickets.json:

  * RAW (preferred) — `{number, short_description, state_display|state, block}`,
    exactly the export shape ServiceNow produces. The labelled `block` is parsed
    with servicenow_client.parse_ticket(), the SAME parser used for live tickets,
    so a demo ticket and a real one cannot drift apart.
  * PARSED — an already-parsed dict (has a "fields" key). Passed through as-is.

Purely additive: real ServiceNow tickets are unaffected. Approving a fake ticket
returns a simulated result and never posts a comment to ServiceNow.

Turn off by setting "enabled": false (or deleting) data/fake_tickets.json.
"""

import os
import json

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "fake_tickets.json")


def _parse_raw(t: dict) -> dict:
    """Run a raw {short_description, block} demo ticket through the real parser.

    Imported lazily: servicenow_client imports THIS module at load time, so a
    top-level import here would be circular.
    """
    import servicenow_client

    state = t.get("state_display") or t.get("state") or "New"
    parsed = servicenow_client.parse_ticket({
        "number": t.get("number", ""),
        "sys_id": t.get("sys_id", ""),
        "state": state,
        "short_description": t.get("short_description", ""),
        "comments": t.get("block", ""),
        "sys_updated_on": t.get("updated", ""),
        # Lets a demo ticket carry an age, the same way a live one does.
        "sys_created_on": t.get("opened", ""),
    })
    # An explicit is_closed in the file wins over the state-string heuristic.
    if "is_closed" in t:
        parsed["is_closed"] = bool(t["is_closed"])
    parsed["source"] = "demo"
    # A demo ticket may carry its own ServiceNow deep link. parse_ticket builds one
    # from sys_id, which demo tickets do not have, so an explicit url wins.
    if t.get("url"):
        parsed["url"] = t["url"]
    return parsed


def _load() -> list:
    """Read fresh on every call so the queue reflects live edits to the JSON."""
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not data.get("enabled", True):
        return []
    out = []
    for t in data.get("tickets", []):
        try:
            out.append(t if "fields" in t else _parse_raw(t))
        except Exception as e:
            print(f"[DEMO] skipping ticket {t.get('number', '?')}: {e}")
    return out


def all_fake() -> list:
    return _load()


def get(number: str) -> dict | None:
    for t in _load():
        if t.get("number") == number:
            return t
    return None


def is_fake(number: str) -> bool:
    return get(number) is not None
