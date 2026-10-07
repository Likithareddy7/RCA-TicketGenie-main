# TicketGenie

**A customer-facing telecom support assistant, grounded in resolved ServiceNow tickets.**

A customer describes a problem in their own words. TicketGenie retrieves the closest
*resolved* tickets, turns their recorded resolutions into steps the customer can
follow, and asks whether that fixed it. If it did not, it collects the required
details and raises a real incident in ServiceNow.

> **Grounded, not generated.** Every step shown comes from an actual recorded
> resolution. Each retrieved ticket is translated in its own isolated model call, so
> resolutions cannot be mixed across tickets, and the tickets behind any answer can
> be inspected on screen.

---

## Features

- **Hybrid RAG over resolved tickets**: ChromaDB semantic search plus BM25 keyword
  search, fused with Reciprocal Rank Fusion, over the closed-ticket knowledge base.
- **Traceable generation**: one model call per retrieved ticket, each seeing only
  that ticket's resolution, then a merge over the translated steps. Citations are
  validated server side, so a reference to a ticket that was not retrieved is
  stripped before it reaches the screen.
- **Honest refusal**: below a measured relevance floor the model is not called at
  all. Off-topic questions are refused rather than answered with a guess.
- **Steps split by who acts**: what the customer can try, then what the provider
  would do if that does not work.
- **Ticket creation with validation**: collect, verify, review, confirm. Nothing is
  created until every required field is present and the customer has approved the
  completed ticket.
- **Live knowledge base**: reads closed incidents straight from ServiceNow, and
  falls back to a local spreadsheet automatically if the instance is unreachable.
- **MCP server**: the internal validation checks are exposed as Model Context
  Protocol tools for any MCP client.

---

## Architecture

```
ServiceNow                               data/demo_support_tickets.json
   │  closed incidents in the demo         │  the resolved tickets, source of truth
   │  group are the knowledge base         ▼
   ▼                                    make_demo_kb.py ──▶ .xlsx (fallback KB)
Backend, FastAPI (backend/)
   ├─ fallout_engine.py      chat_turn() for customers, recommend() for agents
   ├─ fallout_store.py       ChromaDB + BM25 hybrid search, KB source selector
   ├─ servicenow_client.py   read, create_incident, post_comment, reassign
   ├─ kb_source.py           loads the KB from a spreadsheet
   ├─ prompts.py             all LLM system prompts + user-prompt builders
   ├─ fallout_api.py         /fallout/* endpoints
   ├─ validation_tools.py    deterministic system-state checks (internal path)
   ├─ routing_store.py       Buy Flow redirect rules (internal path)
   ├─ seed_demo_tickets.py   creates the demo group and seeds the tickets
   └─ mcp_server.py          exposes the checks as MCP tools
   ▼
Frontend, React + Vite + Tailwind (frontend/)
   ├─ App.jsx                greeting, then the conversation
   └─ components/            ChatComposer, ChatTurn, RecommendationView, ui
```

### The conversation pipeline (`fallout_engine.chat_turn`)

1. **Retrieve** the closest resolved tickets via hybrid search.
2. **Gate** on relevance. Below `SEARCH_MIN_SIM = 0.40` nothing is usable, the model
   is never called, and the customer is offered a ticket instead.
3. **Translate** each retrieved ticket into steps in its **own** model call, seeing
   only that ticket's resolution.
4. **Merge** those translated steps into one ordered list, split into what the
   customer can try and what the provider would do.
5. **Read the reply**. "That worked" closes the conversation, "still not working"
   offers a ticket, and "raise a ticket" goes straight into collection.
6. **Create** the incident once every required field is validated and confirmed.

> **Internal path:** typing an incident number (`INC0010001`) into the same box
> returns the full agent pipeline instead, with deterministic system-state
> validation, Buy Flow redirect routing, and an approve bar that posts a comment or
> reassigns. Customers never do this; it keeps the agent features reachable without
> a second UI.

> **Writes:** exactly three, all human-gated. `create_incident` after the customer
> confirms, `post_comment` and `reassign` on agent approval. The app never closes,
> resolves, or transitions a ticket.

---

## Tech stack

| Layer | Tech |
| --- | --- |
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Language model | Anthropic Claude (`claude-sonnet-4-5` by default) |
| Embeddings | `all-MiniLM-L6-v2` run **locally** via onnxruntime, no API key needed |
| Retrieval | ChromaDB, `rank_bm25`, fused with Reciprocal Rank Fusion |
| Frontend | React 18, Vite, Tailwind CSS, Axios |
| Protocol | Model Context Protocol (FastMCP) |
| Source | ServiceNow Table API |

