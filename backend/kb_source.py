"""
Knowledge-Base Source (Excel)
-----------------------------
The historical knowledge base — the CLOSED tickets the recommendation pipeline
retrieves from — is loaded from a SPREADSHEET (.xlsx / .xls / .csv) instead of
from ServiceNow.

ServiceNow is still the source of the OPEN queue an agent works; only the KB side
moved here.

Column mapping
--------------
Real exports never share one set of headers, so columns are resolved in three
passes and the FIRST hit wins:

    1. an explicit override in data/kb_column_map.json   ({"number": "Ticket #"} ...)
    2. an exact case/space-insensitive match on a known alias
    3. a substring match on a known alias

Anything that resolves to nothing is simply left blank — a missing column is never
an error. Run `python kb_source.py` to print exactly how YOUR file was mapped
before trusting the KB.

Closed filter
-------------
Only rows whose state/status reads as closed are returned by fetch_closed_kb().
CLOSED_STATES below is matched case-insensitively, and 'closed'/'resolved' also
match as a prefix (so "Closed Complete", "Resolved - Duplicate" both count).
"""

import os
import re
import json

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Explicit path wins; otherwise the newest spreadsheet in data/ that looks like an
# incident export is used. Set KB_EXCEL_PATH in backend/.env to pin one file.
KB_EXCEL_PATH = os.getenv("KB_EXCEL_PATH", "")
COLUMN_MAP_PATH = os.path.join(DATA_DIR, "kb_column_map.json")

SHEET_NAME = os.getenv("KB_EXCEL_SHEET", "")   # blank = first sheet

CLOSED_STATES = {
    "closed", "resolved", "solved", "fixed", "complete", "completed", "done",
    "closed complete", "closed/resolved", "cancelled", "canceled",
}
# States that count as closed when they merely START with one of these.
CLOSED_PREFIXES = ("closed", "resolved", "complete")

# Vocabulary used to CONFIRM that a candidate 'state' column really holds ticket
# lifecycle values. Incident exports frequently carry a geographic "State" column
# too, and picking that one produces a KB with zero closed tickets — a silent,
# expensive failure. A column only wins if its values actually look like states.
LIFECYCLE_VOCAB = CLOSED_STATES | {
    "open", "new", "active", "in progress", "inprogress", "work in progress",
    "pending", "on hold", "awaiting", "assigned", "reopened", "closed complete",
    "closed incomplete", "closed skipped", "in review", "escalated",
}

# Aliases that are too generic to match on a SUBSTRING. 'task' would otherwise
# claim ServiceNow's `task_effective_number` (which holds the incident number, not
# an order task) and shadow the OMTASK regex; 'order' would claim the numeric
# `order` sort field. They still win an EXACT header match, where the intent is
# unambiguous.
EXACT_ONLY_ALIASES = {"task", "order", "location", "service", "plan", "type", "area", "id"}

