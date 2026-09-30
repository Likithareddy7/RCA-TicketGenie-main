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
SEARCH_MIN_SIM = 0.30      # below this: no usable match, and the LLM is not called
SEARCH_PARTIAL_SIM = 0.45  # 0.30-0.45 reads as 'weak', 0.45-0.60 as 'partial'
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


def _search_candidates(results: list) -> list:
    """The matches shown for a search. Carries more of the record than the queue's
    `_candidates()` does, because here the historical ticket IS the answer rather
    than supporting evidence for a live one."""
    out = []
    for r in results:
        meta = r["metadata"]
        out.append({
            "number": meta.get("number", ""),
            "similarity": r["similarity"],
            "strength": _strength(r["similarity"]),
            "resolution_code": meta.get("resolution_code", ""),
            "subcategory": meta.get("subcategory", ""),
            "short_description": meta.get("short_description", ""),
            "resolution": meta.get("resolution_notes", ""),
            "state": meta.get("state", ""),
        })
    return out


def _generate_search_resolution(query: str, top: list) -> dict:
    """Consolidated resolution for a typed problem: each match's own steps plus a
    merged summary. Mirrors _generate_resolution(), but prompted for a query with
    no identifiers (see prompts.SEARCH_RESOLUTION_SYSTEM_PROMPT).

    Falls back to quoting each ticket's OWN recorded close notes when the model is
    unavailable, still grounded, still useful, and flagged `verbatim` so the UI
    never presents a quote as tailored advice.
    """
    def _skeleton():
        return [{"number": r["metadata"].get("number", ""),
                 "similarity": r["similarity"],
                 "resolution_code": r["metadata"].get("resolution_code", ""),
                 "steps": _steps_from_notes(r["metadata"].get("resolution_notes", "")),
                 "verbatim": True} for r in top]

    if not top:
        return {"per_ticket": [], "merged_summary": ""}

    user_prompt = prompts.build_search_resolution_user_prompt(query, top)
    try:
        raw = _chat(prompts.SEARCH_RESOLUTION_SYSTEM_PROMPT, user_prompt,
                    max_tokens=1200, temperature=0.2)
        data = _parse_json(raw)
        steps_by_num = {}
        for pt in (data.get("per_ticket") or []):
            num = str(pt.get("number", "")).strip()
            steps_by_num[num] = [str(s).strip() for s in (pt.get("steps") or []) if str(s).strip()]
        # Similarity and resolution code come from metadata, never from the model.
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
        print(f"[ENGINE] search resolution generation failed: {e}")
        # `verbatim` tells the UI these are the resolving agents' own recorded words,
        # not guidance written for this search, so it can say so rather than passing
        # a quote off as tailored advice.
        return {"per_ticket": _skeleton(), "merged_summary": "", "verbatim": True}


def search_resolutions(query: str, k: int = 5) -> dict:
    """Find the closest resolved tickets for a typed problem description.

    Read-only: retrieves, then writes prose about what was already recorded. No
    tool runs, no ticket is touched.
    """
    query = (query or "").strip()
    if not query:
        return {"mode": "search", "query": "", "error": "Enter a problem description to search."}

    try:
        results = fallout_store.search(query, k=max(k, SEARCH_TOP_N))
    except Exception as e:
        print(f"[ENGINE] search retrieval failed: {e}")
        return {"mode": "search", "query": query,
                "error": "The knowledge base is unavailable. Please try again."}

    kb_size = fallout_store.count()
    usable = [r for r in results if r["similarity"] >= SEARCH_MIN_SIM][:SEARCH_TOP_N]

    # Nothing clears the floor: say so, and show the nearest miss WITH its real
    # score so the answer is checkable. No LLM call, writing confident steps from
    # history this distant is how a search tool starts inventing things.
    if not usable:
        near = _search_candidates(results[:1])
        return {"mode": "search", "query": query, "no_match": True,
                "kb_size": kb_size,
                "top_similarity": results[0]["similarity"] if results else 0.0,
                "match_strength": "none", "matches": [],
                "near_miss": near[0] if near else None,
                "resolution": {"per_ticket": [], "merged_summary": ""},
                "message": ("No resolved ticket in the knowledge base is close enough to "
                            "this description to base a resolution on.")}

    resolution = _generate_search_resolution(query, usable)
    top_sim = usable[0]["similarity"]
    return {"mode": "search", "query": query, "no_match": False,
            "kb_size": kb_size,
            "top_similarity": top_sim,
            "match_strength": _strength(top_sim),
            "matches": _search_candidates(usable),
            "resolution": resolution,
            "message": ""}


