"""
ServiceNow Client
-----------------
Read + comment access to the fallout incidents in ServiceNow. This is the single
source of truth for ticket data (replacing the old Jira layer).

Design constraints (agreed with the product owner):
  * READ tickets and PARSE the labelled block that lives in the `comments`
    journal field (native incident fields are not populated).
  * WRITE as a comment (post_comment), and — for redirect rules only —
    REASSIGN by setting `assignment_group` (reassign).
  * This client NEVER closes, resolves, or transitions a ticket. `reassign`
    changes ownership only; `state` is deliberately not in its payload.
"""

import os
import re
import time
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

import truststore
truststore.inject_into_ssl()

import fake_tickets

load_dotenv()

INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
USER = os.getenv("SERVICENOW_USER", "")
PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")
CLIENT_ID = os.getenv("SERVICENOW_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SERVICENOW_CLIENT_SECRET", "").strip()

AUTH = HTTPBasicAuth(USER, PASSWORD)
HEADERS = {"Accept": "application/json"}


# ── authentication ──────────────────────────────────────────────────────
#
# OAuth when a client is configured, Basic otherwise.
#
# ServiceNow now ships "Basic Auth API Restriction" (see
# glide.authenticate.basic_auth.restriction.*), which blocks Basic Auth for REST
# while leaving UI login working — so an instance can look perfectly healthy in a
# browser while every API call returns 401 "User is not authenticated". OAuth is
# not subject to that restriction, which is why it is the default path here.
#
# The password grant is used deliberately over client_credentials: it binds the
# token to a real user, so comments and reassignments are attributed to that
# account in the ticket's audit trail rather than to an anonymous integration.
#
# Falling back to Basic keeps this working against older instances that have no
# OAuth client registered.

_token = {"access_token": "", "expires_at": 0.0}


def _fetch_token() -> str:
    """Get an access token, reusing the cached one until it is nearly expired."""
    if _token["access_token"] and time.time() < _token["expires_at"]:
        return _token["access_token"]

    resp = requests.post(
        f"{INSTANCE}/oauth_token.do", timeout=30,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "password", "client_id": CLIENT_ID,
              "client_secret": CLIENT_SECRET, "username": USER, "password": PASSWORD},
    )
    resp.raise_for_status()
    data = resp.json()
    _token["access_token"] = data["access_token"]
    # Renew a minute early so a token cannot expire mid-request.
    _token["expires_at"] = time.time() + max(int(data.get("expires_in", 1800)) - 60, 60)
    return _token["access_token"]