# Canonical field -> header aliases (lowercased, punctuation-stripped on compare).
COLUMN_ALIASES = {
    "number": [
        "number", "ticket", "ticket #", "ticket no", "ticket number", "ticket id",
        "incident", "incident number", "incident id", "inc", "inc number", "id",
        "case number", "case id", "task",
    ],
    # 'status' before 'state': incident exports often ALSO carry a geographic
    # "State" column, and matching that one silently yields a KB of zero closed
    # tickets. _pick_state_column() additionally checks the VALUES to be sure.
    "state": [
        "status", "incident state", "ticket status", "incident status",
        "ticket state", "current state", "state", "stage",
    ],
    "short_description": [
        "short description", "shortdescription", "summary", "title", "subject",
        "customer complaint", "complaint", "issue", "problem statement", "headline",
    ],
    "description": [
        "description", "long description", "details", "detail", "issue description",
        "problem description", "notes", "additional information", "narrative",
    ],
    # Issue-type aliases come FIRST and 'category' last on purpose. Subcategory is
    # one of the four EMBEDDED fields, so it must carry the discriminative fallout
    # type ('Staging Stuck', 'Account Status Mismatch'), not a coarse application
    # bucket ('Contact Center Applications') that every row shares.
    "subcategory": [
        "u issue type", "issue type", "fallout type", "issue category",
        "subcategory", "sub category", "sub-category", "classification",
        "area", "type", "category",
    ],
    "category": ["category", "application", "app category", "service area"],
    "service_type": [
        "service type", "servicetype", "product", "product type", "offering",
        "plan", "service", "service offering",
    ],
    "location_id": [
        "location id", "locationid", "location", "loc id", "loc", "site id",
        "site", "premise id", "premise", "service address id",
    ],
    "tn": [
        "tn", "telephone number", "telephone", "phone", "phone number",
        "btn", "msisdn", "contact number",
    ],
    "order_ref": [
        "order ref", "order reference", "order number", "order id", "order",
        "customer order", "provisioning request", "dispatch request",
        "work order", "so number", "sales order",
    ],
    "ban": ["ban", "billing account number", "account number", "account", "billing account"],
    "task_ref": ["task", "order task", "task id", "task number", "omtask"],
    "order_type": ["order type", "ordertype", "request type", "order category"],
    "resolution_code": [
        "resolution code", "resolutioncode", "close code", "closure code",
        "close notes code", "resolution category", "cause code", "closure reason",
    ],
    "resolution_notes": [
        "resolution notes", "resolutionnotes", "resolution", "close notes",
        "closure notes", "solution", "fix", "how resolved", "action taken",
        "corrective action",
    ],
    "work_notes": [
        "work notes", "worknotes", "activity", "activity log", "comments",
        "additional comments", "troubleshooting", "investigation",
    ],
    "root_cause": ["root cause", "root_cause", "rca", "cause", "root cause analysis"],
    "referenced_service_id": [
        "service id", "serviceid", "svc id", "svc", "circuit id", "subscriber id",
    ],
    "updated": [
        "updated", "sys updated on", "last updated", "closed at", "resolved at",
        "date", "closed date", "resolution date", "modified",
    ],
}

# Fields whose value is worth surfacing in the UI's generic `fields` list.
_FIELD_ORDER = [
    "state", "subcategory", "category", "service_type", "ban", "location_id", "tn",
    "order_ref", "order_type", "task_ref", "referenced_service_id", "resolution_code",
]
_FIELD_LABELS = {
    "state": "State", "subcategory": "Issue Type", "category": "Category",
    "service_type": "Service Type", "ban": "BAN", "location_id": "Location ID",
    "tn": "TN", "order_ref": "Order Reference", "order_type": "Order Type",
    "task_ref": "Order Task", "referenced_service_id": "Service ID",
    "resolution_code": "Resolution Code",
}

# ── identifiers recovered from free text ─────────────────────────────────
# This export keys on BAN + order id + OMTASK rather than the LOC-/SVC- scheme the
# original demo data used, and none of them get their own column — they are written
# inline in the description and close notes. The remediation router needs them, so
# they are pulled out deterministically here.

# {RCA TAG : Order Completion - Staging Stuck} — also seen as (RCA TAG : ...),
# {RCATag : ...}, and unclosed '{RCA TAG: ...' running to end of line. This tag is
# the closest thing the export has to a resolution code.
_RCA_TAG = re.compile(r"[\{\(\[]?\s*RCA\s*_?\s*TAG\s*[:\-]\s*([^\}\)\]\n]+)", re.I)

# 'BAN 314116701', 'BAN: 302302200', 'Ban:471154842', 'BAN - 1000344272', 'BAN-451670903'
_BAN = re.compile(r"\bBANS?\b\s*[:#\-]?\s*(\d{6,12})", re.I)
# 'Account #: 460143722', 'account number:2006366666'
_ACCOUNT_NO = re.compile(r"\baccount\s*(?:number|no|#)\s*[:\-]?\s*(\d{6,12})", re.I)
# short_description ends with ' - 314116701'
_TRAILING_BAN = re.compile(r"[-–]\s*(\d{6,12})\s*$")
# Order ids: two letters + 8-12 digits, occasionally with a trailing letter
# (WI2100002247, NC1100236685, MO1700000463T). The leading \b prevents matching
# inside 'SID0000002033'.
_ORDER_ID = re.compile(r"\b([A-Z]{2}\d{8,12}[A-Z]?)\b")
_TASK_ID = re.compile(r"\b(OMTASK\d+)\b")

