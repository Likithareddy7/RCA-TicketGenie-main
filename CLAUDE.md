# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

**RCA-TicketGenie** is a **ServiceNow order-fallout remediation assistant** for a
telecom OSS/BSS context. For each *open* fallout ticket it retrieves the most
similar *closed* tickets, validates the current system state with a deterministic
check, and produces a recommended resolution + remediation. A human agent reviews
it and, on approval, the exact recommendation is posted **as a comment** on the
ServiceNow ticket.

**Human-in-the-loop.** The app NEVER closes, resolves, or transitions a ticket.
It performs exactly two writes, both only on human approval: posting a **comment**,
and — for tickets matching a redirect rule (currently Buy Flow) — **reassigning**
the ticket to the owning team by setting `assignment_group`. Approval posts the
*exact* text the agent saw/edited — the LLM is not re-run on approve.

> The reassignment is an explicit, owner-approved exception to what was previously
> a comment-only rule. Buy Flow tickets are genuinely moved to `BUYFLOW TEAM`;
> every other fallout family remains recommend-plus-comment.

> History note: this replaced an earlier Jira + LangGraph triage app. Anything
> referencing Jira, LangGraph, a chat UI, or voice/Whisper is vestigial. Two old
> endpoints (`/transcribe`, `/add-to-watchlist`) and `data_manager.py` still exist
> in the backend but nothing in the current UI calls them.

## Architecture

```
ServiceNow (incidents)          Incidents spreadsheet (data/*.xlsx)
   │  OPEN queue only              │  CLOSED rows only — the knowledge base
   │  parse the labelled block      │  column headers auto-mapped by kb_source
   │  inside `comments`             │
   ▼                                ▼
Backend (FastAPI, backend/)
   ├─ servicenow_client.py   read OPEN queue + post_comment (never closes)
   ├─ kb_source.py           loads the CLOSED KB from the incidents spreadsheet
   ├─ fallout_store.py       ChromaDB + BM25 hybrid search over CLOSED tickets (the KB)
   ├─ fallout_engine.py      recommend(): retrieve → route tool → validate → generate → decide
   ├─ validation_tools.py    @tool registry — deterministic system-state checks
   ├─ routing_store.py       deterministic redirect rules (data/routing_rules.json)
   ├─ provisioning_store.py  LOC-/SVC-keyed inventory (data/provisioning_data.json)
   ├─ oms_store.py           BAN-/order-keyed BOSS OMS inventory (data/oms_data.json)
   ├─ prompts.py             all LLM system prompts + user-prompt builders
   ├─ fallout_api.py         /fallout/* endpoints
   ├─ fake_tickets.py        synthetic demo tickets (data/fake_tickets.json)
   └─ mcp_server.py          exposes the same checks as MCP tools
   ▼
Frontend (React + Vite + Tailwind, frontend/)
   ├─ App.jsx                state container (queue → recommend → approve)
   ├─ components/QueueSidebar.jsx        open queue
   ├─ components/RecommendationView.jsx  the recommendation + approve/edit bar
   ├─ components/SimilarTicket.jsx       expandable historical-match breakdown
   └─ components/ui.jsx                  shared presentational primitives
```

### The recommendation pipeline (`fallout_engine.recommend`)

0. **REDIRECT pre-route** (deterministic, before anything else). `routing_store`
   matches the ticket against `data/routing_rules.json`. A hit — e.g. a **Buy Flow**
   ticket — short-circuits the whole pipeline: the recommendation becomes *reassign
   to the owning team*, with fixed steps and no LLM involvement. "Every Buy Flow
   ticket goes to the Buy Flow team" is a business rule, so it must not depend on
   retrieval quality or model judgement. Everything below runs only for tickets this
   queue actually owns.
1. **RETRIEVE** top-K similar *closed* tickets via hybrid search.
2. **ROUTE** — an LLM router reads how those tickets were resolved + the available
   tools and picks WHICH validation tool to run and which identifiers to pull.
