# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

**TicketGenie** is a **customer-facing telecom support assistant**. A customer
describes a problem in their own words, the app retrieves the most similar *resolved*
tickets, and it replies with steps to try. If the steps work, the conversation closes.
If the customer is stuck, or the fix needs an engineer, it offers to raise a ticket or
pass them to a representative. Raising a ticket collects every required field,
validates them, shows the completed ticket for review, and only then creates the
incident in ServiceNow.

An **internal path** is retained behind the same input: typing a bare incident number
(`INC0010020`) returns the full order-fallout recommendation pipeline, including the
deterministic validation checks, Buy Flow redirect routing, and the approve bar that
posts a comment or reassigns. Customers never type an incident number; this keeps the
ServiceNow comment and reassignment features reachable without a separate UI.

> History note: this began as a Jira + LangGraph triage app, became a ServiceNow
> order-fallout remediation assistant for internal agents, and is now customer facing.
> The order-fallout machinery still exists and works, reached by incident number.
> Anything referencing Jira, LangGraph, voice, or Whisper has been removed.

## Architecture

```
ServiceNow                        data/demo_support_tickets.json
   │  closed incidents in the        │  18 resolved support tickets, the source
   │  demo group (KB_SOURCE          │  of truth, authored by the product owner
   │  =servicenow), plus the         │
   │  internal open queue            ▼
   │                              make_demo_kb.py -> demo_support_tickets.xlsx
   ▼                                 │  (KB_SOURCE=spreadsheet, and the fallback)
Backend (FastAPI, backend/)          ▼
   ├─ servicenow_client.py   read, post_comment, reassign, create_incident
   ├─ kb_source.py           loads the KB from the spreadsheet
   ├─ fallout_store.py       ChromaDB + BM25 hybrid search; KB_SOURCE selector
   ├─ fallout_engine.py      chat_turn() for customers, recommend() for agents
   ├─ validation_tools.py    @tool registry, deterministic system-state checks
   ├─ routing_store.py       deterministic redirect rules (data/routing_rules.json)
   ├─ provisioning_store.py  LOC-/SVC-keyed inventory
   ├─ oms_store.py           BAN-/order-keyed BOSS OMS inventory
   ├─ prompts.py             all LLM system prompts and user-prompt builders
   ├─ fallout_api.py         /fallout/* endpoints
   ├─ fake_tickets.py        synthetic demo tickets for the internal queue
   ├─ make_demo_kb.py        JSON dataset -> KB spreadsheet
   ├─ seed_demo_tickets.py   creates the demo group and seeds the 18 as closed
   ├─ discover_required_fields.py   reads mandatory incident fields from an instance
   └─ mcp_server.py          exposes the validation checks as MCP tools
   ▼
Frontend (React + Vite + Tailwind, frontend/)
   ├─ App.jsx                      greeting, then the conversation
   ├─ components/ChatComposer.jsx  the single input, Enter sends
   ├─ components/ChatTurn.jsx      bubbles, steps, ticket fields, quick replies
   ├─ components/RecommendationView.jsx  internal agent view (incident numbers)
   ├─ components/SimilarTicket.jsx       expandable historical match
   └─ components/ui.jsx                  shared presentational primitives
```

## The customer conversation (`fallout_engine.chat_turn`)

Stateless. The client sends the whole transcript on every turn, so there is no session
store to keep in sync.

**Control flow is deterministic; the model only writes prose.** Intent comes from the
quick-reply button the customer pressed, never from asking a model to interpret free
text. So "that fixed it" cannot be misread as "I am stuck", and the conversation keeps
working when the model is unavailable. Free text is always treated as describing a
problem, which is the safe default.

Stages the client renders:

| Stage | Meaning |
| --- | --- |
| `troubleshoot` | Resolutions found, grouped by source ticket. No buttons: the reply is read |
| `no_match` | Nothing close enough in the KB. Offers a ticket or a rep |
| `escalate` | Customer is stuck, or the fix needs our side. Offers a ticket or a rep |
| `resolved` | Customer confirmed it is fixed. Conversation closed |
| `ticket_collect` | Listing the fields needed to raise a ticket |
| `ticket_missing` | Naming exactly which fields are still outstanding |
| `ticket_review` | The completed ticket, shown for approval before creation |
| `ticket_created` | The incident exists. Shows its real number |
| `ticket_failed` | Creation failed. Nothing was created |
| `ticket_lookup` | An incident number was typed. Returns the internal pipeline |

Actions: `worked`, `stuck`, `open_ticket`, `talk_to_rep`, `follow_up`,
`ticket_details`, `confirm_ticket`, `cancel_ticket`.

**After resolutions are shown there are no buttons.** The customer's reply is read by
`_followup_intent()`, which returns `resolved`, `wants_ticket`, or `unresolved`. An
unambiguous keyword pass runs first, free and instant, and the model is consulted only
for genuinely ambiguous wording. Ticket markers are checked **before** resolved markers,
so "no thanks, just raise a ticket" is not mistaken for gratitude. A reply such as
"No, I want to create a new ticket" goes straight into collection without being offered
a ticket again. Anything unclear resolves to `unresolved`, because continuing to help
someone already fixed is a small annoyance while closing the conversation on someone
still broken is not.

### Generation is per ticket, not blended

The reply is built from **one model call per retrieved ticket**, each seeing only that
ticket's own resolution.

The earlier design put all three retrieved resolutions into a single prompt and asked
for one merged set of steps, and did not even include the ticket numbers in the
context. Steps therefore could not be traced to a source and ran together across
tickets, which is exactly what the product owner reported. Isolating the calls makes
cross-contamination **structurally impossible** rather than merely discouraged. It
costs three calls instead of one and buys a guarantee.

Everything around the steps is written by the application, never the model:

* the opening line is the fixed string `RESOLUTION_HEADER`
* the closing line is the fixed string `RESOLUTION_QUESTION`

That is why no apology, sympathy or filler can appear. The per-ticket prompt also
forbids greetings, sign-offs and commentary outright.

Each group carries its own citation (ticket number, similarity, issue type, cause), and
the expandable sources panel shows the raw ticket text underneath, so a reader can
compare the generated steps against the recorded resolution directly.

### Retrieval ordering was evaluated, not assumed

`search()` fuses semantic and keyword results with Reciprocal Rank Fusion, then sorts
the merged list by **true cosine similarity** rather than by the RRF score. Both
orderings were measured against all 18 tickets: each returns the exactly correct
ticket first for 17 of 18 queries, and they agree on every single query. Cosine
ordering is kept because the relevance thresholds are calibrated against cosine.

### No verbatim fallback in the customer path

The agent-facing paths quote an engineer's recorded close notes when the model is
unavailable, which is a good fallback there. **The customer path must not do this.**
The knowledge base was written by engineers for engineers, so quoting it at a
subscriber would leak internal system names and other customers' identifiers, and
would instruct them to do things only an employee can do. When the model is
unavailable the customer path returns **zero steps** and routes to a ticket or a rep.
This is enforced in `_customer_steps()` and is a safety rule, not a style choice.

## Ticket creation (the third write operation)

The write surface was deliberately two operations, `post_comment` and `reassign`. The
product owner has signed off on a **third**, incident creation, for the case where the
knowledge base has no answer.

**Requirements, as stated by the owner and implemented literally:**

1. The customer can give every detail in a single message.
2. The system verifies whether everything required is present.
3. Anything missing is named **explicitly, field by field**, and asked for again.
4. Nothing is ever assumed or auto-populated.
5. The completed ticket is shown back for review **before** anything is created.
6. The incident is created only once every required field is present and validated.

**Guards:**

* Validation runs **again on confirm**, rather than trusting what the client sends
  back, for the same reason `/fallout/approve` re-derives its reassignment target
  server side. A stale or tampered client cannot skip a check.
* `state` is stripped from the create payload no matter what a caller passes, so this
  client still never transitions a ticket.