# Journal boilerplate that carries no diagnostic signal — stripped so the stored
# work notes stay readable and the metadata stays small.
_BOILERPLATE = re.compile(
    r"(?im)^(?:"
    r".*auto closed in \d+ business days due to inactivity.*"
    r"|.*If your issue persists, please open a new incident.*"
    r"|\s*Assignment Rule[: ].*has been applied\s*"
    r"|\s*Record Producer:.*was used\.?\s*"
    r"|\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} - .*\((?:Work notes|Additional comments)\)\s*"
    r")$"
)


def _scrub_notes(text: str) -> str:
    """Drop ServiceNow journal boilerplate, keeping any real agent commentary."""
    if not text:
        return ""
    cleaned = _BOILERPLATE.sub("", text)
    return re.sub(r"\n{2,}", "\n", cleaned).strip()

_cache = {"path": None, "mtime": None, "tickets": None, "mapping": None}


# ── header normalisation ────────────────────────────────────────────────

def _norm(s) -> str:
    """Lowercase and strip everything that isn't a letter/digit/space, so
    'Ticket #', 'ticket_no.' and 'Ticket No' all compare equal-ish."""
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", _norm(s))


def _load_overrides() -> dict:
    if not os.path.exists(COLUMN_MAP_PATH):
        return {}
    try:
        with open(COLUMN_MAP_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, str) and v.strip()}
    except Exception as e:
        print(f"[KB-SOURCE] ignoring bad {COLUMN_MAP_PATH}: {e}")
        return {}


def _lifecycle_score(values) -> float:
    """Fraction of a column's non-empty values that read as ticket lifecycle states."""
    vals = [_norm(v) for v in values if str(v or "").strip()]
    if not vals:
        return 0.0
    hits = sum(1 for v in vals
               if v in LIFECYCLE_VOCAB or v.startswith(CLOSED_PREFIXES)
               or any(w in v for w in ("closed", "resolved", "open", "pending", "progress")))
    return hits / len(vals)


