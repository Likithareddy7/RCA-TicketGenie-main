# Use the OS certificate store for TLS so corporate proxy / SSL-inspection root CAs
# are trusted. Must run before any HTTPS client (chromadb, requests) is constructed.
import truststore
truststore.inject_into_ssl()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv

import fallout_store
from fallout_api import router as fallout_router

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: init the KB store, then build it from whichever source KB_SOURCE
    selects (the demo spreadsheet, or closed incidents in the ServiceNow demo
    group). A build failure is not fatal: the existing ChromaDB data is reused so
    the assistant still answers."""
    fallout_store.init_collection()
    try:
        fallout_store.build_kb()
    except Exception as e:
        print(f"[WARN] KB build failed on startup: {e}. Using existing ChromaDB data.")
        fallout_store.rebuild_bm25()
    yield


app = FastAPI(title="TicketGenie Support Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5050", "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fallout_router)


@app.get("/health")
def health():
    return {"status": "ok", "kb_size": fallout_store.count()}