* `assignment_group` is read back after the write, because ServiceNow silently
  **drops** a group name it cannot resolve. Without the read-back a typo produces an
  unassigned ticket that looks like a success.
* Creation falls back to `SERVICENOW_DEMO_GROUP` whenever a group cannot be derived,
  so a bug cannot spray tickets into a live queue.

### Which group a new ticket is filed in

The knowledge base spans more than one ServiceNow group, so a new ticket is filed
next to the tickets that answered it. Without this, a Business Hub question answered
from the support knowledge base raised its ticket in the modem queue.

`_ticket_group()` in `fallout_engine.py` decides, and three things about it are
deliberate:

* **The group comes from the single best-matching ticket, not a majority of the
  matches.** Both rules were measured over 20 phrasings. They disagree on two, "the
  caller ID is showing the wrong name" and "I returned a VOIP phone but I am still
  being charged", and on both the top match is the correct one while a majority vote
  files the ticket in the wrong group. Each ticket carries its owning group as
  `kb_group` in Chroma metadata, stamped in `fetch_demo_closed_kb()`.
* **Routing reads the ticket's own short description**, padding a terse one with the
  customer's opening message. It must *not* reuse `_retrieval_query()`: by the time a
  ticket is confirmed the latest message is a block of contact details, and the first
  version of this function pulled names, account numbers and email addresses into the
  retrieval query. That was caught by the stubbed test for the no-match fallback,
  which is the only case where the noise changed the answer.
* **The group is re-derived server side**, never carried by the client, for the same
  reason validation re-runs on confirm. A tampered client must not pick the queue.

When nothing clears `SEARCH_MIN_SIM` there is no matched ticket to learn from, so the
ticket goes to `SERVICENOW_DEMO_GROUP`. Those are exactly the tickets a human has to
triage from scratch, and the product owner chose to keep them in the group already
being watched rather than mixing them into the support queue.

`kb_group` is part of the `_doc_hash()` basis, so moving a ticket between groups
re-upserts it and the routing metadata follows. Adding the field forced a one-time
re-upsert of all 70 tickets, which is how the metadata was populated without
deleting `data/chroma_db/`.

The group is **not** shown in the UI. It is an internal queue name, so it only
appears in the backend log line `[CHAT] routing new ticket to '<group>' (top match
<incident> at <score>)`.

**Required fields** live in one place, `REQUIRED_TICKET_FIELDS` in
`fallout_engine.py`. Six fields: a short description, four personal details, and the
type of issue.

### Type of issue, derived from the knowledge base

The field used to be "Which service is affected", filled by keyword matching into
`internet` / `phone` / `both`. It is now **"Type of issue"**, and the value is the
issue type recorded on the closed ticket that best matches what the customer wrote.
It uses the same `_best_kb_match()` helper as group routing, so the two can never
disagree about which ticket a new one is modelled on.

The KB holds **18 issue types**, and they are what `subcategory` already means in
Chroma metadata for both groups:

* 4 from the modem/voice tickets: Modem Connectivity, Modem Stability, Voice Quality,
  Voice Line Activation
* 14 from the Business Hub tickets: Access/Login/Permission Issue, Billing Issue,
  Order Issue, Configuration Issue, Data Issue, Application Issue, Customer Account
  Issue, Disconnecting a Customer/Service Issue, Plan Change Issue, Functionality
  Issue, Order Fallout, Error Message, Disconnected but Billed, Other

**The customer is never asked for it.** Those 18 are internal classification labels,
so putting that list to a subscriber would be nonsense. It therefore never blocks
creation: when nothing clears `SEARCH_MIN_SIM` it reads `ISSUE_TYPE_UNKNOWN`, which
is the literal string "Not yet classified", chosen by the product owner over hiding
the row so the ticket tells the team that triage is outstanding. A value the customer
labels themselves still wins, as with every other field.

### Writing the classification to ServiceNow

The derived type is written to the incident's native `subcategory`, so a ticket lands
classified rather than needing a human to set it. Two instance facts make this
less simple than it sounds, and both are read from `sys_choice` rather than hardcoded:

* **ServiceNow silently drops a subcategory that is not in the choice list.** No 400,
  no error, the field is just empty afterwards. So `classify()` writes a value only
  when the instance actually has that choice, and `create_incident` reads
  `subcategory` back and warns when it did not stick, exactly as it already does for
  `assignment_group`.
* **Subcategory is a dependent choice**, so a value only resolves under its own
  category. `classify()` therefore returns the choice's own `dependent_value` as the
  category rather than assuming one. The two groups genuinely differ: the Business Hub
  tickets sit under `Marketing, Sales & Billing Applications`, the modem/voice tickets
  under `inquiry` (displayed "Inquiry / Help").

The 14 Business Hub types were already valid choices from the seeding work. The 4
modem/voice types were **not**, and were added as choices dependent on `inquiry`.
Without that, a modem ticket's classification would have been dropped silently while
a Hub ticket's stuck, which is the kind of split behaviour that is painful to debug.

Verified live, both directions, by reading the created incidents back out of the
instance:

```
INC0010078  New  BUS Sales Ordering and Digital Support  Marketing, Sales & Billing Applications  Access/Login/Permission Issue
INC0010079  New  TICKETGENIE DEMO                        Inquiry / Help                           Modem Connectivity
```

In the ticket description the type of issue gets **its own line**, not a row under
`Contact details:`, because it is a classification rather than a contact detail.

`discover_required_fields.py` was run against `dev449716` and found that **ServiceNow
itself requires nothing at create time**: no dictionary-mandatory fields on `incident`
or on `task`. The only enforced rule is a data policy, "Make close info mandatory when
resolved or closed", which applies to REST and requires `close_code` and `close_notes`,
but only when a ticket is being closed. `seed_demo_tickets.py` already sends both when
it closes the seeded tickets.

So the six fields the chat shows are a **business choice, not a ServiceNow
constraint**, and the list can be trimmed freely. Re-run the script against any new
instance before assuming this still holds. It reads both `sys_dictionary` (`mandatory=true`) and `sys_data_policy2`,
because **data policies are enforced on REST inserts** while UI policies are not, so
checking only the dictionary misses genuinely required fields.

**Field extraction** prefers values the customer labelled themselves
("Account: 4471829"), parsed deterministically, and only calls the model for
free-form text. A labelled value is authoritative and the model never overwrites it.
The extraction prompt is forbidden from inferring, deriving, or completing any value:
an absent field comes back null and the flow asks again.

## Knowledge base

The demo KB is the **18 resolved support tickets** in
`data/demo_support_tickets.json`, supplied by the product owner and stored verbatim.
Six error codes (`MODEM-001` to `003`, `VOICE-001` to `003`), three tickets each, four
task types.

`KB_SOURCE` selects where the KB is read from:

* `spreadsheet` (default) reads the file pinned by `KB_EXCEL_PATH`. Works with no
  ServiceNow instance at all.
* `servicenow` reads closed incidents in every group `kb_groups()` returns
  (`SERVICENOW_DEMO_GROUP` and `SERVICENOW_SUPPORT_GROUP`), so what is visible in the
  ServiceNow UI is literally what gets searched. Each ticket keeps its owning group
  as `kb_group`, which is what new-ticket routing reads.

When `servicenow` is selected but the instance cannot be read, it **falls back to the
spreadsheet** rather than serving an empty KB. That is deliberate: an unreachable
instance should cost freshness, not the assistant's ability to answer.

Two things about seeding this instance are not obvious and cost real time:

* **The close code must be a value from the instance's own choice list.** A data
  policy makes `close_code` mandatory when closing, and ServiceNow rejects an invalid
  value with `403 Data Policy Exception: The following fields are mandatory:
  Resolution code`, which reads as though the field were omitted rather than wrong.
  This instance has no "Solved (Permanently)"; it uses "Solution provided" and nine
  others. Check `sys_choice` for `name=incident^element=close_code` before assuming.