def _pick_state_column(headers: list, samples: dict, alias_choice: str | None) -> str | None:
    """Choose the ticket-state column by looking at its VALUES, not just its header.

    Guards against the common 'State' = Maryland/Georgia collision: the alias
    choice is kept only if its values actually look like lifecycle states,
    otherwise the best-scoring other column wins.
    """
    if not samples:
        return alias_choice
    if alias_choice and _lifecycle_score(samples.get(alias_choice, [])) >= 0.5:
        return alias_choice
    scored = sorted(((_lifecycle_score(v), h) for h, v in samples.items()),
                    key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 0.5:
        best = scored[0][1]
        if alias_choice and best != alias_choice:
            print(f"[KB-SOURCE] header '{alias_choice}' does not hold ticket states — "
                  f"using '{best}' for state instead.")
        return best
    return alias_choice


def resolve_columns(headers: list, samples: dict | None = None) -> dict:
    """Map canonical field -> actual header in this file (or None).

    `samples` maps header -> a sample of that column's values; when supplied it is
    used to sanity-check the state column against its real contents.
    """
    overrides = _load_overrides()
    by_squash = {_squash(h): h for h in headers}
    mapping, taken = {}, set()

    for field, aliases in COLUMN_ALIASES.items():
        chosen = None

        # 1. explicit override
        ov = overrides.get(field)
        if ov:
            chosen = by_squash.get(_squash(ov)) or (ov if ov in headers else None)
            if chosen is None:
                print(f"[KB-SOURCE] override '{field}' -> '{ov}' not found in the sheet.")

        # 2. exact alias match
        if chosen is None:
            for alias in aliases:
                hit = by_squash.get(_squash(alias))
                if hit and hit not in taken:
                    chosen = hit
                    break

        # 3. substring alias match (longest alias first — most specific wins)
        if chosen is None:
            for alias in sorted(aliases, key=len, reverse=True):
                a = _squash(alias)
                if len(a) < 3 or alias in EXACT_ONLY_ALIASES:
                    continue
                for h in headers:
                    if h in taken:
                        continue
                    if a in _squash(h):
                        chosen = h
                        break
                if chosen:
                    break

        if field == "state" and not overrides.get("state"):
            chosen = _pick_state_column(headers, samples, chosen)

        if chosen:
            taken.add(chosen)
        mapping[field] = chosen
    return mapping


# ── file discovery ──────────────────────────────────────────────────────

def _candidate_files() -> list:
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for name in os.listdir(DATA_DIR):
        if name.startswith("~$"):          # Excel lock file
            continue
        if name.lower().endswith((".xlsx", ".xls", ".csv")):
            out.append(os.path.join(DATA_DIR, name))
    # Newest first, but never pick our own audit/watchlist bookkeeping CSVs.
    skip = {"fallout_audit.csv", "watchlist.csv", "kb_column_map.json"}
    out = [p for p in out if os.path.basename(p) not in skip]
    return sorted(out, key=os.path.getmtime, reverse=True)


def resolve_path() -> str | None:
    """The spreadsheet backing the KB, or None if there isn't one yet."""
    if KB_EXCEL_PATH:
        p = KB_EXCEL_PATH if os.path.isabs(KB_EXCEL_PATH) else os.path.join(DATA_DIR, KB_EXCEL_PATH)
        return p if os.path.exists(p) else None
    files = _candidate_files()
    return files[0] if files else None


# ── row -> ticket ───────────────────────────────────────────────────────

def _cell(row, col) -> str:
    if not col:
        return ""
    v = row.get(col)
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none", "null") else s


def is_closed_state(state: str) -> bool:
    s = _norm(state)
    if not s:
        return False
    return s in CLOSED_STATES or s.startswith(CLOSED_PREFIXES)


def _row_to_ticket(row, mapping: dict, index: int) -> dict:
    g = lambda f: _cell(row, mapping.get(f))

    number = g("number") or f"KB-{index + 1:05d}"
    state = g("state")
    short_desc = g("short_description")
    description = g("description")
    root_cause = g("root_cause")
    resolution_notes = g("resolution_notes")

    # Root cause is genuinely useful problem-side context, but only when the sheet
    # keeps it in its own column — fold it into the description rather than losing it.
    if root_cause and root_cause not in description:
        description = f"{description}\nRoot cause: {root_cause}".strip()

    work_notes = _scrub_notes(g("work_notes"))

    # Identifiers the remediation engine keys off. Prefer a real column; otherwise
    # recover them from free text the same way the ServiceNow parser does.
    # Resolution notes lead the blob: when an id appears both in the customer's
    # complaint and in the agent's close notes, the close notes hold the corrected one.
    blob = "\n".join([resolution_notes, description, short_desc, work_notes])
    location_id = g("location_id") or _first(r"\b(LOC-\d+)\b", blob)
    tn = g("tn") or _first(r"(\(\d{3}\)\s*\d{3}-\d{4})", blob)
    service_id = g("referenced_service_id") or _first(r"\bSID\s*[:#]?\s*(\d{6,12})\b", blob)

    order_ref = g("order_ref") or _first(_ORDER_ID, blob) or _first(r"\b((?:CO|PR|DR)-\d+)\b", blob)
    task_ref = g("task_ref") or _first(_TASK_ID, blob)
    ban = (g("ban") or _first(_BAN, blob) or _first(_TRAILING_BAN, short_desc)
           or _first(_ACCOUNT_NO, blob))

    # The {RCA TAG : ...} written into the close notes is this export's resolution
    # code — the pipeline groups and reports on it, so pull it out explicitly.
    resolution_code = g("resolution_code") or _first(_RCA_TAG, resolution_notes) or _first(_RCA_TAG, blob)

    ticket = {
        "number": number,
        "sys_id": "",
        "state": state or "Closed",
        "is_closed": is_closed_state(state) if state else True,
        "short_description": short_desc,
        "subcategory": g("subcategory"),
        "category": g("category"),
        "location_id": location_id,
        "ban": ban,
        "tn": tn,
        "order_ref": order_ref,
        "order_type": g("order_type"),
        "task_ref": task_ref,
        "service_type": g("service_type"),
        "resolution_code": resolution_code.strip(" .{}()[]") if resolution_code else "",
        "referenced_service_id": service_id,
        "description": description,
        "work_notes": work_notes,
        "resolution_notes": resolution_notes,
        "updated": g("updated"),
        "source": "excel",
    }

    # Generic views the dynamic UI renders (mirrors servicenow_client.parse_ticket).
    ticket["fields"] = [{"label": _FIELD_LABELS[f], "value": ticket[f]}
                        for f in _FIELD_ORDER if ticket.get(f)]
    ticket["sections"] = [{"label": lbl, "text": txt} for lbl, txt in (
        ("Description", ticket["description"]),
        ("Work Notes", ticket["work_notes"]),
        ("Resolution Notes", ticket["resolution_notes"]),
    ) if txt]
    return ticket


def _first(pattern, text: str) -> str:
    """First capture group of `pattern` in `text`. Accepts a compiled pattern
    (which keeps its own flags) or a string (matched case-insensitively)."""
    m = (pattern.search(text or "") if hasattr(pattern, "search")
         else re.search(pattern, text or "", re.IGNORECASE))
    return m.group(1).strip() if m else ""


# ── public API ──────────────────────────────────────────────────────────

def load_all(force: bool = False) -> list:
    """Every row of the spreadsheet as a parsed ticket dict. Cached on file mtime,
    so editing the spreadsheet is picked up without restarting the app."""
    path = resolve_path()
    if not path:
        print(f"[KB-SOURCE] No KB spreadsheet found in {os.path.abspath(DATA_DIR)}. "
              f"Drop the incidents .xlsx there (or set KB_EXCEL_PATH).")
        return []

    mtime = os.path.getmtime(path)
    if not force and _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["tickets"]

    try:
        if path.lower().endswith(".csv"):
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(path, sheet_name=SHEET_NAME or 0, dtype=str)
    except Exception as e:
        print(f"[KB-SOURCE] failed to read {path}: {e}")
        return []

    df = df.fillna("")
    headers = [str(c) for c in df.columns]
    # Short per-column value sample so column resolution can check contents, not
    # just headers (see _pick_state_column).
    samples = {h: [str(v) for v in df[h].head(200).tolist()] for h in headers}
    mapping = resolve_columns(headers, samples)
    tickets = [_row_to_ticket(row, mapping, i)
               for i, row in enumerate(df.to_dict(orient="records"))]

    _cache.update({"path": path, "mtime": mtime, "tickets": tickets, "mapping": mapping})
    print(f"[KB-SOURCE] Loaded {len(tickets)} rows from {os.path.basename(path)} "
          f"({sum(1 for t in tickets if t['is_closed'])} closed).")
    return tickets


def fetch_closed_kb() -> list:
    """CLOSED tickets only — the historical knowledge base."""
    return [t for t in load_all() if t["is_closed"]]


def get_ticket(number: str) -> dict | None:
    if not number:
        return None
    n = str(number).strip().lower()
    for t in load_all():
        if str(t.get("number", "")).strip().lower() == n:
            return t
    return None


def describe() -> dict:
    """Diagnostics for /fallout/kb-source — which file, which columns, what counts."""
    tickets = load_all()
    path = _cache["path"]
    mapping = _cache["mapping"] or {}
    states = {}
    for t in tickets:
        states[t["state"]] = states.get(t["state"], 0) + 1
    return {
        "file": os.path.basename(path) if path else None,
        "path": os.path.abspath(path) if path else None,
        "sheet": SHEET_NAME or "(first sheet)",
        "total_rows": len(tickets),
        "closed_rows": sum(1 for t in tickets if t["is_closed"]),
        "mapped_columns": {k: v for k, v in mapping.items() if v},
        "unmapped_fields": sorted(k for k, v in mapping.items() if not v),
        "state_counts": dict(sorted(states.items(), key=lambda kv: -kv[1])),
    }


if __name__ == "__main__":
    info = describe()
    if not info["file"]:
        raise SystemExit(f"No spreadsheet found in {os.path.abspath(DATA_DIR)}")
    print(json.dumps(info, indent=2))
    closed = fetch_closed_kb()
    if closed:
        print("\nFirst closed ticket as the KB will see it:")
        print(json.dumps(closed[0], indent=2)[:2000])
