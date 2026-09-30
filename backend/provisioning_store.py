"""
Provisioning / OMS Inventory Store
----------------------------------
Backs the remediation engine's value-check: given a Location ID, is there already
an active service there (and is it in good standing / not pending disconnect)?

Reads data/provisioning_data.json fresh on every call so the inventory can be
edited live without restarting the app.
"""

import os
import json

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "provisioning_data.json")


def _load() -> dict:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_active_service(location_id: str) -> dict | None:
    """Return the active service record for a Location ID, or None if none exists."""
    if not location_id:
        return None
    return _load().get("active_services", {}).get(location_id.strip())


def is_duplicate(location_id: str) -> bool:
    """True when the location already has an active service in good standing and
    is NOT pending disconnect — i.e. a new order there is a genuine duplicate."""
    svc = get_active_service(location_id)
    if not svc:
        return False
    if str(svc.get("status", "")).lower() != "active":
        return False
    if svc.get("pending_disconnect"):
        return False
    return True


def get_order(order_id: str) -> dict | None:
    if not order_id:
        return None
    for o in _load().get("orders", []):
        if o.get("order_id") == order_id.strip():
            return o
    return None


def find_location_by_tn(tn: str) -> str | None:
    """Reverse lookup: which active-service Location ID owns this telephone number."""
    if not tn:
        return None
    tn = tn.strip()
    for loc, svc in _load().get("active_services", {}).items():
        if svc.get("telephone_number") == tn:
            return loc
    return None


def all_active_services() -> dict:
    return _load().get("active_services", {})