* **A journal write to a CLOSED incident is silently dropped.** The PATCH returns 200
  and `sys_journal_field` gains no row. So the labelled block must be posted BEFORE
  closing, and repairing a closed ticket means reopening it, posting, then closing
  again. The seeder does exactly that, and verifies the block landed rather than
  trusting the 200.

`make_demo_kb.py` converts the JSON into a spreadsheet whose headers match the
mapping already pinned in `data/kb_column_map.json`, so switching KB files needs no
mapping change. `seed_demo_tickets.py` seeds the same tickets into ServiceNow, writing
a **labelled block** into the `comments` journal because that is where this app reads
structured fields from. All 18 have been verified to round-trip back through
`parse_ticket()` with the correct subcategory, resolution code, description and
resolution notes.

### Embedding policy

Problem-side only: `short_description + description + subcategory + service_type`.
The **resolution is never embedded**, since it is the answer and embedding it would
break query/document symmetry. Everything else rides in Chroma metadata and is used
after retrieval.

### Relevance thresholds, measured not guessed

| Band | Range | Behaviour |
| --- | --- | --- |
| Rejected | < 0.40 | No usable match. The model is not called at all |
| Weak | 0.40 to 0.55 | Shown with a caution |
| Partial | 0.55 to 0.60 | Shown |
| Strong | >= 0.60 | `SIM_STRONG`, shared with the internal pipeline |

Measured against the 18-ticket KB with 22 realistic customer phrasings:

* On topic: **0.524 to 0.817**, mean 0.679. **21 of 22 retrieved the exactly correct
  error code first**; the miss ("my modem keeps restarting over and over") is
  genuinely ambiguous between MODEM-001 and MODEM-002 and still had the right code in
  its top three.
* Zero domain bleed: no modem question returned a voice ticket, and none the reverse.
* Off topic: 0.007 to 0.093. Closest near-miss, "can I cancel my service", 0.349.

That first calibration produced a floor of 0.45, **and it was wrong**. The way it was
wrong is worth recording, because it is easy to repeat: every phrasing in that set was
fairly specific, while real customers type something short and generic, which scores
much lower against long specific documents. "my internet is not working" scores 0.467,
"i have no internet" 0.372 and "my line is dead" 0.361, so a 0.45 floor refused real
customers while the calibration set looked perfectly healthy.

A second pass measured 15 short generic customer phrasings against 10 off-topic ones
(billing, cancellation, opening hours, nonsense):

* At **0.36**, all 15 on-topic queries are accepted and all 10 off-topic ones refused.
* At 0.45, only 11 of 15 on-topic queries are accepted.
* The nearest false positive is "can I cancel my service" at 0.349, so the margin is
  real but thin, roughly 0.012. Above 0.38, genuine problems start being refused.

A third pass, against the live **ServiceNow-backed** KB rather than the spreadsheet,
moved the numbers again, because the text parsed out of the journal differs slightly
from the spreadsheet rows. The decisive case: **"I want to cancel my service" scores
0.361 there**, so a 0.36 floor answered a cancellation request with modem
troubleshooting steps. Across 15 on-topic and 14 off-topic phrasings, every floor from
0.38 to 0.46 gives zero false accepts.

The floor is therefore **0.40**, sitting 0.04 above the highest off-topic score and
0.06 below the lowest on-topic score it accepts. The cost is that two very terse
phrasings, "my line is dead" (0.354) and "i have no internet" (0.374), fall below it
and get the honest "I could not find anything" plus a ticket offer. That is a safe
answer; troubleshooting a cancellation request is not.

**Recalibrate whenever the KB source or contents change.** It has moved three times.

## The internal pipeline (`fallout_engine.recommend`)

Reached by typing an incident number. Unchanged:

0. **REDIRECT pre-route**, deterministic, before anything else. `routing_store`
   matches `data/routing_rules.json`; a Buy Flow hit short-circuits the pipeline into
   "reassign to the owning team", with no LLM involved. "Every Buy Flow ticket goes to
   the Buy Flow team" is a business rule, so it must not depend on retrieval quality.
