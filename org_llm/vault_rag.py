"""Vault RAG — Qdrant-backed retrieval over org-mode wiki/notes/vault.

Builds a vector index over three source sets:
  - wiki   : docs/wiki/*.org (org-llm concept reference)
  - notes  : docs/notes/*.org (agent reports + design notes)
  - vault  : ~/org/*.org (user's personal vault, PII-filtered)

Public API:
  - build_index(...)      → walk sources, chunk, embed, upload to Qdrant
  - vault_search(query, k=5) → query Qdrant, return JSON list of hits

Embedding: sentence-transformers/all-MiniLM-L6-v2 (FOSS, 384-dim, CPU).
Storage:   Qdrant Cloud (free tier), collection "org-llm-vault".
Auth:      pass slugs org-llm/cloud/qdrant/{url,api-key}.
Cost:      $0 — local embed, free-tier Qdrant.

PII filter (vault only):
  - Skip files matching: *captains-log*, *personal*, *private*,
    *credentials*, *secrets*, *passwords*, *pii*
  - Skip headings whose text contains: PASSWORD / PRIVATE_KEY /
    SECRET_KEY / API_KEY (case-insensitive)
  - Vault files MUST have a top-level :ID: property to be indexed
    (org-roam-marked = canonical knowledge, not personal scratchpad).

R19 Track D — RAG retrieval at edit time. Tool wrapper in specialist.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


# ── Constants ────────────────────────────────────────────────────────────
COLLECTION_NAME = "org-llm-vault"
EMBED_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
DISTANCE_METRIC = "Cosine"

# Approx tokens-per-char for English prose (used for chunk sizing).
# all-MiniLM has a 256-token cap per call, but we chunk to ~1024 chars
# (≈ 256 tokens) so the embedder doesn't need to truncate aggressively.
MAX_CHUNK_CHARS = 1024
MIN_CHUNK_CHARS = 50   # skip near-empty chunks

# PII filter — filename patterns (vault only).
PII_FILENAME_PATTERNS = (
    "captains-log", "personal", "private", "credentials",
    "secrets", "passwords", "pii",
)

# PII filter — heading text matchers (case-insensitive).
PII_HEADING_PATTERNS = ("PASSWORD", "PRIVATE_KEY", "SECRET_KEY", "API_KEY")

# Pass slugs for Qdrant credentials.
QDRANT_URL_SLUG = "org-llm/cloud/qdrant/url"
QDRANT_API_KEY_SLUG = "org-llm/cloud/qdrant/api-key"


# ── Data classes ─────────────────────────────────────────────────────────
@dataclass
class Chunk:
    """One indexable unit: a heading subtree (or paragraph slice if oversized)."""
    chunk_id: str         # deterministic uuid5 over (file, heading, idx)
    file: str             # absolute path string
    heading: str          # heading text (e.g. "* What this is" / "(top)")
    text: str             # the chunk's prose body
    parent_uuid: Optional[str]   # top-of-file :ID: if any
    tags: list[str] = field(default_factory=list)
    source_set: str = "vault"    # "wiki" / "notes" / "vault"


# ── Pass / cred plumbing ────────────────────────────────────────────────
def _pass_show(slug: str) -> str:
    """Run `pass <slug>` and return stripped output."""
    cp = subprocess.run(
        ["pass", "show", slug],
        capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


def _qdrant_creds() -> tuple[str, str]:
    """Read cluster URL + API key from pass."""
    return _pass_show(QDRANT_URL_SLUG), _pass_show(QDRANT_API_KEY_SLUG)


# ── PII filtering ────────────────────────────────────────────────────────
def is_pii_filename(path: Path) -> bool:
    """True if filename matches any PII filename pattern."""
    name = path.name.lower()
    return any(p in name for p in PII_FILENAME_PATTERNS)


def heading_contains_pii(heading: str) -> bool:
    """True if heading text contains any PII heading pattern."""
    upper = heading.upper()
    return any(p in upper for p in PII_HEADING_PATTERNS)


# ── Org parsing — minimal, regex-based ──────────────────────────────────
_HEADING_RE = re.compile(r"^(\*+)\s+(.+?)\s*$")
_ID_RE = re.compile(r"^:ID:\s+([0-9a-f-]{8,})", re.MULTILINE)
_FILETAGS_RE = re.compile(r"^#\+FILETAGS:\s*(.+)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\+TITLE:\s*(.+)$", re.MULTILINE)
_BEGIN_SRC_RE = re.compile(r"^\s*#\+begin_src\b", re.IGNORECASE)
_END_SRC_RE = re.compile(r"^\s*#\+end_src\b", re.IGNORECASE)


def _read_file_text(path: Path) -> Optional[str]:
    """Read text safely, returning None on decode/IO error."""
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


def _extract_top_id(text: str) -> Optional[str]:
    """First :ID: in file (top-level property drawer)."""
    m = _ID_RE.search(text)
    return m.group(1) if m else None


def _extract_filetags(text: str) -> list[str]:
    """Parse #+FILETAGS line into a tag list."""
    m = _FILETAGS_RE.search(text)
    if not m:
        return []
    raw = m.group(1).strip()
    return [t for t in raw.split(":") if t]


