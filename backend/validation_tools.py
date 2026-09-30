"""
Validation Tools
----------------
Each "check the current system state" operation is a function registered with the
@tool decorator. A tool has a name, a description (its docstring), a JSON input
schema, and a DETERMINISTIC body that pulls the relevant value from a system and
returns a validation verdict:

    { label, status, passed, summary, details: [{label, value}], data }
    status ∈ {"confirmed", "ambiguous", "not_confirmed"}

The verdict is deterministic (no LLM). An LLM ROUTER (fallout_engine) only decides
WHICH tool to run and WHICH identifiers to pass, guided by how similar tickets were
resolved — it never produces the verdict itself.

To support a new fallout type, add one more @tool below.
"""

import inspect
import provisioning_store
import oms_store

TOOLS = {}


class _Tool:
    def __init__(self, name, description, input_schema, fn):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn

    def run(self, identifiers: dict) -> dict:
        params = inspect.signature(self.fn).parameters
        kwargs = {k: v for k, v in (identifiers or {}).items() if k in params}
        return self.fn(**kwargs)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def tool(name, description=None, input_schema=None):
    """Register a validation tool. Pulls a system value -> deterministic verdict."""
    def deco(fn):
        TOOLS[name] = _Tool(
            name,
            (description or fn.__doc__ or "").strip(),
            input_schema or {"type": "object", "properties": {}, "required": []},
            fn,
        )
        return fn
    return deco


def _verdict(status, summary, details=None, data=None):
    return {
        "label": "Validation",
        "status": status,
        "passed": status == "confirmed",
        "summary": summary,
        "details": details or [],
        "data": data or {},
    }


# ── provisioning domain ──────────────────────────────────────────────────

@tool(
    "provisioning.check_active_service",
    input_schema={
        "type": "object",
        "properties": {
            "location_id": {"type": "string", "description": "The Location ID to check, e.g. 'LOC-482910'."}
        },
        "required": ["location_id"],
    },
)
def check_active_service(location_id: str = "") -> dict:
    """Check the provisioning/OMS inventory for an existing active service at a
    Location ID and decide whether a new order there is a duplicate. Use this when
    the resolution involves verifying an already-provisioned / active service at a
    location."""
    service = provisioning_store.get_active_service(location_id) or {}
    found = bool(service)
    active = str(service.get("status", "")).lower() == "active"
    pending = bool(service.get("pending_disconnect"))
    if found and active and not pending:
        details = [
            {"label": "Active service", "value": service.get("service_id", "")},
            {"label": "Plan", "value": service.get("service_type", "")},
            {"label": "On telephone", "value": service.get("telephone_number", "")},
            {"label": "Standing", "value": service.get("standing", "")},
        ]
        return _verdict("confirmed", f"Active service confirmed at {location_id}.", details, service)
    if found and pending:
        return _verdict("ambiguous", f"Existing service at {location_id} is pending disconnect — may not be a duplicate.",
                        [{"label": "Active service", "value": service.get("service_id", "")},
                         {"label": "Status", "value": "Pending disconnect"}], service)
    return _verdict("not_confirmed", f"No active service found at {location_id} in the provisioning inventory.")


# ── BOSS / OMS domain ────────────────────────────────────────────────────
# These answer "which value is wrong?" by comparing two values that are supposed
# to agree. Each returns the CURRENT value, the EXPECTED value, and which system
# holds the wrong one, so the recommendation can name the correction precisely.