---

## Prerequisites

- Python 3.11+ (developed against 3.13)
- Node.js 18+
- An Anthropic API key **with credit on the account**. A valid key with a zero
  balance returns `Your credit balance is too low`, which is a billing state rather
  than a key problem.
- A ServiceNow instance you can write to. A free personal developer instance is
  enough.

---

## Setup & running

### 1. Backend

```bash
cd backend

# create a virtualenv and install deps
python -m venv ../rca           # the repo expects the venv at ../rca
source ../rca/bin/activate      # macOS/Linux
# ..\rca\Scripts\activate       # Windows
pip install -r requirements.txt

# configure environment
cp .env.example .env            # then edit .env with your real values
```

Neither the virtualenv (`rca/`) nor `.env` is in the repository, so a fresh clone
has to create both. `data/chroma_db/` is gitignored too and rebuilds itself on first
boot, which takes about a minute.

**In VS Code:** open the repository folder, then run `Python: Select Interpreter`
from the command palette and pick `./rca/bin/python`, so the editor resolves imports
and the integrated terminal uses the right environment. You will want two terminals,
one for the backend and one for the frontend.

### Running it without a ServiceNow instance

The two knowledge-base spreadsheets ship in the repository, so the app runs with no
instance and no seeding. `.env.example` is already set up this way:

```
KB_SOURCE=spreadsheet
KB_EXCEL_PATH=../data/support_kb.xlsx
```

Fill in `ANTHROPIC_API_KEY`, then skip to step 4. Retrieval and the whole redirect
path need no API key at all, since embeddings run locally, but the customer path
deliberately returns **zero steps** when the model is unavailable rather than quoting
an internal resolution at a subscriber, so without a funded key you get a ticket
offer instead of advice.

Only one spreadsheet can be pinned, so this mode serves either the 52 Business Hub
problems or the 18 modem/voice tickets, not both. Use `KB_SOURCE=servicenow` to
search all 70 together.

### 2. Check what your ServiceNow instance requires

```bash
python discover_required_fields.py      # read-only
```

Reports the mandatory incident fields from both `sys_dictionary` and the data
policies. Data policies are enforced on REST inserts while UI policies are not, so
checking only the dictionary misses genuinely required fields. Update
`REQUIRED_TICKET_FIELDS` in `fallout_engine.py` if your instance demands more.

### 3. Seed the knowledge base

Two groups make up the knowledge base, and each has its own seeder. Both take
`--dry-run`, and both are safe to re-run.

```bash
python seed_demo_tickets.py --dry-run     # show what would be written
python seed_demo_tickets.py               # TICKETGENIE DEMO, 18 modem/voice tickets

python seed_support_tickets.py --dry-run
python seed_support_tickets.py             # the support group, 52 Business Hub problems
```

Each creates its assignment group and the resolved tickets inside it, every one
carrying a labelled block in its `comments` journal, which is where this app reads
structured fields from.

Then switch the KB over to the instance:

```
KB_SOURCE=servicenow
```

A ticket raised in the chat is filed in whichever of the two groups holds the closed
ticket that best matches it, and is classified with that ticket's issue type. The
issue types are written to the incident's native `subcategory`, which only resolves
if the instance has that choice: the seeders add the ones they need.

### 4. Run the API

```bash
uvicorn main:app --port 8000
```

On startup the backend builds the knowledge base from whichever source `KB_SOURCE`
selects. If the build fails, the existing local ChromaDB data is reused.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev                     # Vite dev server on http://localhost:5050
```

CORS in `main.py` allows ports **5050 / 5173 / 5174**. The frontend calls the
backend at `http://localhost:8000` (see `frontend/src/api.js`).

### 6. (Optional) MCP server

```bash
cd backend
python mcp_server.py            # Streamable HTTP at http://127.0.0.1:8765/mcp
python mcp_server.py --stdio    # stdio transport (e.g. for a local desktop client)
```

---

## Environment variables (`backend/.env`)