def _strip_code_blocks(body: str) -> str:
    """Drop #+begin_src ... #+end_src blocks from body text."""
    out = []
    in_src = False
    for line in body.splitlines():
        if _BEGIN_SRC_RE.match(line):
            in_src = True
            continue
        if _END_SRC_RE.match(line):
            in_src = False
            continue
        if not in_src:
            out.append(line)
    return "\n".join(out)


def _split_into_subtrees(text: str) -> Iterator[tuple[str, str]]:
    """Yield (heading, body) tuples for each heading subtree.

    The synthetic first subtree (heading="(top)") covers the file
    preamble — text before the first heading line. Property drawers
    and #+keywords are kept; only #+begin_src blocks are dropped.
    """
    lines = text.splitlines(keepends=False)
    cur_heading = "(top)"
    cur_body: list[str] = []
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            # flush prior subtree
            yield cur_heading, "\n".join(cur_body)
            cur_heading = line
            cur_body = []
        else:
            cur_body.append(line)
    yield cur_heading, "\n".join(cur_body)


def _split_long_body(body: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split body on paragraph boundaries (blank lines) so no piece exceeds max_chars.

    Falls back to hard slicing if a single paragraph is itself too large.
    """
    if len(body) <= max_chars:
        return [body]
    paras = re.split(r"\n\s*\n", body)
    pieces: list[str] = []
    cur = ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:
            # hard-slice the over-large paragraph
            if cur:
                pieces.append(cur)
                cur = ""
            for i in range(0, len(p), max_chars):
                pieces.append(p[i:i + max_chars])
            continue
        if cur and len(cur) + len(p) + 2 > max_chars:
            pieces.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        pieces.append(cur)
    return pieces


# ── Chunk builders ───────────────────────────────────────────────────────
def chunks_for_file(path: Path, source_set: str,
                    enforce_id_required: bool = False) -> list[Chunk]:
    """Walk a single org file → list of Chunk records.

    enforce_id_required: when True, return [] unless the file has a
    top-level :ID: property. Used for source_set='vault'.
    """
    text = _read_file_text(path)
    if text is None:
        return []
    parent_uuid = _extract_top_id(text)
    if enforce_id_required and not parent_uuid:
        return []
    tags = _extract_filetags(text)
    chunks: list[Chunk] = []
    for heading, body in _split_into_subtrees(text):
        # PII heading filter (applies to all source sets but is a no-op
        # outside the vault since wiki/notes don't have such headings).
        if heading_contains_pii(heading):
            continue
        body = _strip_code_blocks(body).strip()
        if len(body) < MIN_CHUNK_CHARS:
            continue
        for idx, piece in enumerate(_split_long_body(body)):
            piece_clean = piece.strip()
            if len(piece_clean) < MIN_CHUNK_CHARS:
                continue
            cid_seed = f"{path}|{heading}|{idx}|{hashlib.sha1(piece_clean.encode()).hexdigest()[:12]}"
            cid = str(uuid.uuid5(uuid.NAMESPACE_URL, cid_seed))
            chunks.append(Chunk(
                chunk_id=cid,
                file=str(path),
                heading=heading,
                text=piece_clean,
                parent_uuid=parent_uuid,
                tags=tags,
                source_set=source_set,
            ))
    return chunks


# ── Source enumeration ───────────────────────────────────────────────────
def enumerate_sources(
    wiki_dir: Path,
    notes_dir: Path,
    vault_dir: Path,
) -> dict[str, list[Path]]:
    """Resolve the three source sets to file lists, applying PII-filename filter."""
    out: dict[str, list[Path]] = {"wiki": [], "notes": [], "vault": []}
    if wiki_dir.is_dir():
        out["wiki"] = sorted(wiki_dir.glob("*.org"))
    if notes_dir.is_dir():
        out["notes"] = sorted(notes_dir.glob("*.org"))
    if vault_dir.is_dir():
        out["vault"] = sorted(
            p for p in vault_dir.glob("*.org") if not is_pii_filename(p)
        )
    return out


# ── Embedding ────────────────────────────────────────────────────────────
_EMBEDDER: Any = None


def _get_embedder() -> Any:
    """Lazy-load the sentence-transformers model (CPU)."""
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer(EMBED_MODEL_ID, device="cpu")
    return _EMBEDDER


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Encode texts → list of 384-dim vectors. Returns plain Python lists
    (not numpy) so we can hand them to qdrant-client directly."""
    model = _get_embedder()
    arr = model.encode(
        texts, batch_size=batch_size,
        convert_to_numpy=True, show_progress_bar=False,
        normalize_embeddings=True,
    )
    return [v.tolist() for v in arr]


# ── Qdrant client + collection ──────────────────────────────────────────
def _qdrant_client(url: Optional[str] = None,
                   api_key: Optional[str] = None) -> Any:
    """Construct a qdrant-client. Reads creds from pass when None."""
    from qdrant_client import QdrantClient
    if url is None or api_key is None:
        url, api_key = _qdrant_creds()
    return QdrantClient(url=url, api_key=api_key, timeout=60)


def ensure_collection(client: Any) -> bool:
    """Create the collection if absent. Returns True if newly created."""
    from qdrant_client.http import models as qmodels
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME in existing:
        return False
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=qmodels.VectorParams(
            size=EMBED_DIM,
            distance=qmodels.Distance.COSINE,
        ),
    )
    return True


