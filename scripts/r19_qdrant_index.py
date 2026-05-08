#!/usr/bin/env python3
"""R19 Track D — build the Qdrant vault index.

One-shot script. Walks docs/wiki/, docs/notes/, and ~/org/ (PII-filtered),
chunks per heading subtree, embeds with sentence-transformers/all-MiniLM-L6-v2
(local CPU), uploads to Qdrant Cloud (collection `org-llm-vault`, free tier).

Usage:
    python3 scripts/r19_qdrant_index.py
    python3 scripts/r19_qdrant_index.py --report /tmp/r19-vault-index-report.org

Reads creds from pass:
    org-llm/cloud/qdrant/url
    org-llm/cloud/qdrant/api-key

Cost: $0. Time: ~5-10 min for ~100 wiki + ~10 notes + ~50-150 vault files.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Ensure org_llm is importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from org_llm import vault_rag  # noqa: E402


SAMPLE_QUERIES = [
    ("Bridge Crew agent personas", 5, None),
    ("DEC entry format", 3, None),
    ("Phase 2026-05.20 sync verb", 5, None),
]


def _print_progress(msg: str) -> None:
    print(f"[r19] {msg}", flush=True)


def _format_hit(hit: dict, max_text: int = 200) -> str:
    """One-line summary for an org report block."""
    file_short = hit.get("file") or "?"
    if file_short.startswith("/home/daniel/"):
        file_short = file_short.replace("/home/daniel/", "~/")
    heading = (hit.get("heading") or "(top)").strip()
    text = (hit.get("text") or "").replace("\n", " ").strip()
    if len(text) > max_text:
        text = text[:max_text] + "..."
    score = hit.get("score")
    score_s = f"{score:.3f}" if score is not None else "?"
    return f"  - score={score_s} | {file_short} | {heading}\n    {text}"


def _run_sample_queries() -> list[tuple[str, int, list[dict]]]:
    out: list[tuple[str, int, list[dict]]] = []
    for query, k, src in SAMPLE_QUERIES:
        hits = vault_rag.vault_search(query, k=k, source_filter=src)
        out.append((query, k, hits))
    return out


def _write_report(path: Path, build: vault_rag.BuildResult,
                   sample_hits: list[tuple[str, int, list[dict]]],
                   duration_s: float) -> None:
    lines: list[str] = []
    lines.append("#+TITLE: R19 Track D — Qdrant vault index report")
    lines.append("#+FILETAGS: :r19:rag:qdrant:report:")
    lines.append("")
    lines.append("* Build summary")
    lines.append(f"- Collection: ={vault_rag.COLLECTION_NAME}=")
    lines.append(f"- Embedder: ={vault_rag.EMBED_MODEL_ID}= ({vault_rag.EMBED_DIM}-dim, cosine)")
    lines.append(f"- Files seen (after PII-filename filter): {build.files_seen}")
    lines.append(f"- Files indexed: {build.files_indexed}")
    lines.append(f"- Files skipped (PII filename): {build.files_pii_skipped}")
    lines.append(f"- Files skipped (vault, no :ID:): {build.files_no_id_skipped}")
    lines.append(f"- Total chunks: {build.chunks_total}")
    for s, n in build.chunks_per_set.items():
        lines.append(f"  - {s}: {n}")
    lines.append(f"- Collection newly created: {build.collection_created}")
    lines.append(f"- Build duration: {duration_s:.1f}s")
    lines.append("")
    lines.append("* Sample queries")
    for query, k, hits in sample_hits:
        lines.append(f"** ={query}= (k={k})")
        if not hits:
            lines.append("  (no hits)")
            continue
        for h in hits:
            lines.append(_format_hit(h))
    lines.append("")
    lines.append("* Integration notes")
    lines.append("- Tool wrapper exposed as =vault_search= via "
                 "=org_llm.specialist.VAULT_SEARCH_TOOL=.")
    lines.append("- Dispatch in =org_llm.specialist._dispatch_tool_call= "
                 "lazy-imports =org_llm.vault_rag.vault_search=.")
    lines.append("- Add =VAULT_SEARCH_TOOL= to a SpecialistTask's =tools= "
                 "list to enable retrieval at edit time.")
    lines.append("- Cost is $0: sentence-transformers runs CPU-local; "
                 "Qdrant free tier (1GB) is well within budget.")
    lines.append("- Re-run this script after vault edits to refresh "
                 "the index (overwrites existing chunks by deterministic "
                 "uuid5 over file/heading/idx/sha1).")
    lines.append("")
    lines.append("* PII filter recap")
    lines.append("- Filename patterns skipped: " + ", ".join(
        f"={p}=" for p in vault_rag.PII_FILENAME_PATTERNS))
    lines.append("- Heading patterns skipped: " + ", ".join(
        f"={p}=" for p in vault_rag.PII_HEADING_PATTERNS))
    lines.append("- Vault files require a top-level =:ID:= property; "
                 "anything else is excluded from the index.")
    path.write_text("\n".join(lines) + "\n")
    print(f"[r19] wrote report → {path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", default="/home/daniel/repos/org-llm/docs/wiki")
    ap.add_argument("--notes", default="/home/daniel/repos/org-llm/docs/notes")
    ap.add_argument("--vault", default="/home/daniel/org")
    ap.add_argument("--report", default="/tmp/r19-vault-index-report.org")
    ap.add_argument("--skip-build", action="store_true",
                    help="Skip the index build; only run sample queries + report.")
    args = ap.parse_args(argv)

    t0 = time.time()
    if args.skip_build:
        _print_progress("skipping build (--skip-build)")
        # Still construct a stub BuildResult for the report.
        build = vault_rag.BuildResult(
            files_seen=0, files_indexed=0,
            files_pii_skipped=0, files_no_id_skipped=0,
            chunks_total=0, chunks_per_set={"wiki": 0, "notes": 0, "vault": 0},
            collection_created=False,
        )
    else:
        build = vault_rag.build_index(
            wiki_dir=Path(args.wiki),
            notes_dir=Path(args.notes),
            vault_dir=Path(args.vault),
            progress=_print_progress,
        )
    duration = time.time() - t0
    _print_progress(f"build done in {duration:.1f}s — {build.chunks_total} chunks "
                     f"across {build.files_indexed} files")

    _print_progress("running sample queries...")
    sample_hits = _run_sample_queries()
    for q, k, hits in sample_hits:
        _print_progress(f"  query={q!r} k={k} → {len(hits)} hits")

    _write_report(Path(args.report), build, sample_hits, duration)
    print(json.dumps({
        "files_indexed": build.files_indexed,
        "chunks_total": build.chunks_total,
        "chunks_per_set": build.chunks_per_set,
        "files_pii_skipped": build.files_pii_skipped,
        "files_no_id_skipped": build.files_no_id_skipped,
        "duration_s": round(duration, 1),
        "report": args.report,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
