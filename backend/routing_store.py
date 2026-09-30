"""
Redirect / Reassignment Rules
-----------------------------
Some fallout types are not BOSS OM's to fix — a Buy Flow order-capture defect, for
example, belongs to the Buy Flow team. For those the correct remediation is not a
system check and a fix; it is a REDIRECT: recommend reassigning the incident to the
owning team.

Classification is DETERMINISTIC (regex over the ticket text), never LLM-routed,
because "every Buy Flow ticket goes to the Buy Flow team" is a hard business rule
and must not depend on retrieval quality or model judgement.

Rules live in data/routing_rules.json and are read fresh on every call so they can
be edited without restarting.
"""

import os
import re
import json

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "routing_rules.json")

_cache = {"mtime": None, "rules": []}


def _compile(rule: dict) -> dict:
    return {
        "name": rule.get("name", ""),
        "label": rule.get("label") or rule.get("name", ""),
        "assignment_group": rule.get("assignment_group", ""),
        "reason": rule.get("reason", ""),
        "instructions": [str(s) for s in (rule.get("instructions") or [])],
        "match_any": [re.compile(p, re.I) for p in (rule.get("match_any") or [])],
        "exclude_any": [re.compile(p, re.I) for p in (rule.get("exclude_any") or [])],
    }


def load_rules() -> list:
    """Compiled rules, recompiled only when the file changes."""
    try:
        mtime = os.path.getmtime(DATA_PATH)
    except OSError:
        return []
    if _cache["mtime"] == mtime:
        return _cache["rules"]
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = []
        for r in data.get("rules", []):
            try:
                rules.append(_compile(r))
            except re.error as e:
                print(f"[ROUTING] bad regex in rule {r.get('name', '?')}: {e}")
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ROUTING] could not read {DATA_PATH}: {e}")
        return _cache["rules"]
    _cache.update({"mtime": mtime, "rules": rules})
    return rules


def searchable_text(ticket: dict) -> str:
    """Everything on a ticket worth matching a redirect rule against.

    Includes every labelled field VALUE (not just the convenience keys), so a
    rule can fire on e.g. 'Business Service: BOSS AX Buyflow' without that label
    needing its own parser support.
    """
    parts = [
        ticket.get("short_description", ""),
        ticket.get("description", ""),
        ticket.get("subcategory", ""),
        ticket.get("category", ""),
        ticket.get("service_type", ""),
        ticket.get("work_notes", ""),
    ]
    for f in ticket.get("fields") or []:
        parts.append(f"{f.get('label', '')}: {f.get('value', '')}")
    for s in ticket.get("sections") or []:
        parts.append(s.get("text", ""))
    return "\n".join(p for p in parts if p)


def classify(ticket: dict) -> dict | None:
    """First matching redirect rule, with the evidence that matched, or None.

    Returns {name, label, assignment_group, reason, instructions, evidence}
    where `evidence` is the matched snippet — shown to the agent so the redirect
    is auditable rather than a black box.
    """
    text = searchable_text(ticket)
    if not text:
        return None
    for rule in load_rules():
        if any(p.search(text) for p in rule["exclude_any"]):
            continue
        for pattern in rule["match_any"]:
            m = pattern.search(text)
            if not m:
                continue
            return {
                "name": rule["name"],
                "label": rule["label"],
                "assignment_group": rule["assignment_group"],
                "reason": rule["reason"],
                "instructions": rule["instructions"],
                "evidence": _evidence(text, m),
                "matched": m.group(0),
            }
    return None


def _evidence(text: str, match) -> str:
    """The line the match occurred on, trimmed — enough for a human to verify."""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start:end if end != -1 else len(text)].strip()
    return line[:200] + ("…" if len(line) > 200 else "")


def describe() -> dict:
    """Diagnostics for /fallout/routing-rules."""
    return {
        "file": DATA_PATH,
        "rules": [{
            "name": r["name"], "label": r["label"],
            "assignment_group": r["assignment_group"],
            "patterns": [p.pattern for p in r["match_any"]],
            "excludes": [p.pattern for p in r["exclude_any"]],
        } for r in load_rules()],
    }