# ── customer conversation ("Ticket Genie" chat) ──────────────────────────────
#
# A conversation, not a search box. The customer describes a problem in their own
# words, gets steps to try, and is offered a ticket or a rep when the steps run out.
#
# The CONTROL FLOW here is deterministic and the model only writes prose, which is
# the same split the rest of this app uses. Intent comes from the quick-reply the
# customer pressed, not from asking a model to interpret free text, so "these steps
# worked" can never be misread as "I am stuck" and the conversation cannot be
# derailed by an LLM outage. Free text is always treated as describing a problem,
# which is the safe default: the worst case is that we retrieve and offer help again.
#
# Stages the client renders:
#   troubleshoot   steps were found, ask whether they worked
#   no_match       nothing close enough in the KB, offer a ticket or a rep
#   escalate       the customer is stuck or needs our side to act
#   resolved       the customer confirmed it is fixed, conversation closed
#   ticket_draft   a ticket was drafted. NOTHING was sent to ServiceNow
#   rep            hand off to a human rep
#   ticket_lookup  an incident number was typed, return the full agent pipeline

CHAT_ACTIONS = ("worked", "stuck", "open_ticket", "talk_to_rep")

# Offered after steps, and after an escalation. Deterministic per stage, so the
# buttons and the stage can never disagree.
_QR_AFTER_STEPS = [{"label": "That fixed it", "action": "worked"},
                   {"label": "I am stuck on a step", "action": "stuck"}]
_QR_ESCALATE = [{"label": "Open a ticket for me", "action": "open_ticket"},
                {"label": "Talk to a representative", "action": "talk_to_rep"}]


def _turn(stage, reply, steps=None, quick_replies=None, **extra) -> dict:
    out = {"stage": stage, "reply": reply, "steps": steps or [],
           "quick_replies": quick_replies or [], "needs_engineer": False,
           "sources_count": 0, "match_strength": "none", "ticket_draft": None}
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


def _draft_ticket(messages: list) -> dict:
    """Build a ticket draft from the conversation.

    Deliberately NOT written to ServiceNow. This app's write surface is exactly two
    operations (post_comment and reassign), and creating incidents would be a third,
    which is a product decision rather than an implementation detail. So the draft is
    returned for a human to submit and the response says plainly that nothing was
    filed.
    """
    said = [str(m.get("content", "")).strip() for m in (messages or [])
            if m.get("role") == "user" and str(m.get("content", "")).strip()]
    summary = said[0] if said else "Customer-reported issue"
    transcript = "\n".join(f"Customer: {s}" for s in said)
    return {
        "short_description": summary[:160],
        "description": ("Raised from a customer self-service conversation.\n\n"
                        f"{transcript}"),
        "submitted": False,
        "note": ("This is a draft only. Nothing has been created in ServiceNow. "
                 "An agent must review and submit it."),
    }