3. **VALIDATE** — the chosen tool runs **deterministically** and returns a verdict
   (`confirmed` / `ambiguous` / `not_confirmed`). If no tool fits but grounding is
   strong, the verdict is `not_applicable` (neutral, not a failure).
4. **GENERATE** — a *Resolution* (history-only, from the top-3 matches; always
   produced) and a *Remediation* (validation-driven; only when a tool ran).
5. **DECIDE** — `recommend` vs `needs_review`, with a confidence level.

The LLM **routes and writes prose**; the **verdict and pulled identifiers are
deterministic**. Retrieval confidence gate: `SIM_STRONG = 0.60`.

### Two remediation families (two id schemes)

The app now handles fallout keyed on **two different identifier schemes**, and the
validation tools are split accordingly — do not merge them:

| Family | Ids | Store | Tools |
| --- | --- | --- | --- |
| Duplicate service / porting | `LOC-`, `SVC-` | `provisioning_store` | `provisioning.check_active_service` |
| BOSS OM (order + account) | BAN, order id, `OMTASK` | `oms_store` | `oms.check_account_status`, `oms.check_order_status`, `oms.check_network_type` |

The `oms.*` checks answer **"which value is wrong?"** — each compares two values
that are supposed to agree (account status vs its orders' status; banner vs
provisioned network type; order stage vs whether it reflects on the account) and
returns `data.field` / `data.current` / `data.expected` so the recommendation can
name the exact correction rather than describing it vaguely.

`servicenow_client.parse_ticket()` and `kb_source` both extract `ban` / `order_ref`
/ `task_ref`, so live tickets and spreadsheet KB tickets have the same shape and the
router can pass identifiers from either.

### Redirect / reassignment (Buy Flow)

Some fallout is not this queue's to fix. `data/routing_rules.json` holds regex rules
mapping a ticket to an owning `assignment_group`; `routing_store.classify()` is
deterministic and runs *before* retrieval and LLM routing.

**Approving a redirect actually MOVES the ticket.** `/fallout/approve` reassigns
first, then posts the comment — so a failed move never leaves behind a comment
claiming success. `rec["action"]` is `"redirect"` vs `"comment"`, and
`format_comment()` branches on it.

Two guards on that write, both deliberate:

* The destination is **re-derived server-side** from the routing rules on approve.
  The request's `reassign_to` is only compared against it, so a client cannot move
  a ticket to a team the rules do not sanction, and a stale recommendation is
  rejected rather than applied.
* ServiceNow silently **drops** an `assignment_group` it cannot resolve to a real
  group, so `reassign()` reads the field back and reports a mismatch as a failure.
  Without this, a typo in `routing_rules.json` would look like a successful
  reassignment while the ticket sat untouched.

To add a team: append a rule with `match_any` regexes and the exact ServiceNow
assignment-group name. No code change needed. Verify via `GET /fallout/routing-rules`.
A rule with an empty `assignment_group` downgrades to recommend-and-comment.

### Knowledge-base source (spreadsheet, not ServiceNow)

The historical KB comes from an incidents spreadsheet in `data/` (`.xlsx`/`.csv`),
loaded by `kb_source.py`; ServiceNow supplies only the OPEN queue. Only rows whose
state reads as closed are indexed (`CLOSED_STATES` / `CLOSED_PREFIXES`).

Column headers are auto-mapped by alias — exact match first, then substring — and
`data/kb_column_map.json` overrides the mapping per field when auto-detection is
wrong. **Verify the mapping before trusting the KB**: run `python kb_source.py`
from `backend/`, or `GET /fallout/kb-source`. Set `KB_EXCEL_PATH` to pin a specific
file, otherwise the newest spreadsheet in `data/` wins.