1. **RETRIEVE** top-K similar closed tickets via hybrid search.
2. **ROUTE** an LLM router picks which validation tool to run and which identifiers to
   pull.
3. **VALIDATE** the chosen tool runs deterministically and returns a verdict
   (`confirmed` / `ambiguous` / `not_confirmed`, or `not_applicable` when no tool fits
   but grounding is strong).
4. **GENERATE** a Resolution (history only) and a Remediation (validation driven).
5. **DECIDE** `recommend` vs `needs_review`, with a confidence level.

### Two remediation families (two id schemes)

| Family | Ids | Store | Tools |
| --- | --- | --- | --- |
| Duplicate service / porting | `LOC-`, `SVC-` | `provisioning_store` | `provisioning.check_active_service` |
| BOSS OM (order + account) | BAN, order id, `OMTASK` | `oms_store` | `oms.check_account_status`, `oms.check_order_status`, `oms.check_network_type` |

### Redirect / reassignment (Buy Flow)

Approving a redirect genuinely **moves** the ticket. `/fallout/approve` reassigns
first, then posts the comment, so a failed move never leaves a comment claiming
success. The destination is **re-derived server side** from the routing rules on
approve, so a client cannot move a ticket to a team the rules do not sanction.

### ServiceNow data quirk (important)

On the BOSS OM instance, native incident fields are NOT populated. Every structured
field lives as a labelled text block inside the `comments` journal field.
`_canonical_block()` picks the newest journal entry containing both `Location ID:` and
`Description:`; `_parse_block()` is generic (`Label: value` becomes a field, bare
`Label:` starts a section), so new fallout types need no parser change. Comments this
app posted itself are excluded, otherwise approving a ticket poisons its own source.

## Running

Backend (from `backend/`):

```bash
uvicorn main:app --port 8000
```

Frontend (from `frontend/`, Vite dev server on **5050**):

```bash
npm install
npm run dev
```

CORS in `main.py` allows ports 5050 / 5173 / 5174. The frontend calls the backend at
`http://localhost:8000` (`frontend/src/api.js`).

Setting up a fresh instance, in order:

```bash
cd backend
../rca/bin/python discover_required_fields.py     # read-only; update REQUIRED_TICKET_FIELDS
../rca/bin/python seed_demo_tickets.py --dry-run  # inspect what would be written
../rca/bin/python seed_demo_tickets.py            # create the group and the 18 tickets
```

Then set `KB_SOURCE=servicenow` to read the KB live from the instance.

## Configuration (`backend/.env`)

| Key | Purpose |
| --- | --- |
| `LLM_PROVIDER` | `gemini` (the default) or `anthropic` (Claude) |
| `GEMINI_API_KEY` | Gemini key from Google AI Studio. No Google Cloud project needed |
| `GEMINI_MODEL` | Gemini model id. Verify it with `list_gemini_models.py` |
| `ANTHROPIC_API_KEY` | Claude, used only when `LLM_PROVIDER=anthropic`. Needs **credit on the Console org**, not just a valid key |
| `ANTHROPIC_MODEL` | Claude chat model |
| `SERVICENOW_INSTANCE` | instance base URL |
| `SERVICENOW_USER` / `SERVICENOW_PASSWORD` | ServiceNow account, used by both auth modes |
| `SERVICENOW_CLIENT_ID` / `SERVICENOW_CLIENT_SECRET` | OAuth client, **preferred**; unset both to fall back to Basic |
| `SERVICENOW_QUEUE_GROUP` | assignment group forming the internal queue |
| `SERVICENOW_DEMO_GROUP` | group for the 18 modem/voice demo tickets, and the fallback when a new ticket's group cannot be derived |
| `SERVICENOW_SUPPORT_GROUP` | group for the Business Hub support tickets. Both groups are read into the KB by `kb_groups()` |
| `KB_SOURCE` | `spreadsheet` or `servicenow` |
| `KB_EXCEL_PATH` | the KB spreadsheet when `KB_SOURCE=spreadsheet` |
| `KB_EXCEL_SHEET` | optional sheet name |

