"""
BOSS / OMS Inventory Store
--------------------------
Backs the value-checks for the BOSS OM fallout types, keyed on BAN and order id —
the identifiers the incidents export actually uses.

(provisioning_store.py remains the LOC-/SVC-keyed store for the duplicate-service
fallout type. The two cover different fallout families and different id schemes.)

Reads data/oms_data.json fresh on every call so the inventory can be edited live
without restarting the app.
"""

import os
import json

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "oms_data.json")


def _load() -> dict:
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[OMS] could not read {DATA_PATH}: {e}")
        return {}


def _norm_ban(ban: str) -> str:
    """BANs arrive with stray punctuation from free text ('BAN: 314116701.')."""
    return "".join(ch for ch in str(ban or "") if ch.isdigit())


def get_account(ban: str) -> dict | None:
    if not ban:
        return None
    return _load().get("accounts", {}).get(_norm_ban(ban))


def get_order(order_id: str) -> dict | None:
    if not order_id:
        return None
    return _load().get("orders", {}).get(str(order_id).strip().upper())


def orders_for_ban(ban: str) -> list:
    key = _norm_ban(ban)
    return [o for o in _load().get("orders", {}).values() if _norm_ban(o.get("ban")) == key]


def all_accounts() -> dict:
    return _load().get("accounts", {})


def all_orders() -> dict:
    return _load().get("orders", {})