| Key | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude. Needs credit on the account, not just a valid key |
| `ANTHROPIC_MODEL` | chat model (default `claude-sonnet-4-5`) |
| `SERVICENOW_INSTANCE` | instance base URL |
| `SERVICENOW_USER` / `SERVICENOW_PASSWORD` | ServiceNow account, used by both auth modes |
| `SERVICENOW_CLIENT_ID` / `SERVICENOW_CLIENT_SECRET` | OAuth client. Leave both empty to use Basic auth |
| `SERVICENOW_DEMO_GROUP` | group for the 18 modem/voice tickets, and the fallback when a new ticket's group cannot be derived (default `TICKETGENIE DEMO`) |
| `SERVICENOW_SUPPORT_GROUP` | group for the 52 Business Hub support tickets. Both groups are read into the KB |
| `KB_SOURCE` | `servicenow` or `spreadsheet` |
| `KB_EXCEL_PATH` | the spreadsheet used when `KB_SOURCE=spreadsheet` |
| `SERVICENOW_QUEUE_GROUP` | the group forming the internal agent queue |

See `backend/.env.example` for a template. **`.env` is gitignored, never commit real
credentials.**

> **KB fallback:** `KB_SOURCE=servicenow` falls back to the spreadsheet
> automatically when the instance cannot be read, so an unreachable instance costs
> freshness rather than the ability to answer.

> **Diagnosing a ServiceNow 401:** `invalid_client` means the OAuth client id or
> secret is wrong, whereas `access_denied` means the user credentials are being
> rejected, which on a developer instance usually means the admin password was
> reset. ServiceNow locks an account after six failed attempts, so do not loop a
> script against it.

---

## Key API endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /fallout/chat` | **The main endpoint.** One conversation turn |
| `GET /fallout/recommend/{number}` | Full agent recommendation for one incident |
| `POST /fallout/approve` | Post the approved comment, and reassign on a redirect |
| `POST /fallout/breakdown` | AI breakdown of one historical ticket |
| `POST /fallout/rebuild-kb` | Rebuild the KB from the configured source |
| `GET /fallout/kb-source` | Which spreadsheet backs the KB and how its columns mapped |
| `GET /fallout/tools` | The registered validation tools (schema) |
| `GET /health` | Health + KB size |

`/fallout/chat` is stateless: the client sends the whole transcript each turn, along
with the steps already shown and the fields already captured.

---

## Trying it out

Questions that match the shipped knowledge base well:

- `modem showing solid red light no internet connection`
- `my internet keeps dropping every few hours and the modem randomly reboots`
- `I have no dial tone after my service was installed and the line is dead`
- `calls are cut off exactly at 30 minutes every time`

Name **two symptoms**, the problem and its consequence, for the strongest match.
Very short phrases such as "i have no internet" fall below the relevance floor and
are honestly refused. To see the ticket flow, reply `still not working`, send your
details in one message, check the review table, and confirm.

| Band | Range | Behaviour |
| --- | --- | --- |
| Rejected | < 0.40 | No usable match, the model is not called |
| Weak | 0.40 to 0.55 | Shown with a caution |
| Partial | 0.55 to 0.60 | Shown |
| Strong | >= 0.60 | Treated as reliable grounding |

These thresholds were measured against this knowledge base. **Recalibrate after
changing the KB's source or contents.**

---

## Extending

- **New demo tickets** → add them to `data/demo_support_tickets.json`, run
  `python make_demo_kb.py`, then `python seed_demo_tickets.py`.
- **Different required ticket fields** → edit `REQUIRED_TICKET_FIELDS` in
  `fallout_engine.py`. It is the single place the flow reads its requirements from.
- **New validation check** (internal path) → add one `@tool` function in
  `validation_tools.py` and mirror it with a thin wrapper in `mcp_server.py`.
- **New redirect team** (internal path) → append a rule to
  `data/routing_rules.json`. No code change needed.
- **Prompts** live in `prompts.py`. Keep them there, not inline.

---

## Security

- Never commit `backend/.env`. If a key is ever committed, rotate it, since a
  `.gitignore` entry does not un-track an already-committed file.
- Real customer data (`data/closed tickets.xlsx`, `Comcast_RCA_Final_Dataset.csv`)
  is gitignored. The committed dataset is synthetic.
- The API has no authentication; run it on localhost only. `POST /fallout/chat` and
  `POST /fallout/approve` are write paths into ServiceNow using the configured
  credentials.
- The customer-facing path never shows internal system names or other customers'
  identifiers. This is enforced in the prompts and is a safety rule, not a style
  preference.

---

Design decisions, trade-offs, and the testing behind them are documented in
[CLAUDE.md](CLAUDE.md).
