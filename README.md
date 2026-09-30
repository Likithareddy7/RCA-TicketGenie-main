# RCA-TicketGenie

**A ServiceNow order-fallout remediation assistant for telecom OSS/BSS.**

For every *open* fallout ticket, TicketGenie retrieves the most similar *closed*
tickets, validates the current system state with a deterministic check, and
generates a recommended **Resolution** and **Remediation**. A human agent reviews
it and, on approval, the exact recommendation is posted **as a comment** on the
ServiceNow ticket.

> **Recommend-only + human-in-the-loop.** The app never closes, resolves, or
> transitions a ticket — the only write it performs is posting a comment. On
> approval it posts the *exact* text the agent saw/edited; the LLM is not re-run.

---

## Features

- **Hybrid RAG over historical tickets** — ChromaDB semantic search + BM25 keyword
  search, fused with Reciprocal Rank Fusion, over the closed-ticket knowledge base.
- **Deterministic validation** — an LLM *routes* to a system-state check, but the
  check runs deterministically and produces the verdict (no hallucinated pass/fail).
- **Two-part output** — a history-grounded *Resolution* (always) and a
  validation-driven *Remediation* (when a tool applies).
- **Live knowledge base** — the KB syncs incrementally with ServiceNow on every
  queue load (only changed tickets re-embed); no restart needed.
- **Human-in-the-loop UI** — review, edit, and approve before anything is posted.
- **MCP server** — the same validation checks are exposed as Model Context
  Protocol tools for any MCP client.

---

## Architecture

```
ServiceNow (incidents)
   │  read incidents; parse the labelled block inside the `comments` journal field
   ▼
Backend — FastAPI (backend/)
   ├─ servicenow_client.py   read + post_comment (never closes/resolves)
   ├─ fallout_store.py       ChromaDB + BM25 hybrid search over CLOSED tickets (the KB)
   ├─ fallout_engine.py      recommend(): retrieve → route tool → validate → generate → decide
   ├─ validation_tools.py    @tool registry — deterministic system-state checks
   ├─ provisioning_store.py  mock OMS inventory (data/provisioning_data.json)
   ├─ prompts.py             all LLM system prompts + user-prompt builders
   ├─ fallout_api.py         /fallout/* endpoints
   ├─ fake_tickets.py        synthetic demo tickets (data/fake_tickets.json)
   └─ mcp_server.py          exposes the checks as MCP tools
   ▼
Frontend — React + Vite + Tailwind (frontend/)
   ├─ App.jsx                state container (queue → recommend → approve)
   └─ components/            QueueSidebar, RecommendationView, SimilarTicket, ui
```

### The recommendation pipeline (`fallout_engine.recommend`)

1. **Retrieve** top-K similar *closed* tickets via hybrid search.
2. **Route** — an LLM reads how those tickets were resolved + the available tools,
   and picks which validation tool to run and which identifiers to pull.
3. **Validate** — the chosen tool runs deterministically →
   `confirmed` / `ambiguous` / `not_confirmed`, or `not_applicable` when no tool
   fits but historical grounding is strong.
4. **Generate** — a *Resolution* (history-only, from the top matches; always) and
   a *Remediation* (validation-driven; only when a tool ran).
5. **Decide** — `recommend` vs `needs_review`, with a confidence level.
   (Retrieval confidence gate: `SIM_STRONG = 0.60`.)

> **ServiceNow note:** native incident fields are not populated on this instance —
> every structured field lives as a labelled text block inside the `comments`
> journal field, which `servicenow_client` parses generically. Fallout tickets are
> matched by `short_descriptionLIKEfallout`.

---

## Tech stack

| Layer     | Tech |
| --------- | ---- |
| Backend   | Python, FastAPI, Uvicorn |
| AI/RAG    | OpenAI (`gpt-4o` + `text-embedding-3-small`), ChromaDB, `rank_bm25` |
| Frontend  | React 18, Vite, Tailwind CSS, Axios |
| Protocol  | Model Context Protocol (FastMCP) |
| Source    | ServiceNow Table API |

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- An OpenAI API key
- ServiceNow instance + credentials (Table API access)

---

## Setup & running

### 1. Backend

```bash
cd backend

# create a virtualenv and install deps
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt

# configure environment
cp .env.example .env          # then edit .env with your real keys

# run the API (http://localhost:8000)
uvicorn main:app --port 8000
```

On startup the backend builds the knowledge base from ServiceNow. If ServiceNow is
unreachable, it falls back to the existing local ChromaDB data.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev                   # Vite dev server on http://localhost:5050
```

CORS in `main.py` allows ports **5050 / 5173 / 5174**. The frontend calls the
backend at `http://localhost:8000` (see `frontend/src/api.js`).

### 3. (Optional) MCP server

```bash
cd backend
python mcp_server.py            # Streamable HTTP at http://127.0.0.1:8765/mcp
python mcp_server.py --stdio    # stdio transport (e.g. for a local desktop client)
```

---

## Environment variables (`backend/.env`)

| Key | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI chat + embeddings |
| `OPENAI_MODEL` | chat model (default `gpt-4o`) |
| `OPENAI_EMBED_MODEL` | embedding model (default `text-embedding-3-small`) |
| `SERVICENOW_INSTANCE` | instance base URL |
| `SERVICENOW_USER` / `SERVICENOW_PASSWORD` | ServiceNow Table API basic auth |

See `backend/.env.example` for a template. **`.env` is gitignored — never commit
real credentials.**

---

## Key API endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /fallout/tickets` | Open queue + closed KB (syncs the KB on load) |
| `GET /fallout/recommend/{number}` | Generate a recommendation for an open ticket |
| `POST /fallout/breakdown` | AI breakdown of one historical ticket |
| `POST /fallout/approve` | Post the approved comment to ServiceNow (never closes) |
| `GET /fallout/tools` | The registered validation tools (schema) |
| `POST /fallout/rebuild-kb` | Full KB rebuild from ServiceNow |
| `GET /health` | Health + KB size |

---

## Extending

- **New validation check** → add one `@tool` function in `validation_tools.py`
  (deterministic; returns a verdict). Mirror it with a thin wrapper in
  `mcp_server.py`. The LLM router discovers it automatically.
- **New fallout type** → usually needs *no* parser change; the comment-block parser
  is generic (`Label: value` → fields, bare `Label:` → sections).
- **Prompts** live in `prompts.py` — keep them there, not inline.

---

## Security

- Never commit `backend/.env`. If a key is ever committed, rotate it — a
  `.gitignore` entry does not un-track an already-committed file.
- The API has no authentication; run it on localhost only. `POST /fallout/approve`
  is a write path into ServiceNow (comment-only) using the configured credentials.