def _customer_steps(question: str, messages: list, results: list) -> dict:
    """Translate internal resolutions into customer-safe steps.

    There is deliberately NO verbatim fallback here, unlike the agent-facing paths.
    Quoting an engineer's close notes to a subscriber would leak internal system
    names and other customers' identifiers, and would tell them to do things only an
    employee can do. When the model is unavailable the honest answer is no steps,
    which routes the conversation to a ticket or a rep.
    """
    user_prompt = prompts.build_customer_chat_user_prompt(question, messages, results)
    try:
        raw = _chat(prompts.CUSTOMER_CHAT_SYSTEM_PROMPT, user_prompt,
                    max_tokens=900, temperature=0.3)
        data = _parse_json(raw)
        steps = []
        for s in (data.get("steps") or []):
            text = str(s.get("text", "")).strip() if isinstance(s, dict) else str(s).strip()
            if text:
                steps.append({"text": text,
                              "needs_engineer": bool(s.get("needs_engineer")) if isinstance(s, dict) else False})
        return {"reply": str(data.get("reply", "")).strip(),
                "steps": steps[:5],
                "needs_engineer": bool(data.get("needs_engineer_overall")),
                "closing_question": str(data.get("closing_question", "")).strip(),
                "ai_available": True}
    except Exception as e:
        print(f"[CHAT] customer step generation unavailable: {e}")
        return {"reply": "", "steps": [], "needs_engineer": True,
                "closing_question": "", "ai_available": False}


def chat_turn(messages: list, action: str = "") -> dict:
    """One assistant turn. Stateless: the client sends the whole conversation.

    Read-only. Nothing in this path writes to ServiceNow.
    """
    messages = messages or []
    action = (action or "").strip()

    if action == "worked":
        return _turn("resolved",
                     "That is great to hear. I will close this off here. If the problem "
                     "comes back, just start a new message and we will pick it up again.")

    if action == "talk_to_rep":
        return _turn("rep",
                     "No problem. I will pass you to a representative who can pick this up "
                     "with you directly. Please hold while I connect you.",
                     handoff=True)

    if action == "open_ticket":
        draft = _draft_ticket(messages)
        return _turn("ticket_draft",
                     "I have put together a summary of the problem for our support team. "
                     "One of our agents will review it and get in touch.",
                     quick_replies=[{"label": "Talk to a representative", "action": "talk_to_rep"}],
                     ticket_draft=draft)

    if action == "stuck":
        return _turn("escalate",
                     "Thanks for trying those. This one looks like it needs someone on our "
                     "side to take a proper look at your account. I can raise it with our "
                     "support team, or put you through to a representative now.",
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
                     "I could not find anything in our records that matches this closely "
                     "enough for me to suggest a reliable fix. I would rather get it in front "
                     "of someone who can look at your account properly than guess. I can raise "
                     "a ticket for you, or connect you to a representative.",
                     quick_replies=_QR_ESCALATE,
                     top_similarity=results[0]["similarity"] if results else 0.0)

    gen = _customer_steps(question, messages, usable)
    top_sim = usable[0]["similarity"]

    # Steps were found: present them and ask whether they worked.
    if gen["steps"]:
        reply = gen["reply"] or "Here is what has resolved this for other customers."
        if gen["closing_question"]:
            reply = f"{reply}\n\n{gen['closing_question']}"
        return _turn("troubleshoot", reply, steps=gen["steps"],
                     quick_replies=_QR_AFTER_STEPS,
                     needs_engineer=gen["needs_engineer"],
                     sources_count=len(usable),
                     match_strength=_strength(top_sim),
                     top_similarity=top_sim,
                     ai_available=gen["ai_available"])

    # Matches exist but nothing the customer can do themselves, or the model is
    # unavailable and we will not quote internal notes at a customer.
    reply = gen["reply"] or (
        "I can see we have handled problems like this before, but it is not something "
        "you can fix from your end. It needs one of our team to make a change for you. "
        "I can raise a ticket, or put you through to a representative."
        if gen["ai_available"] else
        "I found similar cases in our records, but I am not able to write them up for you "
        "right now. Rather than guess, let me get this to someone who can help properly. "
        "I can raise a ticket, or connect you to a representative.")
    return _turn("escalate", reply, quick_replies=_QR_ESCALATE, needs_engineer=True,
                 sources_count=len(usable), match_strength=_strength(top_sim),
                 top_similarity=top_sim, ai_available=gen["ai_available"])
