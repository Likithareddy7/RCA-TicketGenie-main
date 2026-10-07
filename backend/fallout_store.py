"""
Fallout Knowledge-Base Store
----------------------------
ChromaDB + BM25 hybrid search over the CLOSED fallout tickets (the historical KB).

Embedding policy (problem-side only — the resolution is NEVER embedded):
    document = short_description + Description + Subcategory + Service Type
Everything else (Location ID, TN, order ref, Resolution Code, Resolution Notes,
referenced Service ID, state) is stored as metadata and used AFTER retrieval.

The KB is (re)built from the incidents SPREADSHEET (kb_source) on demand via
build_kb(). ServiceNow still supplies the OPEN queue; only the closed/historical
side is sourced from the spreadsheet.
"""

import os
import re
import math
import hashlib
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from rank_bm25 import BM25Okapi

import kb_source

load_dotenv()

CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "chroma_db")
COLLECTION_NAME = "fallout_tickets"

METADATA_FIELDS = [
    "number", "state", "short_description", "subcategory", "category",
    "location_id", "ban", "tn", "order_ref", "order_type", "task_ref",
    "service_type", "resolution_code", "referenced_service_id",
    "description", "work_notes", "resolution_notes",
    "kb_group",
]

_client = None
_collection = None
_embed_fn = None

_bm25_index = None
_bm25_corpus = []   # list of {"id", "metadata"}

_BATCH = 500        # embed/upsert chunk size — a spreadsheet KB can be thousands of rows


def _dedupe(tickets: list) -> list:
    """Chroma ids must be unique. Spreadsheet exports routinely repeat a ticket
    number across rows, so keep the first occurrence and suffix later ones rather
    than silently dropping them."""
    seen, out = {}, []
    for t in tickets:
        num = str(t.get("number", "") or "").strip()
        if not num:
            continue
        if num in seen:
            seen[num] += 1
            t = {**t, "number": f"{num}-{seen[num]}"}
        else:
            seen[num] = 0
        out.append(t)
    return out


def _tokenize(text: str) -> list:
    return re.findall(r"\w+", (text or "").lower())


def _get_embedding_function():
    """Embeddings run LOCALLY (all-MiniLM-L6-v2 via onnxruntime), not through an API.

    Anthropic has no embeddings endpoint, so moving the LLM calls to Claude leaves
    the vector side needing a home. A local model is the better answer here anyway:
    it needs no second API key, costs nothing per ticket, and cannot fail the demo
    with a quota or network error. The model is cached under ~/.cache/chroma after
    the first run, so it works offline thereafter.

    Dimension is 384, vs 1536 for OpenAI's text-embedding-3-small. Chroma pins the
    dimension at collection-creation time, so a collection built with the old
    embedder CANNOT be queried with this one — data/chroma_db must be rebuilt.
    """
    global _embed_fn
    if _embed_fn is None:
        _embed_fn = embedding_functions.DefaultEmbeddingFunction()
    return _embed_fn


def build_document(ticket: dict) -> str:
    """Problem-side text that gets embedded + keyword-indexed. Resolution excluded."""
    parts = [
        ticket.get("short_description", ""),
        ticket.get("description", ""),
        ticket.get("subcategory", ""),
        ticket.get("service_type", ""),
    ]
    return "\n".join(p for p in parts if p).strip()


def _doc_hash(ticket: dict) -> str:
    """Fingerprint of the KB-relevant content of a ticket. Changes when the
    problem text, resolution, code, or state changes — used to detect updates
    so only changed tickets are re-embedded on sync."""
    basis = "|".join([
        build_document(ticket),
        str(ticket.get("resolution_notes", "") or ""),
        str(ticket.get("resolution_code", "") or ""),
        str(ticket.get("state", "") or ""),
        str(ticket.get("kb_group", "") or ""),
    ])
    return hashlib.md5(basis.encode("utf-8")).hexdigest()


def _metadata(ticket: dict) -> dict:
    meta = {k: str(ticket.get(k, "") or "") for k in METADATA_FIELDS}
    meta["doc_hash"] = _doc_hash(ticket)   # for incremental sync change-detection
    return meta


# ── init ────────────────────────────────────────────────────────────────

def init_collection():
    global _client, _collection
    _client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
    _init_bm25()
    return _collection


def get_collection():
    global _collection
    if _collection is None:
        init_collection()
    return _collection


# ── KB build / sync from ServiceNow ─────────────────────────────────────

# Where the closed-ticket knowledge base comes from.
#
#   spreadsheet  the file in data/ (default: works with no instance at all)
#   servicenow   closed incidents in the demo assignment group, so what is visible
#                in the ServiceNow UI is literally what gets searched
#
# When 'servicenow' is selected but the instance cannot be read, this falls back to
# the spreadsheet rather than serving an empty KB. That is a deliberate product
# decision: an unreachable instance should degrade the KB's freshness, not wipe out
# the assistant's ability to answer anything.
KB_SOURCE = os.getenv("KB_SOURCE", "spreadsheet").strip().lower()