def _auth_kwargs(extra_headers: dict = None) -> dict:
    """Auth for a requests call: Bearer when OAuth is configured, else Basic."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    if CLIENT_ID and CLIENT_SECRET:
        headers["Authorization"] = f"Bearer {_fetch_token()}"
        return {"headers": headers}
    return {"headers": headers, "auth": AUTH}


def _request(method: str, url: str, **kwargs):
    """HTTP with auth applied, retrying once on 401 with a fresh token.

    A cached token can be revoked or invalidated server-side before it expires, so
    a single 401 is treated as "token is stale" rather than a hard failure.
    """
    extra = kwargs.pop("headers", None)
    resp = requests.request(method, url, **_auth_kwargs(extra), **kwargs)
    if resp.status_code == 401 and CLIENT_ID and CLIENT_SECRET:
        _token["access_token"] = ""
        resp = requests.request(method, url, **_auth_kwargs(extra), **kwargs)
    return resp

# Which incidents this app manages.
#
# A real deployment works a QUEUE — the incidents assigned to its own group — so
# that is the default. Matching instead on the word "fallout" in short_description
# (the original behaviour) fails on genuine tickets, whose summaries read like
# "Remove TN (973) 396-2160 on BAN 1000302044" and never say "fallout".
#
# Consequence worth knowing: once a Buy Flow ticket is reassigned to BUYFLOW TEAM it
# correctly DROPS OUT of this queue — it is no longer ours. Lookups by number
# (get_ticket) are unaffected, so an approved ticket can still be inspected.
#
# SERVICENOW_QUERY overrides the whole thing if a deployment needs different rules.
QUEUE_GROUP = os.getenv("SERVICENOW_QUEUE_GROUP", "BOSS OM IT Support").strip()
FALLOUT_QUERY = os.getenv("SERVICENOW_QUERY", "").strip() or (
    f"assignment_group.name={QUEUE_GROUP}^ORDERBYnumber" if QUEUE_GROUP
    else "short_descriptionLIKEfallout^ORDERBYnumber"
)

INCIDENT_FIELDS = ("number,sys_id,short_description,description,state,comments,"
                   "sys_created_on,sys_updated_on")

# ── low-level HTTP ──────────────────────────────────────────────────────

def _table_get(table: str, params: dict) -> list:
    url = f"{INSTANCE}/api/now/table/{table}"
    resp = _request("GET", url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", [])


def _dv(field):
    """Pull the display value from a sysparm_display_value=all field shape."""
    if isinstance(field, dict):
        return field.get("display_value", field.get("value", "")) or ""
    return field or ""


# ── comment-block parsing ───────────────────────────────────────────────

_ENTRY_SPLIT = re.compile(r"(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - .+? \(Comments\)\s*$")

# A single-line "Label: value" field pair.
_FIELD_LINE = re.compile(r"^([A-Za-z][\w /#().&-]*?):[ \t]+(\S.*)$")
# A section header — a label on its own line with no value (e.g. "Description:").
_SECTION_HEADER = re.compile(r"^([A-Za-z][\w /#().&-]*?):[ \t]*$")


# Comments this app posted itself. Its own output must never be mistaken for the
# ticket's source data: format_comment() opens with a "[Remediation - …]" header, and
# the generic Label: value parser would happily read that back as ticket fields.
_OWN_COMMENT = re.compile(r"^\s*\[(?:Remediation|Recommendation)\b", re.I | re.M)

# An identifier line that marks a block as the ticket's real fallout data. Two id
# schemes are in play — LOC-/SVC- (duplicate service, porting) and BAN/order id
# (BOSS OM) — so keying on 'Location ID:' alone silently missed every BOSS OM
# ticket and fell through to returning the entire journal.
_ID_LABEL = re.compile(r"^(?:Location ID|BAN|Order Reference|Order Task|Port Request|"
                       r"Customer Order|Provisioning Request|Subcategory)\s*:", re.I | re.M)


def _canonical_block(comments: str) -> str:
    """Return the most relevant comment block.

    The journal is newest-first and often holds several near-duplicate re-saves. We
    take the FIRST (newest) entry that looks like genuine fallout data — an
    identifier label AND a Description — then relax to just an identifier label.

    Entries this app wrote are excluded at every stage. Without that, approving a
    ticket poisons its own source: the posted comment lands at the top of the
    journal and the next fetch parses it as the ticket's fields.
    """
    if not comments:
        return ""
    parts = _ENTRY_SPLIT.split(comments)
    parts = [p.strip() for p in parts if p.strip()]
    parts = [p for p in parts if not _OWN_COMMENT.search(p)] or parts

    genuine = [p for p in parts if _ID_LABEL.search(p) and "Description:" in p]
    if genuine:
        return genuine[0]
    with_id = [p for p in parts if _ID_LABEL.search(p)]
    if with_id:
        return with_id[0]
    # No structured block — return the remaining text so callers still get something.
    return "\n".join(parts).strip() or comments.strip()


def _parse_block(block: str):
    """Generically split a labelled comment block into:
      - fields:   ordered list of {label, value} single-line 'Label: value' pairs
      - sections: dict of multi-line free-text sections keyed by their header
                  (e.g. 'Description', 'Work Notes', 'Resolution Notes').
    Fallout-type agnostic: whatever labels a ticket uses are captured, so new
    fallout types with different fields need no parser changes."""
    fields = []
    sections = {}
    current = None
    buf = []
    in_sections = False
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            if in_sections and current is not None:
                buf.append("")
            continue
        header = _SECTION_HEADER.match(line)          # 'Label:' with no value
        if header:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = header.group(1).strip()
            buf = []
            in_sections = True
            continue
        if in_sections:
            buf.append(line)
            continue
        pair = _FIELD_LINE.match(line)                # 'Label: value' on one line
        if pair:
            fields.append({"label": pair.group(1).strip(), "value": pair.group(2).strip()})
        elif fields:
            fields[-1]["value"] += " " + line          # continuation of prior value
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return fields, sections


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else ""



# Keyword -> fallout type, used ONLY to infer a subcategory for a ticket raised
# through the normal ServiceNow form (which has no labelled block). Ordered most
# specific first. This is a best-effort hint so the deterministic router still has
# something to work with; a labelled block always wins over it.
_SUBCATEGORY_HINTS = [
    ("Digital Buyflow",        r"buy\s?flow|byflow"),
    ("Network Type Mismatch",  r"\bbanner\b|network type|postpaid fiber|copper banner"),
    ("Task Closure",           r"\bOMTASK\w*|open task|past due date|blocking task"),
    ("Staging Stuck",          r"not reflect\w*|not showing (?:on|in) the (?:order|account)|"
                               r"order history|stuck in staging|speed upgrade"),
    ("Service Profile Issue",  r"service profile|\b2 services\b|two services|duplicate service|"
                               r"\bBRIM\b|\bSID\b"),
    ("Account Status Mismatch", r"no disconnect order|account is inactive|not showing to be active|"
                                r"account status|still (?:showing |in )?pending"),
    ("Cancellation Completion", r"\bcancel\w*|disconnect order"),
]


def _infer_subcategory(text: str) -> str:
    for label, pattern in _SUBCATEGORY_HINTS:
        if re.search(pattern, text or "", re.I):
            return label
    return ""


def _native_block(short_desc: str, description: str) -> str:
    """Synthesise a labelled block from an incident's native fields.

    This app expects its structured data in a labelled block inside the `comments`
    journal, because that is how the BOSS OM instance it was built against works.
    An incident raised through the ordinary ServiceNow form has none of that — the
    text sits in `description` — and would otherwise parse to an empty ticket with
    no identifiers, no subcategory, and nothing to route on.

    Rendering those fields INTO the same block format means every downstream
    extractor (identifier regexes, section lookup, the router) keeps working
    unchanged rather than needing a second code path.
    """
    text = (description or "").strip()
    if not text:
        return ""
    sub = _infer_subcategory(f"{short_desc}\n{text}")
    lines = []
    if sub:
        lines.append(f"Subcategory: {sub}")
    lines += ["Description:", text]
    return "\n".join(lines)


# ── ticket age ──────────────────────────────────────────────────────────
# Age is the queue's de-facto priority signal: fallout that has sat for a week is
# more urgent than the same fallout raised this morning, and nothing else on these
# incidents carries priority (native fields are unpopulated). Computed once here so
# the queue list, the ticket header and the posted comment cannot disagree.

_AGE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S")

# (minimum days, band). Ordered oldest-first; first match wins.
_AGE_BANDS = ((14, "critical"), (7, "stale"), (3, "aging"))


def _age(created: str) -> dict:
    """Derive age from the created timestamp. Empty dict when it cannot be read."""
    raw = (created or "").strip()
    if not raw:
        return {}
    stamp = None
    for fmt in _AGE_FORMATS:
        try:
            stamp = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if stamp is None:
        return {}

    delta = datetime.now() - stamp
    hours = delta.total_seconds() / 3600
    if hours < 0:            # clock skew between ServiceNow and here
        hours = 0
    days = int(hours // 24)

    if hours < 1:
        label = "under an hour"
    elif days < 1:
        label = f"{int(hours)} hour{'s' if int(hours) != 1 else ''}"
    else:
        label = f"{days} day{'s' if days != 1 else ''}"

    band = "fresh"
    for threshold, name in _AGE_BANDS:
        if days >= threshold:
            band = name
            break

    return {"opened": raw, "age_days": days, "age_hours": round(hours, 1),
            "age_label": label, "age_band": band}


def parse_ticket(raw: dict) -> dict:
    """Convert a raw incident record (sysparm_display_value=all) into a clean
    structured fallout record. Extraction is generic — every labelled line is
    captured in `fields`, every free-text section in `sections` — while a few
    convenience keys are derived for the remediation engine."""
    number = _dv(raw.get("number"))
    state = _dv(raw.get("state"))
    short_desc = _dv(raw.get("short_description"))
    comments = _dv(raw.get("comments"))
    block = _canonical_block(comments)
    # No identifier label anywhere in the journal means this ticket was raised through
    # the normal ServiceNow form rather than by the fallout tooling — fall back to its
    # native fields so it still parses into something usable.
    if not _ID_LABEL.search(block or ""):
        native = _native_block(short_desc, _dv(raw.get("description")))
        if native:
            block = native

    fields, sections = _parse_block(block)
    fmap = {f["label"].lower(): f["value"] for f in fields}

    def sect(name):
        for k, v in sections.items():
            if k.lower() == name.lower():
                return v
        return ""

    # Order reference: Customer Order / Provisioning Request / Dispatch Request.
    order_ref, order_type = "", ""
    for label in ("Customer Order", "Provisioning Request", "Dispatch Request"):
        if fmap.get(label.lower()):
            order_ref, order_type = fmap[label.lower()], label
            break
    if not order_ref:
        order_ref = _first(r"((?:CO|PR|DR)-\d+)", block)

    location_id = fmap.get("location id") or _first(r"(LOC-\d+)", block)
    tn = fmap.get("tn") or _first(r"(\(\d{3}\)\s*\d{3}-\d{4})", short_desc)

    # BAN / order-task identifiers. The BOSS OM fallout types key on these rather
    # than on LOC-/SVC- ids, and they are usually written inline rather than given
    # their own label — so recover them the same way kb_source does, keeping the
    # live-ticket shape symmetric with the spreadsheet KB shape.
    ban = (fmap.get("ban") or _first(r"\bBANS?\b\s*[:#\-]?\s*(\d{6,12})", block)
           or _first(r"\baccount\s*(?:number|no|#)\s*[:\-]?\s*(\d{6,12})", block)
           or _first(r"[-–]\s*(\d{6,12})\s*$", short_desc))
    task_ref = fmap.get("order task") or _first(r"\b(OMTASK\d+)\b", block)
    if not order_ref:
        order_ref = _first(r"\b([A-Z]{2}\d{8,12}[A-Z]?)\b", block)

    sys_id = _dv(raw.get("sys_id"))
    return {
        "number": number,
        "sys_id": sys_id,
        # Deep link to the incident in the ServiceNow UI, so the agent can open the
        # real ticket and see the comment that was posted. Empty for demo tickets,
        # which have no sys_id and do not exist in any instance.
        "url": f"{INSTANCE}/nav_to.do?uri=incident.do%3Fsys_id%3D{sys_id}" if sys_id else "",
        "state": state,
        "is_closed": state.strip().lower() in ("closed", "resolved"),
        "short_description": short_desc,
        "subcategory": fmap.get("subcategory", ""),
        "category": fmap.get("category", ""),
        "location_id": location_id,
        "ban": ban,
        "tn": tn,
        "order_ref": order_ref,
        "order_type": order_type,
        "task_ref": task_ref,
        "service_type": fmap.get("service type", ""),
        "resolution_code": fmap.get("resolution code", ""),
        "referenced_service_id": _first(r"(SVC-\d+)", block),
        "description": sect("Description"),
        "work_notes": sect("Work Notes"),
        "resolution_notes": sect("Resolution Notes"),
        # Generic, type-agnostic views for the dynamic UI:
        "fields": fields,
        "sections": [{"label": k, "text": v} for k, v in sections.items()],
        "updated": _dv(raw.get("sys_updated_on")),
        **_age(_dv(raw.get("sys_created_on"))),
    }


# ── public API ──────────────────────────────────────────────────────────

def fetch_fallout_tickets() -> list:
    """Fetch and parse every fallout incident. Returns a list of structured dicts.

    An unreachable or unconfigured instance degrades to "no live tickets" rather
    than propagating. The demo tickets in data/fake_tickets.json are the supported
    way to run this app with no ServiceNow at all (see fake_tickets.py), and a
    hard failure here would take the whole queue down with it — including tickets
    that need no instance to work. The failure is logged, never swallowed
    silently, so a genuinely broken instance is still visible in the log.
    """
    try:
        rows = _table_get("incident", {
            "sysparm_query": FALLOUT_QUERY,
            "sysparm_fields": INCIDENT_FIELDS,
            "sysparm_display_value": "all",
            "sysparm_limit": 200,
        })
    except Exception as e:
        print(f"[WARN] ServiceNow unavailable ({type(e).__name__}) — serving demo "
              f"tickets only. Check SERVICENOW_INSTANCE/USER/PASSWORD in backend/.env.")
        rows = []
    # Real ServiceNow tickets, plus any synthetic demo tickets (data/fake_tickets.json).
    return [parse_ticket(r) for r in rows] + fake_tickets.all_fake()


def fetch_closed_kb() -> list:
    """Closed fallout tickets (with resolutions) — the historical knowledge base."""
    return [t for t in fetch_fallout_tickets() if t["is_closed"]]


def fetch_open_queue() -> list:
    """Open fallout tickets — the queue an agent works."""
    return [t for t in fetch_fallout_tickets() if not t["is_closed"]]


def get_ticket(number: str) -> dict | None:
    # Synthetic demo tickets are already in parsed shape — return them directly.
    fake = fake_tickets.get(number)
    if fake:
        return fake
    rows = _table_get("incident", {
        "sysparm_query": f"number={number}",
        "sysparm_fields": INCIDENT_FIELDS,
        "sysparm_display_value": "all",
        "sysparm_limit": 1,
    })
    return parse_ticket(rows[0]) if rows else None


def _sys_id_for(number: str) -> str | None:
    rows = _table_get("incident", {
        "sysparm_query": f"number={number}",
        "sysparm_fields": "sys_id",
        "sysparm_limit": 1,
    })
    return rows[0]["sys_id"] if rows else None


def post_comment(number: str, text: str) -> dict:
    """Append a customer-visible comment to a ticket. Does NOT change state,
    close, or resolve the ticket — comment only."""
    # Synthetic demo tickets don't exist in ServiceNow — simulate the post.
    if fake_tickets.is_fake(number):
        return {"number": number, "sys_id": None, "posted": False, "simulated": True,
                "note": "Demo ticket — comment was not posted to ServiceNow."}
    sys_id = _sys_id_for(number)
    if not sys_id:
        raise ValueError(f"Ticket {number} not found")
    url = f"{INSTANCE}/api/now/table/incident/{sys_id}"
    resp = _request("PATCH", url,
                    headers={"Content-Type": "application/json"},
                    json={"comments": text}, timeout=30)
    resp.raise_for_status()
    return {"number": number, "sys_id": sys_id, "posted": True}


def reassign(number: str, assignment_group: str) -> dict:
    """Move a ticket to another team by setting `assignment_group`.

    Ownership only — `state` is deliberately absent from the payload, so this
    never closes, resolves, or transitions the incident.

    ServiceNow resolves `assignment_group` by name against sys_user_group. A name
    that does not resolve is silently DROPPED rather than rejected, so the value is
    read back after the write and a mismatch is reported as a failure. Otherwise a
    typo in routing_rules.json would look like a successful reassignment while the
    ticket sat untouched in the original queue.
    """
    if not (assignment_group or "").strip():
        raise ValueError("assignment_group is required to reassign")

    if fake_tickets.is_fake(number):
        return {"number": number, "sys_id": None, "reassigned": False, "simulated": True,
                "assignment_group": assignment_group,
                "note": f"Demo ticket — not moved in ServiceNow (would go to '{assignment_group}')."}

    sys_id = _sys_id_for(number)
    if not sys_id:
        raise ValueError(f"Ticket {number} not found")

    url = f"{INSTANCE}/api/now/table/incident/{sys_id}"
    resp = _request("PATCH", url,
                    headers={"Content-Type": "application/json"},
                    params={"sysparm_fields": "assignment_group",
                            "sysparm_display_value": "all"},
                    json={"assignment_group": assignment_group}, timeout=30)
    resp.raise_for_status()

    landed = _dv((resp.json().get("result") or {}).get("assignment_group"))
    if landed.strip().lower() != assignment_group.strip().lower():
        return {"number": number, "sys_id": sys_id, "reassigned": False,
                "assignment_group": landed,
                "error": (f"ServiceNow did not accept assignment group "
                          f"'{assignment_group}' — the ticket is still assigned to "
                          f"'{landed or 'nobody'}'. Check the exact group name in "
                          f"data/routing_rules.json.")}
    return {"number": number, "sys_id": sys_id, "reassigned": True,
            "assignment_group": landed}


# ── incident creation (third write operation) ─────────────────────────────
#
# This app's write surface was deliberately limited to two operations,
# post_comment and reassign, and CLAUDE.md records that adding a third is a
# product decision rather than an implementation detail. The product owner has
# now signed that off for the customer conversation: when the knowledge base has
# no answer, the customer can have a ticket raised.
#
# The guards are the same ones the other writes use, for the same reasons:
#   * `state` is absent from the payload, so a created incident lands in the
#     instance default (New) and this client still never transitions anything.
#   * `assignment_group` is read back after the write, because ServiceNow silently
#     DROPS a group name it cannot resolve. Without the read-back, a typo would
#     produce an unassigned ticket that looked like a success.
#   * Creation is restricted to the demo group by default, so a bug here cannot
#     spray tickets into a live queue.

DEMO_GROUP = os.getenv("SERVICENOW_DEMO_GROUP", "TICKETGENIE DEMO").strip()


def create_incident(short_description: str, description: str,
                    assignment_group: str = "", extra: dict = None) -> dict:
    """Create an incident and return {number, sys_id, url, assignment_group}.

    Only called after every required field has been collected and the customer has
    confirmed the details (see fallout_engine's ticket flow). Never sets state.
    """
    if not (short_description or "").strip():
        raise ValueError("short_description is required to create an incident")
    if not (description or "").strip():
        raise ValueError("description is required to create an incident")

    group = (assignment_group or DEMO_GROUP).strip()
    payload = {"short_description": short_description.strip(),
               "description": description.strip()}
    if group:
        payload["assignment_group"] = group
    # Caller-supplied extras are allowed, but `state` is stripped no matter what is
    # passed: this client does not transition tickets, and that rule is enforced here
    # rather than trusted to every call site.
    for k, v in (extra or {}).items():
        if k in ("state", "incident_state", "close_code", "close_notes", "resolved_at"):
            continue
        if str(v or "").strip():
            payload[k] = v

    url = f"{INSTANCE}/api/now/table/incident"
    resp = _request("POST", url,
                    headers={"Content-Type": "application/json"},
                    params={"sysparm_fields": "number,sys_id,assignment_group,"
                                              "category,subcategory",
                            "sysparm_display_value": "all"},
                    json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json().get("result") or {}

    number = _dv(result.get("number"))
    sys_id = _dv(result.get("sys_id"))
    landed = _dv(result.get("assignment_group"))

    out = {"number": number, "sys_id": sys_id, "created": bool(number),
           "assignment_group": landed,
           "category": _dv(result.get("category")),
           "subcategory": _dv(result.get("subcategory")),
           "url": f"{INSTANCE}/nav_to.do?uri=incident.do%3Fsys_id%3D{sys_id}" if sys_id else ""}
    if group and landed.strip().lower() != group.strip().lower():
        # The ticket exists, so this is a warning rather than a failure. Say so
        # precisely instead of reporting a clean success.
        out["warning"] = (f"Incident {number} was created but ServiceNow did not accept "
                          f"assignment group '{group}' (it shows '{landed or 'nobody'}'). "
                          f"Check that the group exists.")
    # Subcategory is read back for the same reason as the group: ServiceNow drops a
    # value that is not in the choice list, and only resolves a choice under its own
    # dependent category, so a silent drop is the expected failure rather than a 400.
    want_sub = str((extra or {}).get("subcategory", "") or "").strip()
    if want_sub and out["subcategory"].strip().lower() != want_sub.lower():
        out.setdefault("warning", "")
        out["warning"] += (f" Subcategory '{want_sub}' was not accepted "
                           f"(it shows '{out['subcategory'] or 'nothing'}').")
    return out


_subcategory_choices = None


def subcategory_choices(refresh: bool = False) -> dict:
    """{subcategory value: the category value it depends on}, read from the instance.

    ServiceNow stores subcategory as a dependent choice: a value that is not in the
    list is silently dropped on write, and a value only resolves under its own
    dependent category. Both facts are read from sys_choice rather than hardcoded, so
    adding a choice on the instance is enough to make it writable here.

    Cached for the process, since the choice list changes only when an instance is
    being set up.
    """
    global _subcategory_choices
    if _subcategory_choices is not None and not refresh:
        return _subcategory_choices
    try:
        rows = _table_get("sys_choice", {
            "sysparm_query": "name=incident^element=subcategory^inactive=false",
            "sysparm_fields": "value,dependent_value",
            "sysparm_limit": 500,
        })
        _subcategory_choices = {r["value"]: r.get("dependent_value", "")
                                for r in rows if r.get("value")}
    except Exception as e:
        print(f"[SN] could not read the subcategory choice list ({type(e).__name__}); "
              f"new tickets will be created unclassified.")
        _subcategory_choices = {}
    return _subcategory_choices


def classify(issue_type: str) -> dict:
    """{'category': ..., 'subcategory': ...} for an issue type, or {} when the
    instance has no such choice. Writing an unknown value would be dropped without
    an error, so it is not attempted."""
    value = (issue_type or "").strip()
    if not value:
        return {}
    choices = subcategory_choices()
    for known, dependent in choices.items():
        if known.strip().lower() == value.lower():
            return ({"category": dependent, "subcategory": known} if dependent
                    else {"subcategory": known})
    return {}


def find_group(name: str) -> dict | None:
    """Look up an assignment group by exact name. Read-only."""
    if not (name or "").strip():
        return None
    rows = _table_get("sys_user_group", {
        "sysparm_query": f"name={name.strip()}",
        "sysparm_fields": "sys_id,name",
        "sysparm_limit": 1,
    })
    return rows[0] if rows else None


def create_group(name: str, description: str = "") -> dict:
    """Create an assignment group, or return the existing one.

    Needed because ServiceNow drops an assignment_group it cannot resolve, so the
    group has to exist before any ticket can be routed to it.
    """
    existing = find_group(name)
    if existing:
        return {"sys_id": existing["sys_id"], "name": existing["name"], "created": False}
    resp = _request("POST", f"{INSTANCE}/api/now/table/sys_user_group",
                    headers={"Content-Type": "application/json"},
                    params={"sysparm_fields": "sys_id,name"},
                    json={"name": name,
                          "description": description or f"{name} (created by TicketGenie)"},
                    timeout=30)
    resp.raise_for_status()
    r = resp.json().get("result") or {}
    return {"sys_id": _dv(r.get("sys_id")), "name": _dv(r.get("name")), "created": True}


def fetch_group_tickets(group: str) -> list:
    """Every parsed incident assigned to one group. Read-only."""
    if not (group or "").strip():
        return []
    try:
        rows = _table_get("incident", {
            "sysparm_query": f"assignment_group.name={group.strip()}^ORDERBYnumber",
            "sysparm_fields": INCIDENT_FIELDS,
            "sysparm_display_value": "all",
            "sysparm_limit": 500,
        })
    except Exception as e:
        print(f"[WARN] could not read group {group!r} from ServiceNow: {e}")
        raise
    return [parse_ticket(r) for r in rows]


# The knowledge base is drawn from more than one group. The two demo domains are
# kept in SEPARATE ServiceNow groups so they stay distinguishable in the UI, but
# both are searchable, so the reader spans them.
SUPPORT_GROUP = os.getenv("SERVICENOW_SUPPORT_GROUP",
                          "BUS Sales Ordering and Digital Support").strip()


def kb_groups() -> list:
    """The assignment groups whose closed incidents form the knowledge base."""
    return [g for g in (DEMO_GROUP, SUPPORT_GROUP) if g]


def fetch_demo_closed_kb() -> list:
    """Closed incidents across every knowledge-base group, when KB_SOURCE is
    'servicenow'. Seeded by seed_demo_tickets.py and seed_support_tickets.py.

    A group that cannot be read is skipped with a warning rather than failing the
    whole load, so one empty or missing group does not take the KB down with it.
    """
    out, seen = [], set()
    for group in kb_groups():
        try:
            tickets = fetch_group_tickets(group)
        except Exception as e:
            print(f"[WARN] KB group {group!r} unreadable ({type(e).__name__}); skipping.")
            continue
        closed = [t for t in tickets if t["is_closed"]]
        print(f"[FALLOUT-KB] group {group!r}: {len(closed)} closed")
        for t in closed:
            # Incident numbers are unique across the instance, but guard anyway so a
            # ticket sitting in two groups cannot be indexed twice.
            if t["number"] not in seen:
                seen.add(t["number"])
                # Stamp the owning group onto the ticket. This is what the chat reads
                # back after retrieval to decide where a NEW ticket should be filed,
                # so a Business Hub question does not raise its ticket in the modem
                # queue. Nothing else uses it.
                t["kb_group"] = group
                out.append(t)
    return out
