"""
Fallout Remediation Engine
--------------------------
Resolution-driven pipeline (Option B):

    1. RETRIEVE  top-K similar CLOSED tickets from the KB (hybrid search).
    2. ROUTE     an LLM router reads HOW those similar tickets were resolved and the
                 available system-check tools, then picks WHICH tool to run and which
                 identifiers to pull from the current ticket.
    3. VALIDATE  the chosen tool runs DETERMINISTICALLY and returns a verdict
                 (confirmed / ambiguous / not_confirmed). If NO tool fits but the
                 historical grounding is strong, the verdict is 'not_applicable'
                 (a neutral state, not a failure).
    4. GENERATE  two separate outputs:
                   RESOLUTION  — history-only, combined from the top-3 similar tickets
                                 (each ticket's steps + a merged summary). Always produced.
                   REMEDIATION — validation-driven action steps, produced ONLY when a
                                 system tool actually ran.
    5. DECIDE    recommend vs needs_review.

The LLM ROUTES (picks the check) and writes prose (resolution + remediation). The
verdict and the pulled values are deterministic. Recommend-only: nothing is written
to ServiceNow here — approval posts a comment, and never closes the ticket.
"""

import os
import re
import json
from anthropic import Anthropic
from dotenv import load_dotenv

import fallout_store
import validation_tools
import servicenow_client
import kb_source
import routing_store
import prompts