def _default_kb_group() -> str:
    """Where a ticket with no group of its own belongs."""
    try:
        import servicenow_client
        return servicenow_client.DEMO_GROUP
    except Exception:
        return ""


def fetch_kb() -> list:
    """The closed tickets to index, from whichever source is configured."""
    if KB_SOURCE == "servicenow":
        import servicenow_client
        try:
            kb = servicenow_client.fetch_demo_closed_kb()
            if kb:
                print(f"[FALLOUT-KB] source: ServiceNow "
                      f"{servicenow_client.kb_groups()} ({len(kb)} closed total)")
                return kb
            print(f"[FALLOUT-KB] ServiceNow groups "
                  f"{servicenow_client.kb_groups()} hold no closed incidents; "
                  f"falling back to the spreadsheet.")
        except Exception as e:
            print(f"[FALLOUT-KB] ServiceNow KB unavailable ({type(e).__name__}); "
                  f"falling back to the spreadsheet.")
    # The spreadsheet carries no group of its own, so its rows are attributed to
    # the demo group, which is also where unmatched tickets are filed.
    rows = kb_source.fetch_closed_kb()
    for t in rows:
        t.setdefault("kb_group", _default_kb_group())
    return rows


def build_kb() -> int:
    """Clear and rebuild the KB collection from the CLOSED tickets of whichever
    source is configured (see fetch_kb). Returns the number indexed."""
    global _client, _collection
    if _client is None:
        init_collection()
    try:
        _client.delete_collection(name=COLLECTION_NAME)
    except Exception:
        pass
    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )

    kb = fetch_kb()
    if not kb:
        print("[FALLOUT-KB] No closed tickets found in the configured KB source.")
        _init_bm25()
        return 0

    kb = _dedupe(kb)
    ids = [t["number"] for t in kb]
    docs = [build_document(t) for t in kb]
    metas = [_metadata(t) for t in kb]
    # Chroma caps a single upsert; batch so a few-thousand-row sheet indexes fine.
    for i in range(0, len(ids), _BATCH):
        _collection.upsert(ids=ids[i:i + _BATCH], documents=docs[i:i + _BATCH],
                           metadatas=metas[i:i + _BATCH])
    print(f"[FALLOUT-KB] Indexed {len(ids)} closed KB tickets from the spreadsheet.")
    _init_bm25()
    return len(ids)


def sync_kb(closed_tickets: list = None) -> dict:
    """Incrementally reconcile the KB with the CURRENT closed tickets, WITHOUT a
    full rebuild:
        * add tickets that were newly closed,
        * re-embed tickets whose content/resolution changed (detected by doc_hash),
        * drop tickets that were deleted or re-opened (no longer closed).
    Only changed tickets are re-embedded, so this is cheap to call often (e.g. on
    every queue load) — so editing the incidents spreadsheet is reflected in the KB
    without needing a restart. Pass an already-loaded closed list to avoid a second
    read of the file.
    """
    global _collection
    if _collection is None:
        init_collection()
    if closed_tickets is None:
        closed_tickets = fetch_kb()
    closed_tickets = _dedupe(closed_tickets)

    existing = _collection.get(include=["metadatas"])
    existing_ids = existing.get("ids") or []
    existing_hash = {i: (m or {}).get("doc_hash", "")
                     for i, m in zip(existing_ids, existing.get("metadatas") or [])}

    desired = {t["number"]: t for t in closed_tickets if t.get("number")}
    desired_ids = set(desired)

    to_delete = [i for i in existing_ids if i not in desired_ids]
    to_upsert = [num for num, t in desired.items()
                 if existing_hash.get(num) != _doc_hash(t)]

    if to_delete:
        for i in range(0, len(to_delete), _BATCH):
            _collection.delete(ids=to_delete[i:i + _BATCH])
    if to_upsert:
        for i in range(0, len(to_upsert), _BATCH):
            chunk = to_upsert[i:i + _BATCH]
            _collection.upsert(
                ids=chunk,
                documents=[build_document(desired[n]) for n in chunk],
                metadatas=[_metadata(desired[n]) for n in chunk],
            )
    if to_delete or to_upsert:
        _init_bm25()
        print(f"[FALLOUT-KB] sync: +{len(to_upsert)} upserted, -{len(to_delete)} removed, size={count()}")
    return {"upserted": len(to_upsert), "removed": len(to_delete), "kb_size": count()}


# ── BM25 ────────────────────────────────────────────────────────────────

def _init_bm25():
    global _bm25_index, _bm25_corpus
    try:
        if _collection is None:
            return
        result = _collection.get(include=["documents", "metadatas"])
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        if not ids:
            _bm25_index, _bm25_corpus = None, []
            return
        _bm25_corpus = []
        tokenized = []
        for doc_id, doc, meta in zip(ids, documents, metadatas):
            _bm25_corpus.append({"id": doc_id, "metadata": meta})
            tokenized.append(_tokenize(doc))
        _bm25_index = BM25Okapi(tokenized)
        print(f"[FALLOUT-KB] BM25 index built with {len(_bm25_corpus)} tickets.")
    except Exception as e:
        print(f"[WARN] BM25 init failed: {e}")
        _bm25_index, _bm25_corpus = None, []