def upload_chunks(client: Any, chunks: list[Chunk],
                  vectors: list[list[float]],
                  batch_size: int = 64) -> int:
    """Upload chunks + vectors to Qdrant in batches. Returns count uploaded."""
    from qdrant_client.http import models as qmodels
    assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
    n = 0
    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i:i + batch_size]
        batch_vecs = vectors[i:i + batch_size]
        points = [
            qmodels.PointStruct(
                id=c.chunk_id,
                vector=v,
                payload={
                    "file": c.file,
                    "heading": c.heading,
                    "text": c.text,
                    "uuid": c.parent_uuid,
                    "tags": c.tags,
                    "source_set": c.source_set,
                },
            )
            for c, v in zip(batch_chunks, batch_vecs)
        ]
        client.upsert(collection_name=COLLECTION_NAME, points=points,
                      wait=True)
        n += len(points)
    return n


# ── Public: build_index ──────────────────────────────────────────────────
@dataclass
class BuildResult:
    files_seen: int
    files_indexed: int
    files_pii_skipped: int
    files_no_id_skipped: int
    chunks_total: int
    chunks_per_set: dict[str, int]
    collection_created: bool


def build_index(
    wiki_dir: Path = Path("/home/daniel/repos/org-llm/docs/wiki"),
    notes_dir: Path = Path("/home/daniel/repos/org-llm/docs/notes"),
    vault_dir: Path = Path("/home/daniel/org"),
    progress: Optional[Any] = None,
) -> BuildResult:
    """One-shot index build. Walks sources → chunks → embeds → uploads."""
    sources = enumerate_sources(wiki_dir, notes_dir, vault_dir)
    files_seen = sum(len(v) for v in sources.values())
    # Track filtered counts (PII filename + missing-:ID:).
    files_pii_skipped = 0
    if vault_dir.is_dir():
        files_pii_skipped = sum(
            1 for p in vault_dir.glob("*.org") if is_pii_filename(p)
        )

    all_chunks: list[Chunk] = []
    files_no_id_skipped = 0
    files_indexed = 0
    chunks_per_set: dict[str, int] = {"wiki": 0, "notes": 0, "vault": 0}

    for source_set, paths in sources.items():
        enforce_id = (source_set == "vault")
        for p in paths:
            cs = chunks_for_file(p, source_set, enforce_id_required=enforce_id)
            if not cs:
                if enforce_id and _extract_top_id(_read_file_text(p) or "") is None:
                    files_no_id_skipped += 1
                continue
            files_indexed += 1
            chunks_per_set[source_set] += len(cs)
            all_chunks.extend(cs)
            if progress is not None:
                progress(f"chunked {p.name}: {len(cs)} chunks")

    if progress is not None:
        progress(f"total: {len(all_chunks)} chunks across {files_indexed} files")
        progress(f"loading embedder {EMBED_MODEL_ID}...")

    if not all_chunks:
        return BuildResult(
            files_seen=files_seen,
            files_indexed=0,
            files_pii_skipped=files_pii_skipped,
            files_no_id_skipped=files_no_id_skipped,
            chunks_total=0,
            chunks_per_set=chunks_per_set,
            collection_created=False,
        )

    # Embed in chunks of 128 to keep memory bounded.
    if progress is not None:
        progress(f"embedding {len(all_chunks)} chunks...")
    texts = [c.text for c in all_chunks]
    vectors = embed_texts(texts, batch_size=32)

    client = _qdrant_client()
    created = ensure_collection(client)
    if progress is not None:
        progress(f"collection {'created' if created else 'exists'}; uploading...")
    upload_chunks(client, all_chunks, vectors)

    return BuildResult(
        files_seen=files_seen,
        files_indexed=files_indexed,
        files_pii_skipped=files_pii_skipped,
        files_no_id_skipped=files_no_id_skipped,
        chunks_total=len(all_chunks),
        chunks_per_set=chunks_per_set,
        collection_created=created,
    )