### ServiceNow auth: use OAuth

ServiceNow ships **Basic Auth API Restriction**
(`glide.authenticate.basic_auth.restriction.*`). When active, Basic auth is blocked
for REST while UI login keeps working, so an instance looks perfectly healthy in a
browser while every API call returns 401. `servicenow_client` therefore prefers OAuth
and falls back to Basic only when no client is configured. It uses the **password
grant** deliberately, so the token is bound to a real user and writes are attributed
to that account in the audit trail. Tokens are cached until a minute before expiry,
and a 401 triggers exactly one refresh-and-retry.

**Diagnosing a 401:** `invalid_client` means the client id or secret is wrong.
`access_denied` means the **user credentials** are being rejected, which on a personal
developer instance usually means the admin password was reset when the instance was
reclaimed. Note that ServiceNow locks an account after six failed attempts, so do not
loop a script against it while testing.

### The LLM provider is one function

Every LLM call in `fallout_engine.py` has the same shape, one system prompt and one
user prompt returning text, so all eight call sites route through `_chat()`. The
provider therefore lives entirely in that one block and nothing else in the engine
knows which model is in use. Swapping provider means editing `_chat()`, `_get_client()`
and `MODEL`, and nothing more.

`LLM_PROVIDER` selects between **Gemini** (the default) and Claude. Gemini goes
through the Gemini Developer API, which authenticates with a plain API key from
Google AI Studio, so there is no Google Cloud project, no `gcloud` and no service
account. Claude is kept reachable rather than deleted so a missing or rejected key
cannot leave the app with no working model at all. Moving to Vertex AI later would
be a change to `_get_client()` alone, swapping the key for
`genai.Client(vertexai=True, project=..., location=...)`.

Four things about the Gemini path are deliberate:

* **The client is built lazily.** The old code constructed the Anthropic client at
  import time, which would raise during import when the provider is unconfigured and
  take the whole backend down,
  including the retrieval and redirect paths that need no model at all.
* **`resp.text` raises rather than returning empty** when a candidate is blocked by a
  safety filter or finishes without content. That is a real difference from Anthropic,
  so `_gemini_text()` normalises the outcome to either a non-empty string or a raised
  error, and never to a silent empty string that would be parsed as a valid reply. It
  recovers partial parts where a candidate has them.
* **The system prompt moves into the config** as `system_instruction`, and
  `max_tokens` becomes `max_output_tokens`. The app uses budgets from 60 to 1200.
* **No tool-use translation was needed.** The `@tool` decorator in
  `validation_tools.py` is local to this repo, not a provider API, and the schemas
  reach the router as text inside a prompt via `build_router_user_prompt()`. Likewise
  `_parse_json()` already slices between the first `{` and the last `}`, so a model
  that wraps JSON in markdown fences is handled without change.

Measured with the provider unconfigured: retrieval still runs, step generation fails
per ticket, and the turn comes back as `escalate` with **zero steps** and a ticket or
rep offer. That is the required safety behaviour, not a degraded guess.

`list_gemini_models.py` lists the models a key can actually call and warns when
`GEMINI_MODEL` is not among them. Model ids are retired on Google's own schedule, so
the configured id is worth verifying rather than trusting: `gemini-2.5-flash` was
already past its stated retirement date when this was written.

### Embeddings need no key

Anthropic has no embeddings endpoint, so the vector side runs locally:
`all-MiniLM-L6-v2` through `onnxruntime` via Chroma's `DefaultEmbeddingFunction`. The
model caches under `~/.cache/chroma` on first use and works offline after that. So
**retrieval, BM25, and the whole redirect path work with no API key at all**; only the
written prose needs Claude.

Local embeddings are **384-dimensional** where OpenAI's were 1536. Chroma pins
dimension at collection-creation time, so switching embedders requires deleting
`data/chroma_db/` and rebuilding. A mismatched collection fails to query rather than
silently degrading.