def rebuild_bm25():
    _init_bm25()


# ── hybrid search ───────────────────────────────────────────────────────

def _embed_query(query_text: str):
    try:
        return _get_embedding_function()([query_text])[0]
    except Exception as e:
        print(f"[WARN] Query embedding failed: {e}")
        return None


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(query_text: str, k: int = 5) -> list:
    """Hybrid search over the KB. Returns up to k results as
    {id, similarity, rrf_score, metadata}, best first."""
    query_embedding = _embed_query(query_text)
    semantic = _semantic_search(query_text, n=20, query_embedding=query_embedding)
    keyword = _bm25_search(query_text, n=20)
    merged = _reciprocal_rank_fusion(semantic, keyword)
    merged = _fill_missing_similarities(query_embedding, merged)
    merged.sort(key=lambda r: r["similarity"], reverse=True)
    return merged[:k]


def _semantic_search(query_text, n=20, query_embedding=None):
    collection = get_collection()
    kwargs = {"n_results": n}
    if query_embedding is not None:
        kwargs["query_embeddings"] = [query_embedding]
    else:
        # _embed_query already failed here; query_texts would make Chroma retry the
        # same embedding call. Let it, but degrade to no semantic hits rather than
        # propagating — BM25 and the deterministic paths can still answer.
        kwargs["query_texts"] = [query_text]
    try:
        results = collection.query(**kwargs)
    except Exception as e:
        print(f"[WARN] Semantic search unavailable: {e}")
        return []
    if not results["ids"] or not results["ids"][0]:
        return []
    out = []
    for i, doc_id in enumerate(results["ids"][0]):
        out.append({
            "id": doc_id,
            "similarity": round(1 - results["distances"][0][i], 4),
            "metadata": results["metadatas"][0][i],
        })
    return out


def _bm25_search(query_text, n=20):
    if _bm25_index is None or not _bm25_corpus:
        return []
    scores = _bm25_index.get_scores(_tokenize(query_text))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [{"id": _bm25_corpus[i]["id"], "metadata": _bm25_corpus[i]["metadata"]}
            for i in ranked if scores[i] > 0][:n]


def _reciprocal_rank_fusion(semantic, keyword, k=60, semantic_weight=0.6, keyword_weight=0.4):
    """Fuse the semantic and keyword result lists by Reciprocal Rank Fusion: each
    hit scores weight/(k+rank) from each list it appears in, and the scores add.
    This blends both rankings using only rank position (robust to the two search
    methods being on different score scales). Semantic is weighted higher (0.6 vs
    0.4). The true cosine similarity is carried through for the confidence gate."""
    rrf, id_to_meta, id_to_sim = {}, {}, {}
    for rank, item in enumerate(semantic, start=1):
        did = item["id"]
        rrf[did] = rrf.get(did, 0.0) + semantic_weight / (k + rank)
        id_to_meta[did] = item["metadata"]
        id_to_sim[did] = item["similarity"]
    for rank, item in enumerate(keyword, start=1):
        did = item["id"]
        rrf[did] = rrf.get(did, 0.0) + keyword_weight / (k + rank)
        if did not in id_to_meta:
            id_to_meta[did] = item["metadata"]
            id_to_sim[did] = 0.0
    merged = sorted(rrf.items(), key=lambda x: x[1], reverse=True)
    return [{"id": did, "similarity": id_to_sim[did], "rrf_score": round(s, 6),
             "metadata": id_to_meta[did]} for did, s in merged]


def _fill_missing_similarities(query_embedding, results):
    """Backfill true cosine similarity for keyword-only hits (similarity 0.0),
    so the confidence gate reflects hybrid relevance, not just semantic top-N."""
    if query_embedding is None:
        return results
    missing = [r["id"] for r in results if r.get("similarity", 0.0) == 0.0 and r.get("id")]
    if not missing:
        return results
    try:
        fetched = get_collection().get(ids=missing, include=["embeddings"])
        # `embeddings` comes back as a 2-D numpy array. Testing it for truthiness
        # (`... or []`) raises "truth value of an array is ambiguous", so the
        # absent case is checked explicitly against None instead.
        embs = fetched.get("embeddings")
        if embs is None:
            embs = []
        id_to_emb = dict(zip(fetched.get("ids", []), embs))
    except Exception as e:
        print(f"[WARN] embedding backfill failed: {e}")
        return results
    for r in results:
        if r.get("similarity", 0.0) == 0.0:
            emb = id_to_emb.get(r.get("id"))
            if emb is not None and len(emb):
                r["similarity"] = round(_cosine(query_embedding, emb), 4)
    return results


def count() -> int:
    return get_collection().count()
