# Use the OS (Windows) certificate store for TLS so corporate proxy / SSL-inspection
# root CAs are trusted. Must run before any HTTPS client (openai, chromadb, requests)
# is constructed.
import truststore
truststore.inject_into_ssl()

import os
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager
from openai import OpenAI
from dotenv import load_dotenv

import data_manager
import fallout_store
from fallout_api import router as fallout_router

load_dotenv()


def _openai_client():
    """Lazily build the OpenAI client, used ONLY by the vestigial /transcribe
    endpoint (nothing in the current UI calls it). The recommendation pipeline runs
    on Claude and local embeddings, so OPENAI_API_KEY is normally unset — building
    the client eagerly at import would then raise and take the whole backend down.
    """
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: init the fallout KB store, then build it from the incidents
    spreadsheet (data/*.xlsx — see backend/kb_source.py)."""
    data_manager.init_watchlist_csv()
    fallout_store.init_collection()
    try:
        fallout_store.build_kb()
    except Exception as e:
        print(f"[WARN] KB build from the incidents spreadsheet failed on startup: {e}. "
              f"Using existing ChromaDB data.")
        fallout_store.rebuild_bm25()
    yield


app = FastAPI(title="Fallout Remediation Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5050", "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fallout_router)


# ── Request Models ──────────────────────────────────────────────
class WatchlistRequest(BaseModel):
    user_name: str
    email: str
    phone: str
    ticket_id: str
    target_status: str


# ── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "kb_size": fallout_store.count()}


@app.post("/add-to-watchlist")
async def add_to_watchlist(req: WatchlistRequest):
    """Add a user to the watchlist for a ticket."""
    entry = data_manager.append_watchlist(
        user_name=req.user_name, email=req.email, phone=req.phone,
        ticket_id=req.ticket_id, target_status=req.target_status,
    )
    return {"success": True, "entry": entry}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Transcribe uploaded voice input (webm) to text via OpenAI Whisper."""
    try:
        audio_bytes = await audio.read()
        if not audio_bytes:
            return {"error": "empty audio"}
        transcript = _openai_client().audio.transcriptions.create(
            model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1"),
            file=(audio.filename or "recording.webm", audio_bytes),
        )
        return {"text": transcript.text}
    except Exception as e:
        print(f"[TRANSCRIBE] Failed: {e}")
        return {"error": str(e)}