Gotcha the mapper already guards: incident exports often carry a geographic
**State** column alongside the ticket **Status**. Matching on the header alone
picks the wrong one and silently produces a KB with zero closed tickets, so
`_pick_state_column()` also checks the column's *values* against a lifecycle
vocabulary. Duplicate ticket numbers are suffixed (`_dedupe`) since Chroma ids must
be unique, and upserts are batched at 500.

#### What the BOSS OM export needs (and why)

The current export's headers are `number, assigned_to, state,
comments_and_work_notes, category, assignment_group, u_issue_type,
short_description, description, close_notes`. Three things about it are not
obvious and are pinned in `data/kb_column_map.json`:

* **`subcategory` maps to `u_issue_type`, never `category`.** Subcategory is one of
  the four EMBEDDED fields. `u_issue_type` is the discriminative fallout type
  (`Staging Stuck`, `Account Status Mismatch`, `Network Type Mismatch`);
  `category` is a two-value application bucket that adds no retrieval signal.
  `category` is kept as separate non-embedded metadata.
* **There is no resolution-code column.** The `{RCA TAG : Order Completion -
  Staging Stuck}` marker agents write into `close_notes` *is* the resolution code,
  and `_RCA_TAG` extracts it. Brace/paren/`RCATag`/unclosed variants all parse.
* **Identifiers are inline, not columnar.** This data keys on **BAN**, order ids
  (`WI2100002247` — two letters + 8–12 digits) and `OMTASK…`, not the `LOC-`/`SVC-`
  scheme the original demo data used. `_row_to_ticket` recovers them by regex,
  reading `close_notes` before the customer's own description so the agent's
  corrected id wins.

`comments_and_work_notes` is ~90% journal boilerplate (auto-close notices,
`Assignment Rule … has been applied`, `Record Producer: … was used`).
`_scrub_notes()` strips exactly those lines and keeps genuine agent commentary.

### ServiceNow data quirk (important)

Native incident fields are NOT populated. Every structured field (State, Location
ID, order refs, Service Type, Resolution Code, Description, Work/Resolution Notes)
lives as a labelled text block inside the `comments` journal field.
`servicenow_client._canonical_block()` picks the newest journal entry containing
both `Location ID:` and `Description:`; `_parse_block()` is generic
(`Label: value` → fields, bare `Label:` → sections), so new fallout types need no
parser change. Fallout tickets are found by `short_descriptionLIKEfallout`.

### Embedding policy

Problem-side ONLY: `short_description + description + subcategory + service_type`.
The **resolution is never embedded** (it's the answer — embedding it would break
query/document symmetry). Everything else rides in Chroma metadata and is used
after retrieval.

## Running

Backend (from `backend/`, needs the env vars below):

```bash
uvicorn main:app --port 8000
```

Frontend (from `frontend/`, Vite dev server on **5050**):

```bash
npm install
npm run dev
```

CORS in `main.py` allows ports 5050 / 5173 / 5174. The frontend calls the backend
at `http://localhost:8000` (`frontend/src/api.js`).

Optional MCP server (exposes the validation checks as MCP tools):

```bash
python mcp_server.py            # Streamable HTTP at http://127.0.0.1:8765/mcp
python mcp_server.py --stdio    # stdio transport
```

## Configuration (`backend/.env`)

Create `backend/.env` (do NOT commit it — it is gitignored). Keys used:

| Key | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude — every LLM call (router, resolution, remediation, breakdown) |
| `ANTHROPIC_MODEL` | chat model (default `claude-sonnet-4-5`) |
| `SERVICENOW_INSTANCE` | instance base URL |
| `SERVICENOW_USER` / `SERVICENOW_PASSWORD` | ServiceNow account (used by both auth modes) |
| `SERVICENOW_CLIENT_ID` / `SERVICENOW_CLIENT_SECRET` | OAuth client — **preferred**; unset both to fall back to Basic auth |
| `SERVICENOW_QUEUE_GROUP` | assignment group whose incidents form the queue (default `BOSS OM IT Support`) |
| `SERVICENOW_QUERY` | optional — override the whole incident query |
| `KB_EXCEL_PATH` | optional — pin the KB spreadsheet (default: newest in `data/`) |
| `KB_EXCEL_SHEET` | optional — sheet name (default: first sheet) |