@tool(
    "oms.check_account_status",
    input_schema={
        "type": "object",
        "properties": {
            "ban": {"type": "string", "description": "Billing account number, e.g. '1000342627'."}
        },
        "required": ["ban"],
    },
)
def check_account_status(ban: str = "") -> dict:
    """Check whether a BAN's account status agrees with the state of its orders.
    Use this when the ticket says the account shows the wrong status — still
    pending after an order was cancelled, inactive with no disconnect order, or
    not going active after an order completed."""
    acct = oms_store.get_account(ban)
    if not acct:
        return _verdict("not_confirmed", f"No account found for BAN {ban} in OMS.")

    status = str(acct.get("account_status", "")).strip()
    orders = oms_store.orders_for_ban(ban)
    statuses = {str(o.get("order_status", "")).lower() for o in orders}
    base = [{"label": "BAN", "value": acct.get("ban", "")},
            {"label": "Account status (current)", "value": status}]

    expected, why = None, ""
    if orders and statuses <= {"cancelled", "canceled"}:
        expected, why = "Inactive", "every order on the account is cancelled"
    elif orders and "completed" in statuses and status.lower() == "pending":
        expected, why = "Active", "an order on the account has completed"
    elif not orders and status.lower() == "inactive":
        expected, why = "Active", "there is no disconnect order on file"

    if expected and expected.lower() != status.lower():
        details = base + [
            {"label": "Account status (expected)", "value": expected},
            {"label": "Basis", "value": why},
            {"label": "Orders on account", "value": ", ".join(
                f"{o['order_id']}={o.get('order_status', '')}" for o in orders) or "none"},
        ]
        return _verdict("confirmed",
                        f"Account status for BAN {ban} is wrong: shows '{status}', should be "
                        f"'{expected}' because {why}.",
                        details, {"ban": ban, "current": status, "expected": expected,
                                  "field": "account_status"})
    if expected:
        return _verdict("not_confirmed",
                        f"Account status for BAN {ban} is already '{status}' — nothing to correct.",
                        base)
    return _verdict("ambiguous",
                    f"Account status for BAN {ban} is '{status}', but the orders on file do not "
                    f"determine what it should be. Manual review required.",
                    base + [{"label": "Orders on account", "value": ", ".join(
                        f"{o['order_id']}={o.get('order_status', '')}" for o in orders) or "none"}])


@tool(
    "oms.check_order_status",
    input_schema={
        "type": "object",
        "properties": {
            "order_ref": {"type": "string", "description": "Order id, e.g. 'TN1100045344'."}
        },
        "required": ["order_ref"],
    },
)
def check_order_status(order_ref: str = "") -> dict:
    """Check whether an order is stuck — staged but not reflected on the account,
    or held by an open blocking task. Use this when the ticket says an order is
    not showing on the account, is stuck in progress, or needs a task completed."""
    order = oms_store.get_order(order_ref)
    if not order:
        return _verdict("not_confirmed", f"No order {order_ref} found in OMS.")

    status = str(order.get("order_status", "")).strip()
    stage = str(order.get("stage", "")).strip()
    reflected = bool(order.get("reflected_on_account"))
    blocking = [t for t in (order.get("open_tasks") or []) if t.get("blocking")]
    base = [{"label": "Order", "value": order.get("order_id", "")},
            {"label": "BAN", "value": order.get("ban", "")},
            {"label": "Order status", "value": status},
            {"label": "Stage", "value": stage}]

    if blocking:
        t = blocking[0]
        return _verdict("confirmed",
                        f"Order {order_ref} is held at '{stage}' by open task "
                        f"{t.get('task_id', '')} ({t.get('description', '')}).",
                        base + [{"label": "Blocking task", "value": t.get("task_id", "")},
                                {"label": "Task description", "value": t.get("description", "")}],
                        {"order_ref": order_ref, "blocking_task": t.get("task_id", ""),
                         "field": "open_task"})
    if not reflected:
        return _verdict("confirmed",
                        f"Order {order_ref} exists in OMS at stage '{stage}' but is NOT reflected "
                        f"on the account — it did not progress past staging.",
                        base + [{"label": "Reflected on account", "value": "No"}],
                        {"order_ref": order_ref, "current": "not reflected",
                         "expected": "reflected on account", "field": "reflected_on_account"})
    if status.lower() in ("inprogress", "in progress") :
        return _verdict("ambiguous",
                        f"Order {order_ref} is reflected on the account but still '{status}' at "
                        f"'{stage}' with no blocking task. Manual review required.", base)
    return _verdict("not_confirmed",
                    f"Order {order_ref} is '{status}' and reflected on the account — nothing stuck.",
                    base)


