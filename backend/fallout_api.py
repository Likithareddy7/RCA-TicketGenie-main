"""
Fallout Remediation API
-----------------------
Endpoints the queue-driven agent UI consumes. Recommend-only + human-in-the-loop:
approving a recommendation posts a COMMENT to ServiceNow. It never closes,
resolves, or transitions the ticket.
"""

import os
import csv
from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

import servicenow_client
import provisioning_store
import fallout_store
import fallout_engine
import validation_tools
import kb_source
import oms_store
import routing_store

router = APIRouter(prefix="/fallout", tags=["fallout"])

AUDIT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "fallout_audit.csv")
AUDIT_COLUMNS = ["timestamp", "ticket", "resolution_code", "confidence", "best_match",
                 "agent", "reassigned_to"]


class ApproveRequest(BaseModel):
    number: str
    comment: str                          # the EXACT text the agent approved (what they saw)
    agent: Optional[str] = ""
    resolution_code: Optional[str] = ""    # audit metadata, carried from the shown recommendation
    confidence: Optional[str] = ""
    best_match: Optional[str] = ""
    # Redirect approvals: the group the agent saw on the recommendation. Used only
    # to CONFIRM intent — the group actually written is re-derived server-side from
    # the routing rules, so a client cannot move a ticket to an arbitrary team.
    reassign_to: Optional[str] = ""


class BreakdownRequest(BaseModel):
    number: str                       # the historical (closed) ticket to break down
    against: Optional[str] = None      # the open ticket it was matched against


def _audit(row: dict):
    """Append an approval to the audit trail.

    An audit file written before a column was added keeps its own header — rows are
    written against THAT header (extra keys dropped) rather than silently sliding
    values into the wrong columns.
    """
    columns, new = AUDIT_COLUMNS, not os.path.exists(AUDIT_PATH)
    if not new:
        try:
            with open(AUDIT_PATH, "r", newline="", encoding="utf-8") as f:
                existing = next(csv.reader(f), None)
            if existing:
                columns = existing
        except OSError:
            pass
    with open(AUDIT_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in columns})


@router.get("/health")
def health():
    return {"status": "ok", "kb_size": fallout_store.count()}


@router.post("/rebuild-kb")
def rebuild_kb():
    n = fallout_store.build_kb()
    return {"status": "ok", "kb_size": n}


@router.get("/tickets")
def tickets():
    """The open fallout queue the agent works, plus the closed KB for reference.

    The OPEN queue comes from ServiceNow (live tickets). The CLOSED knowledge base
    comes from the incidents spreadsheet (kb_source). The KB is re-synced on every
    load, so editing the spreadsheet is reflected without a restart — only rows
    whose content actually changed are re-embedded."""
    try:
        open_q = servicenow_client.fetch_open_queue()
    except Exception as e:
        print(f"[FALLOUT] ServiceNow open-queue fetch failed: {e}")
        open_q = []

    kb = kb_source.fetch_closed_kb()
    try:
        fallout_store.sync_kb(kb)   # incremental: only changed tickets re-embed
    except Exception as e:
        print(f"[FALLOUT] KB sync on queue load failed: {e}")
    return {"open": open_q, "closed": kb, "open_count": len(open_q), "kb_count": len(kb)}


@router.get("/kb-source")
def kb_source_info():
    """Which spreadsheet is backing the KB, how its columns were mapped, and how
    many rows counted as closed. Use this to verify the mapping after dropping in
    a new export — an unmapped field here means that field is blank in the KB."""
    return kb_source.describe()


@router.get("/recommend/{number}")
def recommend(number: str):
    return fallout_engine.recommend(number)


@router.post("/breakdown")
def breakdown(req: BreakdownRequest):
    """AI breakdown of one historical ticket (symptom correlation + root cause +
    resolution steps), matched against the open ticket. Read-only."""
    return fallout_engine.generate_breakdown(req.number, req.against)


@router.get("/provisioning")
def provisioning():
    return {"active_services": provisioning_store.all_active_services()}


@router.get("/oms")
def oms():
    """The mock BOSS/OMS inventory the BAN-keyed value-checks read."""
    return {"accounts": oms_store.all_accounts(), "orders": oms_store.all_orders()}


@router.get("/routing-rules")
def routing_rules():
    """The deterministic redirect rules (e.g. Buy Flow -> its owning team) and the
    patterns each one matches on."""
    return routing_store.describe()


@router.get("/tools")
def tools():
    """The registered validation tools (name / description / input schema).
    Function-calling / MCP shaped — ready for an LLM router or an MCP server."""
    return {"tools": validation_tools.tool_schemas()}


def _redirect_target(number: str) -> str:
    """The assignment group the routing rules say owns this ticket, re-derived from
    the ticket itself. Deterministic (regex, no LLM), so re-deriving on approve is
    cheap and cannot disagree with what /recommend showed."""
    ticket = servicenow_client.get_ticket(number)
    if not ticket:
        return ""
    hit = routing_store.classify(ticket)
    return (hit or {}).get("assignment_group", "") or ""