# ── Public: vault_search ─────────────────────────────────────────────────
def vault_search(query: str, k: int = 5,
                 source_filter: Optional[str] = None) -> list[dict]:
    """Query the index. Returns list of {file, heading, text, score, uuid, tags, source_set}.

    source_filter, if given, must be one of "wiki" / "notes" / "vault".
    """
    if not query or not query.strip():
        return []
    [vec] = embed_texts([query])
    client = _qdrant_client()
    from qdrant_client.http import models as qmodels
    flt = None
    if source_filter in ("wiki", "notes", "vault"):
        flt = qmodels.Filter(must=[
            qmodels.FieldCondition(
                key="source_set",
                match=qmodels.MatchValue(value=source_filter),
            )
        ])
    # Use query_points (current API; .search is deprecated upstream).
    resp = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        limit=k,
        with_payload=True,
        query_filter=flt,
    )
    hits = resp.points if hasattr(resp, "points") else resp
    out: list[dict] = []
    for h in hits:
        p = h.payload or {}
        out.append({
            "file": p.get("file"),
            "heading": p.get("heading"),
            "text": p.get("text"),
            "score": float(h.score) if h.score is not None else None,
            "uuid": p.get("uuid"),
            "tags": p.get("tags") or [],
            "source_set": p.get("source_set"),
        })
    return out


# ── Tool spec for specialist.py dispatch ────────────────────────────────
VAULT_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "vault_search",
        "description": (
            "Semantic search over the user's org-mode wiki + notes + vault. "
            "Returns the top-k most relevant paragraph-sized chunks of "
            "context (file, heading, text, score). Use this when you need "
            "background context for an edit — e.g. to find the canonical "
            "definition of a concept, prior decisions, or related design "
            "notes. Cost: $0 (local embed + free-tier Qdrant). Use early "
            "in a task to ground your edits in real prior work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query (e.g. 'DEC entry format', 'agent persona spec').",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of results (1-20). Default 5.",
                    "default": 5,
                },
                "source": {
                    "type": "string",
                    "description": "Optional filter: 'wiki' / 'notes' / 'vault'. Omit for all.",
                    "enum": ["wiki", "notes", "vault"],
                },
            },
            "required": ["query"],
        },
    },
}