## Testing

Dry tests are run against stubs, so nothing is written to any external system.

**Ticket creation flow, 28 assertions, all passing:**

* `open_ticket` asks for the 4 personal details and creates nothing. The short
  description comes from the customer's own words and the type of issue is derived,
  so neither is asked for.
* Partial details produce `ticket_missing` naming **exactly** the outstanding fields,
  and no email or phone is invented.
* Confirming while incomplete is **blocked** and creates nothing.
* The review stage shows all fields and still creates nothing.
* Confirm creates **exactly one** incident, with `short_description` set to the
  customer's summary and the other fields carried in the labelled description.
* Cancel creates nothing.
* Off-topic input is refused and never turned into advice.

**Seeding round trip:** all 18 tickets were fed through `parse_ticket()` as the app
would read them back from ServiceNow, and every one recovered the correct
short_description, subcategory, resolution code, description and resolution notes.

**KB fallback:** with `KB_SOURCE=servicenow` and the instance unreachable, the KB
falls back to the spreadsheet and returns all 18 tickets rather than going empty.

**Similarity:** 22 phrasings, numbers recorded in the thresholds section above.

## Known blockers

* **Anthropic key: resolved.** Worth recording how, because the symptom was
  misleading. A working key was pasted into `.env` with a **space inserted in the
  middle** (107 characters instead of 106), which produced `API key is invalid`.
  Before that, a genuinely unfunded key produced `Your credit balance is too low`.
  Those two errors mean different things: the credit error means the key authenticated
  fine and the organisation has no funds, so issuing a new key on that organisation
  changes nothing. Check the key's length and for stray whitespace before assuming
  anything else.
* **Synthetic demo tickets are disabled.** `data/fake_tickets.json` defines
  INC0010018 to INC0010027, which **collide** with the real seeded incidents
  INC0010001 to INC0010019. `get_ticket()` checks the synthetic set first, so
  INC0010018 resolved to a Buy Flow ticket instead of the real Voice Quality one. The
  file is set to `enabled: false` rather than deleted. Do not re-enable it while the
  seeded tickets exist.
* **ServiceNow instance: resolved.** A replacement developer instance
  (`dev449716`) is configured and reachable. The OAuth client from the previous
  instance does not exist on it, so `SERVICENOW_CLIENT_ID` and
  `SERVICENOW_CLIENT_SECRET` are cleared and the client falls back to Basic auth,
  which works there. Register an OAuth client and set both values if Basic auth is
  ever restricted on this instance.

## Conventions and gotchas

- **Writes are limited to exactly three operations: `post_comment`, `reassign`, and
  `create_incident`.** Never close, resolve, or transition a ticket. `reassign` sets
  `assignment_group` only, and `create_incident` strips `state`. Do not widen this
  surface without asking the product owner; adding a write is a product decision, not
  an implementation detail.
- **Approve posts stored text**, it does not re-run the LLM, so what is written is
  exactly what the human approved.
- **Verdicts are deterministic.** Add new checks as `@tool` functions in
  `validation_tools.py`; the LLM only routes to them. Mirror new tools in
  `mcp_server.py` with a thin wrapper.
- **All prompts live in `prompts.py`**, not inline.
- **Conversation intent comes from buttons**, not from model interpretation of free
  text. Keep it that way: it is what makes the flow survive an LLM outage.
- **Never show the customer an unrewritten internal resolution.** See the safety note
  above.
- **Data files** (`data/*.json`, `data/chroma_db/`) are read fresh where noted so they
  can be edited without a restart. `data/chroma_db/` is gitignored.
- **Do not add tickets or scenarios to the dataset without asking the owner.**
- **Secrets: never commit `backend/.env`.** If a real key is committed, rotate it.
  A gitignore entry does not un-track an already-committed file. Real customer data
  (`data/closed tickets.xlsx`, `Comcast_RCA_Final_Dataset.csv`) is gitignored for the
  same reason.