@router.post("/approve")
def approve(req: ApproveRequest):
    """Apply the recommendation the agent approved.

    Always posts the EXACT comment the agent saw — the recommendation is generated
    ONCE in /recommend and this writes that stored text verbatim, never re-running
    the LLM (human-in-the-loop, and no extra LLM/retrieval cost).

    For a REDIRECT approval it additionally REASSIGNS the ticket to the owning team
    (assignment_group only — never state, close, or resolve). Reassignment happens
    BEFORE the comment: if the move fails, no comment is posted claiming it
    succeeded, and the agent gets the error instead.

    The destination group is re-derived server-side from data/routing_rules.json;
    `reassign_to` from the request is only checked for agreement. A client
    therefore cannot move a ticket to a team the rules do not sanction.
    """
    if not (req.comment or "").strip():
        return {"success": False, "error": "no comment to post"}

    reassigned = None
    if (req.reassign_to or "").strip():
        target = _redirect_target(req.number)
        if not target:
            return {"success": False,
                    "error": (f"{req.number} does not match any redirect rule, so it "
                              f"cannot be reassigned. Nothing was changed.")}
        if target.strip().lower() != req.reassign_to.strip().lower():
            return {"success": False,
                    "error": (f"Reassignment target has changed since this recommendation "
                              f"was generated (you approved '{req.reassign_to}', the rules "
                              f"now say '{target}'). Re-run the recommendation. Nothing "
                              f"was changed.")}
        try:
            reassigned = servicenow_client.reassign(req.number, target)
        except Exception as e:
            return {"success": False, "error": f"Reassignment failed: {e}. No comment posted."}
        if reassigned.get("error"):
            return {"success": False, "error": reassigned["error"], "reassigned": reassigned}

    result = servicenow_client.post_comment(req.number, req.comment)

    _audit({
        "timestamp": datetime.now().isoformat(),
        "ticket": req.number,
        "resolution_code": req.resolution_code or "",
        "confidence": req.confidence or "",
        "best_match": req.best_match or "",
        "agent": req.agent or "",
        "reassigned_to": (reassigned or {}).get("assignment_group", ""),
    })

    return {"success": True, "posted": result, "comment": req.comment,
            "reassigned": reassigned}


# ── free-text search ("Ticket Genie") ────────────────────────────────────

MAX_QUERY_CHARS = 500


class SearchRequest(BaseModel):
    query: str
    k: Optional[int] = 5


@router.post("/search")
def search(req: SearchRequest):
    """Search the closed-ticket knowledge base with a plain-language problem
    description, and return the closest resolved tickets with their resolutions.

    ONE input, two behaviours, because the UI has a single search box:
      * a bare incident number ("INC0010018") is a request for the FULL pipeline
        on that ticket, validation, redirect routing, and an approvable comment.
        It is answered by /recommend's engine call, so nothing about the queue
        workflow changes just because the queue sidebar is gone.
      * anything else is a history search: retrieve, then consolidate what was
        actually recorded. Read-only, and no validation tool runs (they key on a
        BAN / Location ID / order id, which a typed sentence does not have).

    The KB is re-synced here because this endpoint replaced /fallout/tickets as
    the first call the UI makes, and that was the only caller of sync_kb(), so
    without this an edit to the incidents spreadsheet would stop being picked up.
    It is incremental (content-hashed), so unchanged rows are not re-embedded.
    """
    query = (req.query or "").strip()
    if not query:
        return {"error": "Enter a problem description to search."}
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]

    number = fallout_engine.looks_like_ticket_number(query)
    if number:
        rec = fallout_engine.recommend(number)
        rec["mode"] = "ticket"
        rec["query"] = query
        return rec

    try:
        fallout_store.sync_kb()
    except Exception as e:
        print(f"[FALLOUT] KB sync on search failed: {e}")
    return fallout_engine.search_resolutions(query, k=req.k or 5)


@router.get("/examples")
def examples():
    """Seed queries for the search screen's empty state.

    Deliberately drawn from the fallout types the KB actually contains: the KB is
    order-fallout only, so a plausible-sounding prompt like "how to fix modem"
    returns nothing and makes a working search look broken. These are measured to
    return strong matches against the shipped KB.
    """
    return {"examples": [
        "order not showing on the account after a speed upgrade",
        "account still pending after the order completed",
        "open task past due date blocking the order",
        "cancelled order but the account is still active",
        "wrong banner showing on a copper account",
    ]}


# ── customer conversation ("Ticket Genie" chat) ───────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = []
    # Which quick reply the customer pressed, if any. Intent is taken from the
    # button rather than inferred from free text, so the conversation's control
    # flow stays deterministic (see fallout_engine.chat_turn).
    action: Optional[str] = ""


@router.post("/chat")
def chat(req: ChatRequest):
    """One assistant turn in the customer conversation.

    Stateless: the client sends the whole transcript each time, so there is no
    session store to keep in sync. Read-only, like every other path here except
    /approve. Accepting "open a ticket" returns a DRAFT and creates nothing in
    ServiceNow.
    """
    msgs = [{"role": m.role, "content": m.content} for m in (req.messages or [])]
    if len(msgs) > 40:
        msgs = msgs[-40:]
    action = (req.action or "").strip()
    if action and action not in fallout_engine.CHAT_ACTIONS:
        action = ""
    try:
        return fallout_engine.chat_turn(msgs, action)
    except Exception as e:
        print(f"[FALLOUT] chat turn failed: {e}")
        return {"stage": "escalate", "reply": "Something went wrong on my side. Let me get "
                                              "you to someone who can help.",
                "steps": [], "quick_replies": [{"label": "Talk to a representative",
                                                "action": "talk_to_rep"}],
                "needs_engineer": True, "sources_count": 0, "ticket_draft": None}