load_dotenv()

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _chat(system: str, user: str, max_tokens: int, temperature: float) -> str:
    """One turn against Claude, returning the raw text reply.

    Every LLM call in this module has the same shape — one system prompt, one user
    prompt, a JSON reply — so they all route through here. Note that Anthropic takes
    `system` as a top-level argument rather than as a message in the list.
    """
    resp = _client.messages.create(
        model=MODEL, max_tokens=max_tokens, temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")

SIM_STRONG = 0.60   # retrieval confidence gate

# How many similar closed tickets to SHOW the agent. Retrieval itself still runs at
# k=5 — the extra hits feed the router and the resolution-code agreement check — so
# this trims the display only and does not weaken any decision.
TOP_N_SIMILAR = 3

# Out-of-context / unmatched: genuinely could not be validated -> human review.
_NO_CHECK = {"label": "Remediation", "status": "not_confirmed", "passed": False,
             "summary": "No applicable system check was identified for this ticket.",
             "details": [], "data": {}}

# Real fallout type with strong historical grounding but no wired system-check tool.
# Not a failure — the Resolution block (combined from similar tickets) stands on its own.
_NO_TOOL = {"label": "Remediation", "status": "not_applicable", "passed": False,
            "summary": "No automated system check applies to this fallout type. The "
                       "resolution below is based on the closest historical resolutions.",
            "details": [], "data": {}}

REDIRECT_TOOL = "routing.check_redirect_queue"


def _servicenow(number: str) -> dict | None:
    try:
        return servicenow_client.get_ticket(number)
    except Exception as e:
        print(f"[ENGINE] ServiceNow lookup for {number} failed: {e}")
        return None


def _lookup(number: str, prefer: str = "kb") -> dict | None:
    """Resolve a ticket number across both sources.

    Open tickets live in ServiceNow; historical/closed tickets live in the incidents
    spreadsheet, so a KB match returned by retrieval only ever resolves there.
    `prefer` decides which source is consulted first when a number could be in both.
    """
    if not number:
        return None
    order = (kb_source.get_ticket, _servicenow) if prefer == "kb" else (_servicenow, kb_source.get_ticket)
    for fn in order:
        hit = fn(number)
        if hit:
            return hit
    return None


def _parse_json(raw: str) -> dict:
    """Robustly extract a JSON object from an LLM response."""
    raw = (raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)


def _route_validation(ticket: dict, results: list) -> dict:
    """LLM router: pick which validation tool to run + identifiers, guided by how
    the similar tickets were resolved. Returns {tool, identifiers, rationale}.
    Only tool names in the registry are accepted (guardrail)."""
    tools = validation_tools.tool_schemas()
    user_prompt = prompts.build_router_user_prompt(ticket, results, tools)
    try:
        raw = _chat(prompts.ROUTER_SYSTEM_PROMPT, user_prompt,
                    max_tokens=300, temperature=0)
        data = _parse_json(raw)
        tool = data.get("tool")
        if tool not in validation_tools.TOOLS:
            tool = None
        ids = data.get("identifiers")
        return {"tool": tool, "identifiers": ids if isinstance(ids, dict) else {},
                "rationale": str(data.get("rationale", "")).strip()}
    except Exception as e:
        print(f"[ENGINE] router failed: {e}")
        return _route_by_subcategory(ticket)


# Deterministic routing fallback, used ONLY when the LLM router is unavailable.
# The subcategory already names the fallout type precisely, so a table lookup gets
# the right check most of the time. It exists so an LLM outage degrades the WORDING
# of a recommendation rather than removing the system check entirely — without it,
# every ticket reports "no applicable system check" and the product has nothing to say.
_SUBCATEGORY_TOOL = {
    "network type mismatch":               ("oms.check_network_type", ["ban"]),
    "service profile issue":               ("oms.check_network_type", ["ban"]),
    "account status mismatch":             ("oms.check_account_status", ["ban"]),
    "account type mismatch":               ("oms.check_account_status", ["ban"]),
    "staging stuck":                       ("oms.check_order_status", ["order_ref"]),
    "task closure":                        ("oms.check_order_status", ["order_ref"]),
    "order status sync issue":             ("oms.check_order_status", ["order_ref"]),
    "cancellation completion":             ("oms.check_order_status", ["order_ref"]),
    "duplicate service validation":        ("provisioning.check_active_service", ["location_id"]),
    "provisioning request - duplicate block": ("provisioning.check_active_service", ["location_id"]),
}


def _route_by_subcategory(ticket: dict) -> dict:
    sub = str(ticket.get("subcategory") or "").strip().lower()
    hit = _SUBCATEGORY_TOOL.get(sub)
    if not hit:
        return {"tool": None, "identifiers": {}, "rationale": ""}
    tool, fields = hit
    ids = {f: ticket.get(f) for f in fields if str(ticket.get(f) or "").strip()}
    if not ids:
        return {"tool": None, "identifiers": {}, "rationale": ""}
    print(f"[ENGINE] routed deterministically on subcategory {sub!r} -> {tool}")
    return {"tool": tool, "identifiers": ids,
            "rationale": f"Selected from the ticket's subcategory ('{ticket.get('subcategory')}') "
                         f"because the model router was unavailable."}


def _generate_remediation(ticket: dict, validation: dict, results: list) -> dict:
    """AI recommendation body: {summary, steps[]}, grounded in the retrieved
    historical resolutions + the validation verdict."""
    user_prompt = prompts.build_remediation_user_prompt(ticket, validation, results)
    try:
        raw = _chat(prompts.REMEDIATION_SYSTEM_PROMPT, user_prompt,
                    max_tokens=900, temperature=0.2)
        data = _parse_json(raw)
        return {"summary": str(data.get("summary", "")).strip(),
                "steps": [str(s).strip() for s in (data.get("steps") or [])]}
    except Exception as e:
        print(f"[ENGINE] remediation generation failed: {e}")
        return {"summary": "", "steps": []}



# Resolution notes are often written as "1. did this 2. then this". Recover those as
# discrete steps so a recorded resolution can be shown without an LLM rewriting it.
_STEP_SPLIT = re.compile(r"(?:(?<=^)|(?<=\s))\d{1,2}\.\s+")
_NOTE_TAIL = re.compile(r"\{\s*RCA\s*TAG.*$|(?:BAN|Order ID)\s*[:#]?\s*\S+\s*$",
                        re.I | re.S)


def _steps_from_notes(notes: str) -> list:
    """Split a recorded resolution into steps, dropping the RCA tag / id trailer."""
    text = " ".join((notes or "").split())
    if not text:
        return []
    text = _NOTE_TAIL.sub("", text).strip()
    parts = [p.strip(" .") for p in _STEP_SPLIT.split(text) if p.strip(" .")]
    if len(parts) > 1:
        return parts
    return [text] if text else []


def _generate_resolution(ticket: dict, results: list) -> dict:
    """History-only 'Resolution' block. For each of the top-3 similar resolved
    tickets it lays out that ticket's own resolution steps, then a merged summary
    that reconciles them for the current ticket. Needs NO validation tool.
    Returns {per_ticket: [{number, similarity, resolution_code, steps[]}], merged_summary}."""
    top = results[:3]

    def _skeleton():
        """Fall back to the historical ticket's OWN recorded resolution.

        The LLM normally rewrites each match's resolution into steps for the current
        ticket. When it is unavailable, quoting what the agent actually wrote is far
        better than showing nothing — and it is still grounded, not invented.
        """
        return [{"number": r["metadata"].get("number", ""),
                 "similarity": r["similarity"],
                 "resolution_code": r["metadata"].get("resolution_code", ""),
                 "steps": _steps_from_notes(r["metadata"].get("resolution_notes", "")),
                 "verbatim": True} for r in top]

    if not top:
        return {"per_ticket": [], "merged_summary": ""}

    user_prompt = prompts.build_resolution_user_prompt(ticket, top)
    try:
        raw = _chat(prompts.RESOLUTION_SYSTEM_PROMPT, user_prompt,
                    max_tokens=1200, temperature=0.2)
        data = _parse_json(raw)
        steps_by_num = {}
        for pt in (data.get("per_ticket") or []):
            num = str(pt.get("number", "")).strip()
            steps_by_num[num] = [str(s).strip() for s in (pt.get("steps") or []) if str(s).strip()]
        # Enrich with real similarity/code from metadata (never trust the LLM for those).
        per_ticket = []
        for r in top:
            meta = r["metadata"]
            num = meta.get("number", "")
            per_ticket.append({"number": num, "similarity": r["similarity"],
                               "resolution_code": meta.get("resolution_code", ""),
                               "steps": steps_by_num.get(num, [])})
        return {"per_ticket": per_ticket,
                "merged_summary": str(data.get("merged_summary", "")).strip()}
    except Exception as e:
        print(f"[ENGINE] resolution generation failed: {e}")
        return {"per_ticket": _skeleton(), "merged_summary": ""}


def generate_breakdown(hist_number: str, against: str = None) -> dict:
    """AI breakdown of ONE resolved historical ticket (symptom_correlation +
    root_cause + numbered resolution steps), matched against the open ticket."""
    hist = _lookup(hist_number)
    if not hist:
        return {"error": f"Ticket {hist_number} not found"}
    open_ticket = _lookup(against, prefer="servicenow") if against else {}
    user_prompt = prompts.build_breakdown_user_prompt(hist, open_ticket or {})
    try:
        raw = _chat(prompts.BREAKDOWN_SYSTEM_PROMPT, user_prompt,
                    max_tokens=800, temperature=0.2)
        data = _parse_json(raw)
        return {"number": hist_number,
                "symptom_correlation": str(data.get("symptom_correlation", "")).strip(),
                "root_cause": str(data.get("root_cause", "")).strip(),
                "steps": [str(s).strip() for s in (data.get("steps") or [])]}
    except Exception as e:
        print(f"[ENGINE] breakdown generation failed: {e}")
        return _breakdown_from_record(hist, open_ticket or {})


def _breakdown_from_record(hist: dict, open_ticket: dict) -> dict:
    """Build the breakdown from the historical record itself, with no model.

    Every section the UI shows has a factual source on the ticket: the two problem
    statements give the symptom correlation, the RCA tag IS the categorised root
    cause, and the recorded close notes give the steps. So when the model is
    unavailable this reports what the record actually says instead of an error —
    less insightful than a tailored analysis, but true, and it never invents a
    cause that was not recorded.
    """
    def _clean(x):
        return " ".join(str(x or "").split())

    h_sub, o_sub = _clean(hist.get("subcategory")), _clean(open_ticket.get("subcategory"))
    h_sd, o_sd = _clean(hist.get("short_description")), _clean(open_ticket.get("short_description"))

    sym = []
    if o_sd:
        if h_sub and o_sub and h_sub.lower() == o_sub.lower():
            sym.append(f"Both incidents are classified as '{h_sub}' fallout.")
        elif h_sub:
            sym.append(f"This resolved incident was classified as '{h_sub}'"
                       + (f", the current one as '{o_sub}'." if o_sub else "."))
        sym.append(f"Reported here: {h_sd}")
        sym.append(f"Reported on the current ticket: {o_sd}")
    else:
        sym.append(f"Reported here: {h_sd}" if h_sd else "No problem statement recorded.")

    code = _clean(hist.get("resolution_code"))
    steps = _steps_from_notes(hist.get("resolution_notes", ""))
    cause = (f"Closed under RCA tag '{code}'." if code
             else "No RCA tag was recorded on this incident.")
    if steps:
        cause += f" The recorded finding was: {steps[0]}."

    return {"number": hist.get("number", ""),
            "symptom_correlation": " ".join(sym),
            "root_cause": cause,
            "steps": steps,
            "source": "record"}


def _candidates(results: list) -> list:
    """The similar closed tickets shown to the agent, best match first."""
    return [{
        "number": r["metadata"]["number"],
        "similarity": r["similarity"],
        "resolution_code": r["metadata"].get("resolution_code", ""),
        "location_id": r["metadata"].get("location_id", ""),
        "resolution": r["metadata"].get("resolution_notes", ""),
    } for r in results[:TOP_N_SIMILAR]]


def _redirect_recommendation(base: dict, ticket: dict, redirect: dict, results: list) -> dict:
    """Build the recommendation for a ticket owned by another team.

    The action here is REASSIGNMENT, not remediation, so the steps are produced
    deterministically from the matched rule rather than by the LLM — there is
    nothing to reason about once the rule has fired, and a fixed action keeps the
    posted comment identical for every ticket of this type.

    Recommend-only, as everywhere else in this app: approving posts a comment
    stating the redirect. It does NOT change assignment_group or state.
    """
    group = redirect["assignment_group"]
    validation = validation_tools.run_tool(REDIRECT_TOOL, {"_ticket": ticket})

    steps = [f"Reassign incident {ticket.get('number', '')} to '{group}'."]
    steps += redirect.get("instructions") or []

    base.update({
        "decision": "recommend",
        "action": "redirect",
        "confidence": "High",
        "reason": redirect.get("reason") or f"Ticket is owned by {group}.",
        "validation": validation,
        "routed_tool": REDIRECT_TOOL,
        "route_rationale": (f"Matched the '{redirect['label']}' redirect rule on: "
                            f"{redirect.get('evidence', '')}"),
        "resolution_code": f"Reassigned - {redirect['label']}",
        "remediation_summary": (f"This is a {redirect['label']} ticket. It is owned by "
                                f"'{group}' and should be reassigned rather than remediated here."),
        "remediation_steps": steps,
        # Historical context is still shown, but it is NOT the basis for the action —
        # the redirect rule is. Retrieval here is for the agent's situational awareness.
        "resolution": {"per_ticket": [], "merged_summary": ""},
        "redirect": {"assignment_group": group, "rule": redirect["name"],
                     "label": redirect["label"], "evidence": redirect.get("evidence", "")},
        "best_match": ({"number": results[0]["metadata"]["number"],
                        "similarity": results[0]["similarity"]} if results else {}),
        "candidates": _candidates(results),
    })
    base["comment_preview"] = format_comment(base)
    return base


def recommend(number: str) -> dict:
    """Produce a recommendation for an open fallout ticket by INC number."""
    ticket = _lookup(number, prefer="servicenow")
    if not ticket:
        return {"error": f"Ticket {number} not found"}

    base = {"ticket": ticket}

    # ── REDIRECT pre-route (deterministic, runs before retrieval) ──
    # "Every Buy Flow ticket goes to the Buy Flow team" is a business rule, not a
    # judgement call, so it must not depend on retrieval quality or the router LLM.
    # A redirect short-circuits remediation entirely: this queue does not fix these,
    # it hands them over.
    #
    # This is deliberately evaluated BEFORE the KB search. Retrieval on a redirect
    # is only for the agent's situational awareness, so an empty KB or an embedding
    # outage must not stop a ticket being handed to its owning team.
    redirect = routing_store.classify(ticket)
    if redirect and redirect.get("assignment_group"):
        try:
            results = fallout_store.search(fallout_store.build_document(ticket), k=5)
        except Exception as e:
            print(f"[FALLOUT] retrieval unavailable for redirect {number}: {e}")
            results = []
        return _redirect_recommendation(base, ticket, redirect, results)

    query_text = fallout_store.build_document(ticket)
    results = fallout_store.search(query_text, k=5)

    if not results:
        base.update({"decision": "needs_review", "confidence": "Low",
                     "reason": "No historical match found in the knowledge base.",
                     "validation": dict(_NO_CHECK), "routed_tool": None, "route_rationale": "",
                     "resolution_code": ticket.get("resolution_code", "") or "Pending",
                     "remediation_summary": "", "remediation_steps": [],
                     "resolution": {"per_ticket": [], "merged_summary": ""},
                     "best_match": {}, "candidates": []})
        base["comment_preview"] = format_comment(base)
        return base

    best = results[0]
    best_sim = best["similarity"]

    # ── resolution-driven routing → deterministic verdict ────────────
    route = _route_validation(ticket, results)
    tool = route["tool"]
    if tool:
        validation = validation_tools.run_tool(tool, route["identifiers"])
    elif best_sim >= SIM_STRONG:
        # Real fallout type, strong historical grounding, but no wired tool.
        # Not a failure — the combined Resolution stands on its own.
        validation = dict(_NO_TOOL)
    else:
        # No tool AND weak retrieval → likely out of context → human review.
        validation = dict(_NO_CHECK)

    status = validation.get("status", "not_confirmed")
    codes = {r["metadata"].get("resolution_code", "") for r in results if r["similarity"] >= SIM_STRONG}
    agree = len(codes) <= 1

    if status == "confirmed" and best_sim >= SIM_STRONG:
        decision, confidence = "recommend", ("High" if agree else "Medium")
        reason = ("Historical matches agree and the system check confirms the condition."
                  if agree else
                  "The system check confirms the condition; historical matches were mixed, "
                  "recommending the validated resolution.")
    elif status == "confirmed" and best_sim < SIM_STRONG:
        decision, confidence = "recommend", "Medium"
        reason = "The system check confirms the condition, but retrieval similarity is weak."
    elif status == "not_applicable":
        decision, confidence = "recommend", ("High" if agree else "Medium")
        reason = ("No automated system check applies to this fallout type; the resolution is "
                  "grounded in the closest historical resolutions."
                  if agree else
                  "No automated system check applies to this fallout type; the closest historical "
                  "resolutions differ (different underlying causes), so review the combined "
                  "resolution before acting.")
    elif status == "ambiguous":
        decision, confidence = "needs_review", "Medium"
        reason = validation.get("summary", "") + " Human review required."
    else:
        decision, confidence = "needs_review", "Low"
        reason = validation.get("summary", "") + " The condition could not be validated automatically."

    if status in ("confirmed", "not_applicable"):
        resolution_code = best["metadata"].get("resolution_code") or "Resolved"
    elif status == "ambiguous":
        resolution_code = "Manual Review"
    else:
        resolution_code = ticket.get("resolution_code", "") or "Pending"

    # Remediation (validation-driven actions) only when a system tool actually ran.
    remediation = _generate_remediation(ticket, validation, results) if tool else {"summary": "", "steps": []}
    # Resolution (combined from the top-3 similar tickets) — always available.
    resolution = _generate_resolution(ticket, results)

    base.update({
        "decision": decision,
        "action": "comment",
        "confidence": confidence,
        "reason": reason,
        "validation": validation,
        "routed_tool": tool,
        "route_rationale": route["rationale"],
        "resolution_code": resolution_code,
        "remediation_summary": remediation["summary"],
        "remediation_steps": remediation["steps"],
        "resolution": resolution,
        "best_match": {"number": best["metadata"]["number"], "similarity": best_sim},
        "candidates": _candidates(results),
    })
    base["comment_preview"] = format_comment(base)
    return base


# How each age band should read to an agent triaging a queue. Age is the only
# priority signal these incidents carry — native ServiceNow priority fields are not
# populated — so it is stated on every comment rather than left for the reader to
# work out from the opened date.
_AGE_NOTE = {
    "fresh":    "raised recently",
    "aging":    "ageing - prioritise ahead of newer fallout",
    "stale":    "OVERDUE - this has been open over a week",
    "critical": "SEVERELY OVERDUE - open more than two weeks, escalate",
}


def _age_line(ticket: dict) -> list:
    """One line stating how long the incident has been open, or nothing."""
    label = (ticket or {}).get("age_label")
    if not label:
        return []
    band = (ticket or {}).get("age_band", "fresh")
    opened = (ticket or {}).get("opened", "")
    note = _AGE_NOTE.get(band, "")
    line = f"Ticket age: {label} open"
    if opened:
        line += f" (raised {opened})"
    if note:
        line += f" - {note}"
    return [line + "."]


def format_comment(rec: dict) -> str:
    """Render the recommendation as the comment posted to ServiceNow on approval.

    Two clearly separated sections:
      RESOLUTION  — combined from the top similar resolved tickets (history-driven).
      REMEDIATION — the system-state validation + any validated actions (tool-driven),
                    or a neutral note when no system check applies to this fallout type.
    """
    best = rec.get("best_match", {})
    val = rec.get("validation", {})
    res = rec.get("resolution", {}) or {}

    # ── REDIRECT: a different comment entirely. The action is a handover, so the
    # comment states the owning team and the evidence, and nothing else. ──
    if rec.get("action") == "redirect":
        rd = rec.get("redirect", {})
        lines = [
            "[Remediation - Reassignment Recommended]",
            "",
            f"This is a {rd.get('label', '')} ticket and is owned by "
            f"'{rd.get('assignment_group', '')}', not this queue.",
            "",
            f"Recommended action: reassign to {rd.get('assignment_group', '')}.",
            f"Reason: {rec.get('reason', '')}",
        ]
        lines += _age_line(rec.get("ticket", {}))
        if rd.get("evidence"):
            lines.append(f"Matched on: {rd['evidence']}")
        steps = rec.get("remediation_steps", [])
        if steps:
            lines += ["", "Steps:"] + [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
        lines += ["", f"Ownership is being transferred to {rd.get('assignment_group', '')}. "
                      "The incident state is unchanged — it has not been closed or resolved."]
        return "\n".join(lines)

    header = ("[Remediation Recommendation]" if rec.get("decision") == "recommend"
              else "[Remediation - Human Review Required]")

    lines = [header, "", f"Recommended Resolution Code: {rec.get('resolution_code', '')}"]
    lines += _age_line(rec.get("ticket", {}))
    lines += [""]

    # ── RESOLUTION (from similar resolved tickets) ──
    lines.append("RESOLUTION (based on similar resolved tickets)")
    if res.get("merged_summary"):
        lines += [f"Combined guidance: {res['merged_summary']}", ""]
    per = res.get("per_ticket", [])
    if per:
        for pt in per:
            sim = int(round((pt.get("similarity") or 0) * 100))
            code = pt.get("resolution_code") or ""
            hdr = f"From {pt.get('number', 'n/a')} ({sim}% similar)"
            hdr += f" - {code}" if code else ""
            # `verbatim` means these are the historical agent's own recorded steps,
            # quoted because the model was unavailable to adapt them. Say so rather
            # than presenting them as advice tailored to THIS ticket.
            if pt.get("verbatim"):
                hdr += " - recorded resolution"
            lines.append(hdr + ":")
            steps = pt.get("steps", [])
            lines += ([f"  {i}. {s}" for i, s in enumerate(steps, 1)] if steps
                      else ["  (no resolution was recorded on this ticket)"])
            lines.append("")
    else:
        lines += ["(no similar resolved tickets found)", ""]

    # ── REMEDIATION (system validation) ──
    lines.append("REMEDIATION (system validation)")
    if val.get("status") == "not_applicable":
        lines.append("No automated system check applies to this fallout type; the resolution "
                     "above is based on the closest historical resolutions.")
    else:
        detail_str = "; ".join(f"{d['label']}: {d['value']}" for d in val.get("details", [])) or val.get("summary", "")
        lines.append(f"Validation ({val.get('label', 'Remediation')} via {rec.get('routed_tool') or 'no tool'}): {detail_str}.")
    rem_steps = rec.get("remediation_steps", [])
    if rem_steps:
        lines.append("Recommended actions:")
        lines += [f"  {i}. {s}" for i, s in enumerate(rem_steps, 1)]

    # Always end by stating plainly what happens next. This app cannot execute any
    # of the actions above — it only recommends — so a comment that stops after the
    # findings can read as though the fallout has been dealt with. It has not.
    lines += ["", "ACTION REQUIRED"]
    if rem_steps:
        lines.append("A human agent must carry out the recommended actions above and "
                     "update the account in BOSS OM. This tool does not apply changes; "
                     "the incident stays open until an agent actions it.")
    elif val.get("status") == "not_applicable":
        lines.append("No automated check covers this fallout type, so no correction has "
                     "been identified automatically. A human agent must review this "
                     "incident and determine the action, using the historical "
                     "resolutions above as a starting point.")
    else:
        lines.append("A human agent must review this incident and action it. This tool "
                     "recommends only — nothing has been changed on the account.")

    lines += ["", f"Best historical match: {best.get('number', 'n/a')} ({int(best.get('similarity', 0)*100)}% similar)."]
    return "\n".join(lines)


# ── free-text search ("Ticket Genie") ────────────────────────────────────────
#
# The queue path answers "what should I do about INC0010018?". This answers
# "has anyone fixed something like this before?", the input is a typed
# description, not a ticket.
#
# What deliberately does NOT run here:
#   * the redirect pre-route and the validation tools, because both need a real
#     ticket. The tools key on a BAN / Location ID / order id, and a typed
#     sentence has none, so every verdict would be 'not_applicable', a card
#     saying nothing. Search returns history only.
#   * anything that writes. There is no incident to comment on.
#
# Relevance bands. `search()` always returns k hits ranked by similarity, even
# when nothing is relevant, so an unfiltered "top 3" answers "what is the capital
# of France" with three order-fallout tickets and invents steps for them. These
# thresholds were set by measuring the real KB rather than guessed: genuine
# in-domain queries land at 0.60-0.78, a weak but real one ("duplicate service at
# the location") at 0.37, an unrelated one ("how to fix modem") at 0.35, and
# nonsense at 0.10-0.23. Out-of-domain and weak-but-real therefore OVERLAP, and no
# single cut separates them, hence three bands and an honest label on each result,
# instead of one threshold pretending to be precise.
# Re-measured against the 18-ticket customer support KB, twice.
#
# The first pass used 22 fairly specific phrasings ("wifi is connected but there is
# no internet") and suggested a floor of 0.45. That was wrong, and the way it was
# wrong is worth recording: SHORT, GENERIC phrasings are exactly what customers
# actually type, and they score much lower against long specific documents. "my
# internet is not working" scores 0.467, "i have no internet" 0.372 and "my line is
# dead" 0.361, so a 0.45 floor refused real customers while the calibration set
# looked healthy.
#
# The second pass measured 15 short generic customer phrasings against 10 off-topic
# ones (billing, cancellation, opening hours, nonsense). At 0.36 every on-topic query
# is accepted and every off-topic one is refused. The nearest false positive, "can I
# cancel my service", sits at 0.349, so the margin is real but thin: roughly 0.012.
# Raising the floor above 0.38 starts refusing genuine problems again.
# Recalibrated a third time, against the live ServiceNow-backed KB rather than the
# spreadsheet, because the parsed text differs slightly and the scores moved with it.
# The decisive case: "I want to cancel my service" scores 0.361 here, so a 0.36 floor
# answered a cancellation request with modem troubleshooting steps. Measured across 15
# on-topic and 14 off-topic phrasings, any floor from 0.38 to 0.46 gives zero false
# accepts. 0.40 is chosen for margin: 0.04 above the highest off-topic score and 0.06
# below the lowest on-topic one it accepts.
#
# The cost is that two very terse phrasings, "my line is dead" (0.354) and "i have no
# internet" (0.374), fall below the floor and get the honest "I could not find
# anything" plus a ticket offer. That is a safe answer; troubleshooting a cancellation
# request is not.
SEARCH_MIN_SIM = 0.40      # below this: no usable match, and the LLM is not called
SEARCH_PARTIAL_SIM = 0.55  # 0.36-0.55 reads as 'weak', 0.55-0.60 as 'partial'
SEARCH_TOP_N = 3           # how many matches to return (retrieval still runs at k=5)

# A query that is just an incident number is a request for the FULL pipeline on
# that ticket, not a history search, the UI has one input for both.
_TICKET_NUMBER = re.compile(r"^\s*((?:INC|inc)[0-9]{4,})\s*$")


def looks_like_ticket_number(query: str) -> str:
    """The incident number if the query is one, else ''."""
    m = _TICKET_NUMBER.match(query or "")
    return m.group(1).upper() if m else ""


def _strength(sim: float) -> str:
    if sim >= SIM_STRONG:
        return "strong"
    if sim >= SEARCH_PARTIAL_SIM:
        return "partial"
    return "weak"


# ── customer conversation ("Ticket Genie" chat) ──────────────────────────────
#
# A conversation, not a search box. The customer describes a problem in their own
# words, gets steps to try, and is offered a ticket or a rep when the steps run out.
#
# The CONTROL FLOW here is deterministic and the model only writes prose, which is
# the same split the rest of this app uses. Where there are buttons, intent comes from
# the button rather than from model interpretation, so the two can never disagree.
#
# The exception is the reply to troubleshooting steps. Those buttons were removed on
# the owner's instruction, so that reply has to be READ: _followup_intent() tries an
# unambiguous keyword pass first and only consults the model for genuinely ambiguous
# wording. It is biased towards 'unresolved', because continuing to help someone who
# is already fixed is a small annoyance, while closing the conversation on someone who
# is still broken is not. Any other free text is treated as describing a problem.
#
# Stages the client renders:
#   troubleshoot   steps were found, ask whether they worked
#   no_match       nothing close enough in the KB, offer a ticket or a rep
#   escalate       the customer is stuck or needs our side to act
#   resolved       the customer confirmed it is fixed, conversation closed
#   ticket_review  the completed ticket, shown for approval before creation
#   rep            hand off to a human rep
#   ticket_lookup  an incident number was typed, return the full agent pipeline

CHAT_ACTIONS = ("worked", "stuck", "open_ticket", "talk_to_rep", "follow_up",
                "ticket_details", "confirm_ticket", "cancel_ticket")

# Offered after an escalation. Deterministic per stage, so the buttons and the stage
# can never disagree. There are deliberately NO buttons after troubleshooting steps:
# the customer's reply is read instead (see the follow_up action).
_QR_ESCALATE = [{"label": "Open a ticket for me", "action": "open_ticket"},
                {"label": "Talk to a representative", "action": "talk_to_rep"}]


def _turn(stage, reply, steps=None, quick_replies=None, **extra) -> dict:
    out = {"stage": stage, "reply": reply, "steps": steps or [],
           "quick_replies": quick_replies or [], "needs_engineer": False,
           "sources_count": 0, "match_strength": "none", "sources": [],
           "resolutions": [], "closing_question": "", "provider_steps": [],
           "customer_header": "", "provider_header": "", "no_customer_steps_note": ""}
    out.update(extra)
    return out


def _last_user_message(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user" and str(m.get("content", "")).strip():
            return str(m["content"]).strip()
    return ""


def _retrieval_query(messages: list) -> str:
    """What to search on.

    A follow-up is often too short to retrieve on by itself ("still broken"), so a
    short latest message is combined with the first thing the customer said, which
    is where the actual problem description lives.
    """
    last = _last_user_message(messages)
    firsts = [str(m.get("content", "")).strip() for m in (messages or [])
              if m.get("role") == "user" and str(m.get("content", "")).strip()]
    first = firsts[0] if firsts else ""
    if first and first != last and len(last.split()) < 6:
        return f"{first} {last}"
    return last

def _overall_steps(groups: list) -> list:
    """One ordered list of recommended steps, merged from the per-ticket steps.

    This runs on the ALREADY TRANSLATED per-ticket steps, never on the raw tickets, so
    the isolation guarantee survives: nothing can appear here that did not come from a
    real recorded resolution, and each step keeps the ticket numbers it came from.

    If the merge fails, the per-ticket steps are still shown in the dropdown, so the
    customer is never left with nothing.
    """
    if not groups:
        return []
    try:
        raw = _chat(prompts.OVERALL_STEPS_SYSTEM_PROMPT,
                    prompts.build_overall_steps_user_prompt(groups),
                    max_tokens=700, temperature=0)
        data = _parse_json(raw)
    except Exception as e:
        print(f"[CHAT] consolidated step generation failed: {e}")
        return []

    valid = {g["number"] for g in groups}
    out = []
    for item in (data.get("steps") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        # Only citations pointing at tickets we actually retrieved are kept, so a
        # fabricated reference cannot reach the screen.
        cites = [c for c in (item.get("from") or []) if str(c).strip() in valid]
        out.append({"text": text,
                    "needs_engineer": bool(item.get("needs_engineer")),
                    "from": cites})
    return out[:6]


def chat_turn(messages: list, action: str = "", tried_steps: list = None,
              suggested_steps: list = None, known_fields: dict = None) -> dict:
    """One assistant turn. Stateless: the client sends the whole conversation.

    Read-only. Nothing in this path writes to ServiceNow.
    """
    messages = messages or []
    action = (action or "").strip()

    if action == "worked":
        return _turn("resolved",
                     "Good, glad that sorted it. If it comes back, just start a new message "
                     "and we will pick it up again.")

    if action == "talk_to_rep":
        return _turn("rep",
                     "No problem. I am putting you through to someone now. Please hold.",
                     handoff=True)

    if action in ("open_ticket", "ticket_details", "confirm_ticket", "cancel_ticket"):
        return _ticket_flow(messages, action, tried_steps, suggested_steps,
                            known_fields)

    if action == "follow_up":
        # The "That fixed it" / "I am stuck" buttons were removed, so the customer's
        # own words decide what happens next: close the conversation, or move towards
        # raising a ticket.
        msg = _last_user_message(messages)
        intent = _followup_intent(msg, _last_assistant_steps(messages))
        if intent == "resolved":
            return _turn("resolved",
                         "Good, glad that sorted it. If it comes back, just start a new "
                         "message and we will pick it up again.")
        if intent == "wants_ticket":
            # They asked for a ticket outright, so do not offer one again.
            return _ticket_flow(messages, "open_ticket", tried_steps,
                                suggested_steps, known_fields)
        return _turn("escalate",
                     "If it's still not working, let us raise a ticket for you.",
                     quick_replies=_QR_ESCALATE, needs_engineer=True)

    if action == "stuck":
        return _turn("escalate",
                     "If it's still not working, let us raise a ticket for you.",
                     quick_replies=_QR_ESCALATE, needs_engineer=True)

    question = _last_user_message(messages)
    if not question:
        return _turn("no_match", "Tell me what is going wrong and I will see what I can find.")

    # An incident number is an internal power path, not something a customer types.
    # It returns the full agent pipeline so the comment and reassign flows stay
    # reachable from the one input.
    number = looks_like_ticket_number(question)
    if number:
        rec = recommend(number)
        rec["stage"] = "ticket_lookup"
        rec["mode"] = "ticket"
        return rec

    try:
        results = fallout_store.search(_retrieval_query(messages), k=5)
    except Exception as e:
        print(f"[CHAT] retrieval failed: {e}")
        results = []

    usable = [r for r in results if r["similarity"] >= SEARCH_MIN_SIM][:SEARCH_TOP_N]
    if not usable:
        return _turn("no_match",
                     "I could not find a close enough match in our records to suggest a "
                     "fix, and I do not want to guess. I can raise a ticket for you, or put "
                     "you through to someone who can help.",
                     quick_replies=_QR_ESCALATE,
                     top_similarity=results[0]["similarity"] if results else 0.0)

    groups = _per_ticket_resolutions(usable)
    top_sim = usable[0]["similarity"]

    if groups:
        # Every word around the steps is written here rather than by the model, so a
        # reply cannot acquire an apology, sympathy or any other filler.
        overall = _overall_steps(groups)
        customer_steps = [s for s in overall if not s["needs_engineer"]]
        provider_steps = [s for s in overall if s["needs_engineer"]]
        return _turn("troubleshoot", RESOLUTION_HEADER,
                     steps=customer_steps,
                     provider_steps=provider_steps,
                     customer_header=CUSTOMER_STEPS_HEADER,
                     provider_header=PROVIDER_STEPS_HEADER,
                     no_customer_steps_note=("" if customer_steps else NO_CUSTOMER_STEPS_NOTE),
                     resolutions=groups,
                     closing_question=RESOLUTION_QUESTION,
                     sources_count=len(groups),
                     match_strength=_strength(top_sim),
                     top_similarity=top_sim)

    # Tickets matched but none of them yielded usable steps.
    return _turn("escalate",
                 "I found similar cases, but none of them has a fix recorded that I can pass "
                 "on. I can raise a ticket for you, or put you through to someone who can help.",
                 quick_replies=_QR_ESCALATE, needs_engineer=True,
                 sources=_source_tickets(usable),
                 sources_count=len(usable), match_strength=_strength(top_sim),
                 top_similarity=top_sim)


# ── ticket creation flow ──────────────────────────────────────────────────────
#
# Collect, validate, review, confirm. The product owner's requirements, which the
# code below follows literally:
#   * the customer can give every detail in ONE message
#   * the system verifies whether everything required is present
#   * anything missing is named EXPLICITLY, field by field, and asked for again
#   * nothing is ever assumed or auto-populated
#   * the completed ticket is shown back for review BEFORE anything is created
#   * the incident is created only once every required field is present and validated
#
# PROVISIONAL FIELD LIST. The owner asked for the required fields to be read from
# the ServiceNow instance itself. That instance is being replaced, so this list is
# the working set until it can be read. Run backend/discover_required_fields.py
# against the new instance and update this one list; nothing else needs to change.
#
# `sn_field` is where the value lands on the incident. Fields with sn_field None are
# still written, inside the labelled description block, because this app parses its
# structured data out of labelled text anyway (see servicenow_client._parse_block),
# so a ticket created here stays machine readable by the same app later.

REQUIRED_TICKET_FIELDS = [
    {"key": "short_description", "label": "Short description",
     "hint": "for example, modem has a red light and no internet",
     "sn_field": "short_description"},
    {"key": "full_name", "label": "Your full name", "hint": None, "sn_field": None},
    {"key": "account_number", "label": "Your account number", "hint": None, "sn_field": None},
    {"key": "contact_phone", "label": "A contact phone number", "hint": None, "sn_field": None},
    {"key": "contact_email", "label": "A contact email address", "hint": None, "sn_field": None},
    {"key": "service_affected", "label": "Which service is affected",
     "hint": "internet, phone, or both", "sn_field": None},
]

_FIELD_BY_KEY = {f["key"]: f for f in REQUIRED_TICKET_FIELDS}

# Values that mean "not provided" rather than being an answer.
_NULLISH = {"", "n/a", "na", "none", "null", "-", "unknown", "dont know", "don't know",
            "not sure", "no idea", "tbd"}


def _user_messages(messages: list) -> list:
    return [str(m.get("content", "")).strip() for m in (messages or [])
            if m.get("role") == "user" and str(m.get("content", "")).strip()]


def _clean_value(v) -> str:
    s = " ".join(str(v or "").split())
    return "" if s.lower() in _NULLISH else s


def _extract_labelled(messages: list) -> dict:
    """Pull out values the customer labelled themselves ("Name: Jane Smith").

    Deterministic and therefore available even when the model is not. It can only
    ever read a label the customer typed, so it cannot invent a value, which is the
    property that matters here. Free-form messages fall through to the model.
    """
    found = {}   # key -> {"value", "index"}, index being the user message it came from
    # Match a field by its key, or by the significant words of its label, so both
    # "account_number:" and "Account number:" are understood.
    aliases = {}
    for f in REQUIRED_TICKET_FIELDS:
        keys = {f["key"], f["key"].replace("_", " ")}
        label = f["label"].lower()
        for prefix in ("your ", "a ", "which "):
            if label.startswith(prefix):
                label = label[len(prefix):]
        keys.add(label)
        if f["key"] == "short_description":
            keys |= {"summary", "problem", "issue", "short description", "short desc",
                     "details", "description", "what happened"}
        if f["key"] == "full_name":
            keys |= {"name"}
        if f["key"] == "account_number":
            keys |= {"account", "account no", "account #", "ban"}
        if f["key"] == "contact_phone":
            keys |= {"phone", "phone number", "contact number", "mobile", "telephone"}
        if f["key"] == "contact_email":
            keys |= {"email", "email address", "e-mail"}
        if f["key"] == "service_affected":
            keys |= {"service", "affected service"}
        for k in keys:
            aliases[k] = f["key"]

    pattern = re.compile(r"^\s*([A-Za-z][A-Za-z /#'()-]{1,40}?)\s*[:\-]\s*(.+?)\s*$")
    for idx, msg in enumerate(_user_messages(messages)):
        for line in msg.splitlines():
            m = pattern.match(line)
            if not m:
                continue
            label = " ".join(m.group(1).split()).lower().strip(" #")
            key = aliases.get(label)
            if not key:
                continue
            val = _clean_value(m.group(2))
            if val:
                found[key] = {"value": val, "index": idx}   # a later message wins
    return found


def _extract_with_model(messages: list) -> dict:
    """Model-based extraction for free-form messages. Returns {} if unavailable."""
    user_prompt = prompts.build_ticket_extract_user_prompt(
        REQUIRED_TICKET_FIELDS, _user_messages(messages))
    try:
        raw = _chat(prompts.TICKET_EXTRACT_SYSTEM_PROMPT, user_prompt,
                    max_tokens=700, temperature=0)
        data = _parse_json(raw)
    except Exception as e:
        print(f"[CHAT] ticket extraction unavailable: {e}")
        return {}
    out = {}
    for f in REQUIRED_TICKET_FIELDS:
        val = _clean_value(data.get(f["key"]))
        if val:
            out[f["key"]] = val
    return out


# Which service a problem is about, worked out from the customer's own words rather
# than asked for. "my modem has a red light" is plainly an internet problem, and
# making someone answer a question they have already answered is friction.
#
# This is a DERIVATION, not an assumption: it only ever fires on words the customer
# actually wrote, it only fills the field when nothing else supplied it, and it is
# shown in the review table so they can correct it before anything is raised. Where
# a problem mentions both sides, it says "both" rather than picking one.
_INTERNET_WORDS = (
    "modem", "internet", "wifi", "wi-fi", "broadband", "router", "online",
    "website", "web site", "browsing", "speed", "data", "connection drop",
    "no connection", "wan", "ethernet", "hub",
)
_PHONE_WORDS = (
    "phone", "dial tone", "dialtone", "call", "calls", "calling", "voice",
    "handset", "landline", "busy signal", "ring", "voicemail", "echo",
    "audio", "caller", "hang up", "hung up",
)


def _infer_service(messages: list, short_description: str = "") -> str:
    """'internet', 'phone', 'both', or '' when the words do not say."""
    text = " ".join([short_description] + _user_messages(messages)).lower()
    if not text.strip():
        return ""
    net = any(w in text for w in _INTERNET_WORDS)
    voice = any(w in text for w in _PHONE_WORDS)
    if net and voice:
        return "both"
    if net:
        return "internet"
    if voice:
        return "phone"
    return ""


def extract_ticket_fields(messages: list, known: dict = None) -> tuple:
    """Collect what the customer has given. Returns (fields, missing_keys).

    Collection is MONOTONIC: a field that has already been captured is carried
    forward and can only be replaced by the customer giving a new value, never
    silently lost. Without this the flow regressed, because each turn re-derived
    every field from the whole transcript and the model is not perfectly consistent
    between calls. A customer who had already been told "I have your affected
    service" could be asked for it again two messages later, which is exactly the
    behaviour the product owner reported.

    Carrying forward must not make a value IMMUTABLE. A customer who mistypes their
    account number has to be able to correct it, so a newly found value always wins
    over a carried one. The carried value only fills a gap where this turn found
    nothing, which is precisely the regression being guarded against.

    Precedence is by RECENCY, not by format. An earlier labelled line such as
    "account number- 123456789" must not outrank a later correction written in prose
    ("actually my account number is 777000111"), which is what happens if labelled
    values are treated as authoritative simply because they are unambiguous.

    Strongest first:
      1. a value LABELLED in the customer's most recent message, which is both
         explicit and the latest thing they said
      2. a value the model read from the transcript, which is recency aware because
         the prompt tells it to take the most recent mention
      3. a value LABELLED in an earlier message
      4. a value established on an earlier turn, filling only what this turn missed
    """
    carried = {k: _clean_value(v) for k, v in (known or {}).items()
               if k in _FIELD_BY_KEY and _clean_value(v)}

    labelled = _extract_labelled(messages)
    last_index = max(0, len(_user_messages(messages)) - 1)
    latest_labelled = {k: d["value"] for k, d in labelled.items() if d["index"] == last_index}
    older_labelled = {k: d["value"] for k, d in labelled.items() if d["index"] != last_index}

    # The model runs unless the newest message labelled everything, because free text
    # is where corrections live and a carried value would otherwise mask them.
    from_model = {}
    if len(latest_labelled) < len(REQUIRED_TICKET_FIELDS):
        from_model = _extract_with_model(messages)

    fields = dict(carried)
    fields.update(older_labelled)
    fields.update(from_model)
    fields.update(latest_labelled)

    # Last resort for the affected service only: derive it from what the customer
    # described. Runs after everything else, so an explicit answer always wins.
    if not fields.get("service_affected"):
        derived = _infer_service(messages, fields.get("short_description", ""))
        if derived:
            fields["service_affected"] = derived

    missing = [f["key"] for f in REQUIRED_TICKET_FIELDS if not fields.get(f["key"])]
    return fields, missing


def _field_prompt_lines() -> list:
    out = []
    for f in REQUIRED_TICKET_FIELDS:
        line = f"{f['label']}"
        if f.get("hint"):
            line += f" ({f['hint']})"
        out.append(line)
    return out


def _review_rows(fields: dict) -> list:
    """The completed ticket as label and value pairs, in the configured order."""
    return [{"key": f["key"], "label": f["label"], "value": fields.get(f["key"], "")}
            for f in REQUIRED_TICKET_FIELDS]


def build_incident_payload(fields: dict, tried_steps: list = None,
                           suggested_steps: list = None) -> dict:
    """Turn validated fields into what create_incident() needs.

    The description opens with a single sentence stating the problem AND the outcome,
    for example "modem has a red light, tried the recommended steps but still not
    working", because an agent reading the first line should immediately know that the
    obvious fixes have already failed. The structured breakdown follows underneath.

    The contact fields stay in a labelled block, which is the same shape
    servicenow_client._parse_block() reads, so a ticket raised here can be parsed back
    by this app without a special case.
    """
    problem = fields.get("short_description", "").strip().rstrip(".")

    # Attempted and suggested are kept APART on purpose. Listing the provider steps as
    # "attempted" would tell the agent a technician had already been sent, which is the
    # opposite of true and would waste the first call.
    tried = [str(x).strip() for x in (tried_steps or []) if str(x).strip()]
    suggested = [str(x).strip() for x in (suggested_steps or []) if str(x).strip()]

    opening = (f"{problem}, tried the recommended steps but still not working."
               if tried else
               f"{problem}. No self-service steps were available for this issue.")

    body = [opening, "", "Raised from a customer self-service conversation.", ""]

    if tried:
        body += ["Already attempted by the customer, from the recommended resolutions:"]
        body += [f"  {i}. {t}" for i, t in enumerate(tried, 1)]
        body += ["", "Outcome: the customer reported the issue is still not resolved.", ""]
    if suggested:
        body += ["Suggested next steps for our team, taken from how similar tickets were "
                 "resolved. NOT yet carried out:"]
        body += [f"  {i}. {t}" for i, t in enumerate(suggested, 1)]
        body += [""]

    contact = []
    for f in REQUIRED_TICKET_FIELDS:
        if f["key"] == "short_description":
            continue
        val = fields.get(f["key"], "")
        if val:
            contact.append(f"{f['label']}: {val}")
    if contact:
        body += ["Contact details:"] + contact + [""]

    # No trailing "Description:" section: the opening sentence already states the
    # problem, and parse_ticket() falls back to synthesising a block from the native
    # description field for tickets that have no labelled journal entry, so nothing
    # downstream needs the duplicate.
    return {"short_description": problem, "description": "\n".join(body).rstrip()}


def _ticket_flow(messages: list, action: str, tried_steps: list = None,
                 suggested_steps: list = None, known_fields: dict = None) -> dict:
    """The collect, validate, review, confirm sequence for raising a ticket.

    Validation runs again on confirm rather than trusting what the client sends
    back, for the same reason /fallout/approve re-derives its reassignment target
    server side: a stale or tampered client must not be able to skip a check.
    """
    if action == "cancel_ticket":
        return _turn("escalate",
                     "No problem, I have not raised anything. I can put you through to "
                     "someone if you would rather.",
                     quick_replies=[{"label": "Talk to a representative", "action": "talk_to_rep"},
                                    {"label": "Raise a ticket after all", "action": "open_ticket"}])

    fields, missing = extract_ticket_fields(messages, known_fields)

    if action == "open_ticket":
        # Ask for everything still outstanding. Anything the customer already stated
        # is carried forward rather than asked for twice, but nothing is filled in on
        # their behalf.
        if not missing:
            return _ticket_review(fields)
        asks = [_FIELD_BY_KEY[k] for k in missing]
        lines = [f"  - {f['label']}" + (f" ({f['hint']})" if f.get("hint") else "") for f in asks]
        have = [f["label"] for f in REQUIRED_TICKET_FIELDS if fields.get(f["key"])]
        reply = ["I can raise a ticket for you. I just need these details, and you can "
                 "send them all in one message:", ""] + lines
        if have:
            reply += ["", "I already have: " + ", ".join(have) + "."]
        return _turn("ticket_collect", "\n".join(reply),
                     ticket_fields=_review_rows(fields),
                     missing_fields=[f["label"] for f in asks],
                     quick_replies=[{"label": "Cancel", "action": "cancel_ticket"}])

    if action == "ticket_details":
        if missing:
            return _ticket_missing(fields, missing)
        return _ticket_review(fields)

    # confirm_ticket: last gate before the only write this flow performs.
    if missing:
        return _ticket_missing(fields, missing)
    payload = build_incident_payload(fields, tried_steps, suggested_steps)
    try:
        created = servicenow_client.create_incident(
            short_description=payload["short_description"],
            description=payload["description"],
        )
    except Exception as e:
        print(f"[CHAT] incident creation failed: {e}")
        return _turn("ticket_failed",
                     "I could not raise the ticket just now. Nothing was created and your "
                     "details are safe. Let me put you through to someone who can raise it.",
                     quick_replies=[{"label": "Talk to a representative", "action": "talk_to_rep"},
                                    {"label": "Try again", "action": "confirm_ticket"}],
                     ticket_fields=_review_rows(fields),
                     error=str(e)[:200])

    number = created.get("number", "")
    reply = (f"Your ticket is raised. The reference is {number}. Our team will be in touch "
             f"using the contact details you gave."
             if number else
             "Your ticket is raised. Our team will be in touch.")
    if created.get("warning"):
        print(f"[CHAT] {created['warning']}")
    return _turn("ticket_created", reply,
                 ticket_fields=_review_rows(fields),
                 created_ticket={"number": number, "url": created.get("url", ""),
                                 "assignment_group": created.get("assignment_group", "")})


def _ticket_missing(fields: dict, missing: list) -> dict:
    """Name exactly what is outstanding. Never fills a gap, never hints at a value."""
    asks = [_FIELD_BY_KEY[k] for k in missing]
    lines = [f"  - {f['label']}" + (f" ({f['hint']})" if f.get("hint") else "") for f in asks]
    noun = "one more detail" if len(asks) == 1 else f"{len(asks)} more details"
    have = [f["label"] for f in REQUIRED_TICKET_FIELDS if fields.get(f["key"])]
    reply = [f"Thanks. I still need {noun} before I can raise this:", ""] + lines
    if have:
        reply += ["", "Already noted: " + ", ".join(have) + "."]
    return _turn("ticket_missing", "\n".join(reply),
                 ticket_fields=_review_rows(fields),
                 missing_fields=[f["label"] for f in asks],
                 quick_replies=[{"label": "Cancel", "action": "cancel_ticket"}])


def _ticket_review(fields: dict) -> dict:
    """Show the completed ticket back before anything is created."""
    return _turn("ticket_review",
                 "Here is everything I have. Check it over and I will raise the ticket once "
                 "you confirm.",
                 ticket_fields=_review_rows(fields),
                 quick_replies=[{"label": "Confirm and raise ticket", "action": "confirm_ticket"},
                                {"label": "Cancel", "action": "cancel_ticket"}])


# ── source tickets and follow-up reading ──────────────────────────────────────

def _source_tickets(results: list) -> list:
    """The resolved tickets a reply was built from, for the expandable panel.

    Shown so the agent, or the person being demoed to, can see exactly which
    historical tickets produced the answer and how close each one was. The
    similarity is the real retrieval score, never a rounded-up presentation number.
    """
    out = []
    for r in results:
        m = r["metadata"]
        out.append({
            "number": m.get("number", ""),
            "similarity": r["similarity"],
            "subcategory": m.get("subcategory", ""),
            "resolution_code": m.get("resolution_code", ""),
            "short_description": m.get("short_description", ""),
            "description": m.get("description", ""),
            "resolution": m.get("resolution_notes", ""),
            "state": m.get("state", ""),
        })
    return out


# Unambiguous replies, read without spending a model call. Ordered so that a
# negative phrase wins: "thanks, still not working" must not be read as resolved
# just because it contains "thanks".
_UNRESOLVED_MARKERS = (
    "not work", "doesnt work", "does not work", "didnt work", "did not work",
    "still not", "still down", "still broken", "still no", "no luck", "nope",
    "not fixed", "same problem", "same issue", "worse", "stuck", "cant ", "can not",
    "cannot", "failed", "no change", "nothing happened", "not sure how", "how do i",
)
# An explicit request for a ticket goes straight into the creation flow rather than
# being answered with another offer. Checked BEFORE the resolved markers so that
# "no thanks, just raise a ticket" is not read as gratitude.
_TICKET_MARKERS = (
    "create a ticket", "create a new ticket", "raise a ticket", "raise a new ticket",
    "open a ticket", "open a new ticket", "log a ticket", "new ticket", "raise it",
    "raise this", "make a ticket", "file a ticket", "want a ticket", "ticket please",
)
_RESOLVED_MARKERS = (
    "that worked", "it worked", "worked now", "working now", "its working",
    "it is working", "all good", "all set", "sorted", "fixed now", "that fixed",
    "its fixed", "it is fixed", "resolved now", "back online", "back up",
    "thank you", "thanks", "cheers", "perfect", "great, that", "yes that",
)


def _followup_intent(message: str, last_steps: list) -> str:
    """Did the steps work? Returns 'resolved' or 'unresolved'.

    The keyword pass runs first because it is free, instant, and certain on the
    phrasings people actually use. The model is consulted only for genuinely
    ambiguous replies. Any failure resolves to 'unresolved', which keeps helping
    the customer rather than closing the conversation on someone still broken.
    """
    text = " ".join((message or "").lower().split())
    if not text:
        return "unresolved"
    if any(k in text for k in _TICKET_MARKERS):
        return "wants_ticket"
    if any(k in text for k in _UNRESOLVED_MARKERS):
        return "unresolved"
    if any(k in text for k in _RESOLVED_MARKERS):
        return "resolved"
    try:
        raw = _chat(prompts.FOLLOWUP_INTENT_SYSTEM_PROMPT,
                    prompts.build_followup_intent_user_prompt(message, last_steps),
                    max_tokens=60, temperature=0)
        intent = str(_parse_json(raw).get("intent", "")).strip().lower()
        return intent if intent in ("resolved", "wants_ticket") else "unresolved"
    except Exception as e:
        print(f"[CHAT] follow-up intent unavailable: {e}")
        return "unresolved"


def _last_assistant_steps(messages: list) -> list:
    """The steps the customer is replying to, for the classifier's context."""
    for m in reversed(messages or []):
        if m.get("role") == "assistant":
            return [s for s in str(m.get("content", "")).splitlines() if s.strip()]
    return []


# ── per-ticket resolutions (traceable generation) ─────────────────────────────
#
# Each retrieved ticket is converted to steps in ITS OWN model call, seeing only its
# own resolution. That is the whole point: the previous design passed all three
# resolutions in one prompt and asked for a single merged answer, so a step could
# come from anywhere and nothing shown to the customer could be traced back to a
# source. Isolation costs one call per ticket and buys a guarantee.
#
# The prose around the steps is written HERE, not by the model, so no apology,
# sympathy or filler can appear in a reply.

RESOLUTION_HEADER = "Based on previous tickets, here are the recommended resolution steps:"
RESOLUTION_QUESTION = "Has this resolved your issue?"

# The steps are split by WHO performs them, because a single interleaved list reads
# badly: step 1 asks the customer to do something, step 2 says we will do something,
# and the reader cannot tell what is actually being asked of them. Customer actions
# come first as things to try; provider actions are framed as the fallback.
CUSTOMER_STEPS_HEADER = "Try these steps:"
PROVIDER_STEPS_HEADER = "If those don't work:"
NO_CUSTOMER_STEPS_NOTE = "There is nothing to try from your end on this one."


def _steps_for_one(meta: dict) -> dict:
    """Steps for a single ticket, derived only from that ticket's resolution."""
    try:
        raw = _chat(prompts.TICKET_STEPS_SYSTEM_PROMPT,
                    prompts.build_ticket_steps_user_prompt(meta),
                    max_tokens=600, temperature=0)
        data = _parse_json(raw)
        steps = []
        for s in (data.get("steps") or []):
            text = str(s.get("text", "")).strip() if isinstance(s, dict) else str(s).strip()
            if text:
                steps.append({"text": text,
                              "needs_engineer": bool(s.get("needs_engineer")) if isinstance(s, dict) else False})
        return {"steps": steps[:5], "summary": str(data.get("summary", "")).strip()}
    except Exception as e:
        print(f"[CHAT] step generation failed for {meta.get('number', '?')}: {e}")
        return {"steps": [], "summary": ""}


def _per_ticket_resolutions(results: list) -> list:
    """One resolution group per retrieved ticket, each carrying its own citation.

    A group with no steps is dropped rather than shown empty, so the customer never
    sees a ticket heading with nothing under it.
    """
    groups = []
    for r in results:
        meta = r["metadata"]
        gen = _steps_for_one(meta)
        if not gen["steps"]:
            continue
        groups.append({
            "number": meta.get("number", ""),
            "similarity": r["similarity"],
            "subcategory": meta.get("subcategory", ""),
            "resolution_code": meta.get("resolution_code", ""),
            "short_description": meta.get("short_description", ""),
            "state": meta.get("state", ""),
            "cause": gen["summary"],
            "steps": gen["steps"],
            # The ticket's own recorded text, shown in the dropdown so the generated
            # steps can be checked against the source directly.
            "root_cause": meta.get("description", ""),
            "resolution": meta.get("resolution_notes", ""),
        })
    return groups