@tool(
    "oms.check_network_type",
    input_schema={
        "type": "object",
        "properties": {
            "ban": {"type": "string", "description": "Billing account number, e.g. '314116701'."}
        },
        "required": ["ban"],
    },
)
def check_network_type(ban: str = "") -> dict:
    """Check whether the banner shown on an account matches its provisioned network
    type. Use this when the ticket reports a wrong account banner — e.g. showing
    postpaid fiber when the service is copper."""
    acct = oms_store.get_account(ban)
    if not acct:
        return _verdict("not_confirmed", f"No account found for BAN {ban} in OMS.")

    network = str(acct.get("network_type", "")).strip()
    banner = str(acct.get("banner", "")).strip()
    details = [{"label": "BAN", "value": acct.get("ban", "")},
               {"label": "Banner (current)", "value": banner},
               {"label": "Provisioned network type", "value": network}]

    if not network or not banner:
        return _verdict("ambiguous", f"BAN {ban} is missing a banner or network type in OMS.", details)
    if network.lower() in banner.lower() or banner.lower() in network.lower():
        return _verdict("not_confirmed",
                        f"Banner for BAN {ban} already matches the provisioned network type "
                        f"({network}) — nothing to correct.", details)
    return _verdict("confirmed",
                    f"Banner for BAN {ban} is wrong: shows '{banner}' but the account is "
                    f"provisioned as '{network}'.",
                    details + [{"label": "Banner (expected)", "value": network}],
                    {"ban": ban, "current": banner, "expected": network, "field": "banner"})


# ── routing domain ───────────────────────────────────────────────────────

@tool(
    "routing.check_redirect_queue",
    input_schema={
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Incident number to classify."}
        },
        "required": [],
    },
)
def check_redirect_queue(number: str = "", _ticket: dict = None) -> dict:
    """Check whether a ticket belongs to another team's queue (e.g. Buy Flow) and
    should be REASSIGNED rather than remediated here. Matching is rule-based and
    deterministic; rules live in data/routing_rules.json.

    The engine calls this directly for every ticket before LLM routing, so a
    redirect never depends on retrieval quality."""
    import routing_store
    import servicenow_client

    ticket = _ticket
    if ticket is None:
        ticket = servicenow_client.get_ticket(number) if number else None
    if not ticket:
        return _verdict("not_confirmed", f"Ticket {number} not found; cannot classify ownership.")

    hit = routing_store.classify(ticket)
    if not hit:
        return _verdict("not_confirmed",
                        "Ticket does not match any redirect rule — it is owned by this queue.")
    if not hit["assignment_group"]:
        return _verdict("ambiguous",
                        f"Ticket matches the '{hit['label']}' redirect rule, but no assignment "
                        f"group is configured for it in data/routing_rules.json.",
                        [{"label": "Matched rule", "value": hit["label"]},
                         {"label": "Evidence", "value": hit["evidence"]}], hit)
    return _verdict("confirmed",
                    f"{hit['label']} ticket — owned by '{hit['assignment_group']}', not this queue.",
                    [{"label": "Matched rule", "value": hit["label"]},
                     {"label": "Reassign to", "value": hit["assignment_group"]},
                     {"label": "Evidence", "value": hit["evidence"]}],
                    hit)


# ── registry API ─────────────────────────────────────────────────────────

def get_tool(name):
    return TOOLS.get(name)


def run_tool(name, identifiers: dict) -> dict:
    t = TOOLS.get(name)
    if t is None:
        return {"error": f"unknown tool: {name}"}
    return t.run(identifiers)


def tool_schemas() -> list:
    """All tool descriptors — for the router, /fallout/tools, or MCP."""
    return [t.schema() for t in TOOLS.values()]