### ServiceNow auth: use OAuth

ServiceNow now ships **Basic Auth API Restriction**
(`glide.authenticate.basic_auth.restriction.*`). When active, Basic auth is blocked
for REST while UI login keeps working — so an instance looks perfectly healthy in a
browser while every API call returns `401 User is not authenticated`. Diagnose it by
reading those properties; the allow-list lives in `sys_user_basic_auth_exception`,
which needs an *elevated* `security_admin` session to write (holding the role is not
enough).

`servicenow_client` therefore prefers OAuth and falls back to Basic only when no
client is configured. It uses the **password grant** rather than client_credentials
deliberately: the token is bound to a real user, so comments and reassignments are
attributed to that account in the ticket's audit trail. Tokens are cached until a
minute before expiry, and a 401 triggers exactly one refresh-and-retry.

To create the client: **System OAuth → Application Registry → New → Create an OAuth
API endpoint for external clients**. The `client_secret` field reads back encrypted
via the API, so set it to a known value rather than trying to read the generated one.

### Which incidents form the queue

`FALLOUT_QUERY` matches on **assignment group**, not on the word "fallout" in the
summary — real tickets read like "Remove TN (973) 396-2160 on BAN 1000302044" and
never say "fallout". Note that an approved Buy Flow redirect therefore makes the
ticket **drop out of the queue**, which is correct: it is no longer this team's.
`get_ticket()` looks up by number and is unaffected.

Seed a queue into an instance with `python seed_open_queue.py` (from `backend/`;
`--dry-run` supported). It creates the queue group and every group the routing rules
redirect to, since ServiceNow silently drops an `assignment_group` it cannot resolve.

**Embeddings need no key.** Anthropic has no embeddings endpoint, so the vector side
runs locally — `all-MiniLM-L6-v2` through `onnxruntime`, via Chroma's
`DefaultEmbeddingFunction` (`fallout_store._get_embedding_function`). The model is
cached under `~/.cache/chroma` on first use and works offline afterwards. This means
**retrieval, BM25, and the whole redirect path work with no API key at all** — only
the LLM-written prose needs Claude.

Consequence worth remembering: local embeddings are **384-dimensional**, OpenAI's
were 1536. Chroma pins dimension at collection-creation time, so switching embedders
requires deleting `data/chroma_db/` and rebuilding — a mismatched collection fails to
query rather than silently degrading.

`OPENAI_API_KEY` is now optional and unused by the pipeline; only the vestigial
`/transcribe` endpoint touches it, and nothing in the UI calls that. `JIRA_*` and
`NGROK_AUTHTOKEN` are vestigial from the old app and unused.

## Conventions & gotchas

- **Writes are limited to exactly two operations: `post_comment` and `reassign`.**
  Never close, resolve, or transition a ticket — `reassign` sets `assignment_group`
  and deliberately omits `state` from its payload. Do not widen this surface
  without asking the product owner; adding a third write is a product decision,
  not an implementation detail.
- **Approve posts stored text**, it does not re-run the LLM — keep it that way so
  what's written is exactly what the human approved.
- **Verdicts are deterministic.** Add new checks as `@tool` functions in
  `validation_tools.py`; the LLM only routes to them. Mirror new tools in
  `mcp_server.py` with a thin wrapper.
- **All prompts live in `prompts.py`** — keep them there, not inline.
- **Data files** (`data/*.json`, `data/chroma_db/`) are read fresh where noted so
  they can be edited without a restart. `data/chroma_db/` is gitignored.
- **Secrets: never commit `backend/.env`.** If a real key is ever committed, rotate
  it — gitignore does not un-track an already-committed file.
