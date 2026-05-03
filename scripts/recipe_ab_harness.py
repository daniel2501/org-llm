#!/usr/bin/env python3
"""A/B harness — does the recipe layer earn its keep?

For each prompt × {recipes_on, recipes_off} × N trials:

  recipes_on  (arm A): match_recipe → execute_recipe → format_prefetch_block,
                       then ONE cloud call: persona + (user + prefetch).
                       No tools array. The data is already in the prompt.

  recipes_off (arm B): persona + user + tools, then a tool-use LOOP. Each
                       tool_call gets executed locally against org_tools /
                       vault_facts and the result fed back. Continue until
                       the model emits a final answer (no more tool_calls)
                       or we hit MAX_ROUNDS.

Captures per prompt-arm-trial: wall-clock latency, prompt+completion tokens
(summed across round-trips), tool_call shapes, final narration text. Output
is JSONL at /tmp/recipe_ab_results.jsonl, one line per prompt-arm-trial.

Usage::

    scripts/recipe_ab_harness.py --dry-run     # routing check only, no cloud
    scripts/recipe_ab_harness.py --trials 1
    scripts/recipe_ab_harness.py --trials 3    # full pass

The DB toggle `proxy_orchestration_mode` is NOT used here — we directly
synthesize each arm's behaviour so the comparison is hermetic and the
production proxy doesn't need to be running.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path("/home/daniel/repos/org-llm")
sys.path.insert(0, str(REPO))
os.environ.setdefault("ORG_LLM_DB",
                      os.path.expanduser("~/.local/share/org-llm/org-llm.db"))
os.environ.setdefault("ORG_LLM_ORG_DIR", os.path.expanduser("~/org"))

from org_llm.orchestration import match_recipe                    # noqa: E402
from org_llm.recipe_runner import execute_recipe, format_prefetch_block  # noqa: E402
from org_llm.llm_proxy import _resolve_cloud_failover_target      # noqa: E402

OUT_PATH    = Path("/tmp/recipe_ab_results.jsonl")
MAX_ROUNDS  = 4          # tool-use round-trip cap for arm B
ROUND_TIMEOUT = 90       # seconds per cloud call

# ── prompt set ────────────────────────────────────────────────────────────────
# Three buckets:
#   anchor  — recipe-eligible (recipes' designed-for population)
#   control — no recipe match (toggle should be a no-op here)
#   bait    — recipe matches but user intent diverges (false-fire risk)
PROMPTS: list[tuple[str, str, str]] = [
    ("A1", "anchor",  "in dailies, how many times have I made my bed?"),
    ("A2", "anchor",  "how often have I exercised across the vault?"),
    ("A3", "anchor",  "how many times have I drunk coffee this year?"),
    ("A4", "anchor",  "summarize what I have been working on recently"),
    ("A5", "anchor",  "pull the most common routine items from my recent dailies"),
    ("A6", "anchor",  "what has my mood been like over the past month?"),
    ("A7", "anchor",  "given today's weather and my schedule, what should I prioritise today?"),
    ("C1", "control", "what are my top 10 tags by frequency?"),
    ("C2", "control", "where in my notes do I mention Postgres?"),
    ("C3", "control", "which of my notes are orphaned?"),
    ("C4", "control", "what's overdue or due this week?"),
    ("C5", "control", "what doom packages do I have enabled?"),
    ("C6", "control", "what's my keybinding for org-roam-node-find?"),
    ("C7", "control", "show me every node that has a :CUSTOM_ID: property"),
    ("B1", "bait",    "in dailies, do I journal regularly?"),
    ("B2", "bait",    "how often am I burnt out?"),
    ("B3", "bait",    "summarize my postgres setup"),
    ("B4", "bait",    "help me scope a new @therapist agent that reasons over my dailies' emotional content"),
]

# Single neutral persona used by both arms, so neither benefits from a
# persona that's biased toward pre-fetch-style or tool-use-style answers.
PERSONA = (
    "You are an analyst with deep knowledge of the user's org-roam vault. "
    "Answer the user's question directly using the data available. Cite "
    "specific files or dates when natural. Be concise — 1 to 3 sentences "
    "for simple questions, longer only when the question genuinely warrants "
    "it. Never invent files, counts, or content you don't have evidence for. "
    "If the data is empty or insufficient, say so plainly."
)


# ── tool inventory (arm B) ────────────────────────────────────────────────────
# Each tool: (function, openai_tool_def). The function takes kwargs and
# returns a JSON-serialisable result; we cap the JSON before feeding back
# to the model so a runaway tool result can't blow the context.

def _cap(obj: Any, n: int = 4000) -> str:
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s if len(s) <= n else s[:n] + f"\n…[truncated; full payload was {len(s)} chars]"

def _t_count_matches(pattern: str, path_glob: str = "**/*.org") -> Any:
    from org_llm.org_tools import org_count_matches
    return org_count_matches(pattern, path_glob=path_glob)

def _t_list_recent_nodes(days: int = 14) -> Any:
    from datetime import datetime, timedelta
    from sqlalchemy.orm import Session
    from org_llm.db import Node, make_engine
    out: list[dict] = []
    with Session(make_engine()) as s:
        since = (datetime.now() - timedelta(days=days)).timestamp()
        for n in (s.query(Node).filter(Node.mtime >= since)
                   .order_by(Node.mtime.desc()).limit(40).all()):
            out.append({"title": n.title, "tags": n.tags or "",
                        "date": (datetime.fromtimestamp(n.mtime).date().isoformat()
                                 if n.mtime else "?")})
    return out

def _t_recent_files(days: int = 7) -> Any:
    from datetime import datetime, timedelta
    from sqlalchemy.orm import Session
    from org_llm.db import File, make_engine
    out: list[dict] = []
    with Session(make_engine()) as s:
        since = (datetime.now() - timedelta(days=days)).timestamp()
        for f in (s.query(File).filter(File.mtime >= since)
                   .order_by(File.mtime.desc()).limit(40).all()):
            out.append({"path": str(f.path),
                        "date": (datetime.fromtimestamp(f.mtime).date().isoformat()
                                 if f.mtime else "?")})
    return out

def _t_list_dailies(limit: int = 5, include_content: bool = False) -> Any:
    org_dir = Path(os.environ["ORG_LLM_ORG_DIR"]) / "daily"
    if not org_dir.exists():
        return []
    files = sorted(org_dir.glob("*.org"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        rec = {"path": str(f),
               "date": time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))}
        if include_content:
            try:
                rec["content"] = f.read_text()[:3000]
            except Exception:
                rec["content"] = ""
        out.append(rec)
    return out

def _t_get_fact(name: str) -> Any:
    from org_llm.vault_facts import get_fact
    return get_fact(name)

def _t_org_agenda(window_days: int = 7) -> Any:
    from org_llm.org_tools import org_agenda
    return org_agenda(window_days=window_days)

def _t_weather_for_agenda(days: int = 7) -> Any:
    try:
        from org_llm import weather as _w
        return _w.weather_for_agenda(days=days)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

def _t_org_tag_index(limit: int = 10) -> Any:
    from org_llm.org_tools import org_tag_index
    return org_tag_index(limit=limit)

def _t_org_grep(pattern: str, path_glob: str = "**/*.org", max_results: int = 20) -> Any:
    from org_llm.org_tools import org_grep
    return org_grep(pattern, path_glob=path_glob, max_results=max_results)

def _t_org_orphans(max_results: int = 50) -> Any:
    from org_llm.org_tools import org_orphans
    return org_orphans(max_results=max_results)

def _t_org_property_search(prop: str, value: str | None = None, max_results: int = 30) -> Any:
    from org_llm.org_tools import org_property_search
    return org_property_search(prop, value=value, max_results=max_results)

def _t_org_id_find(query: str, top_n: int = 5) -> Any:
    from org_llm.org_tools import org_id_find
    return org_id_find(query, top_n=top_n)

def _t_doom_packages() -> Any:
    from org_llm.org_tools import doom_packages
    return doom_packages()

def _t_doom_keybinds() -> Any:
    from org_llm.org_tools import doom_keybinds
    return doom_keybinds()

# OpenAI-style tool definitions. Descriptions are short — the function names
# carry most of the signal, and shorter descs help fit qwen's 32k context
# along with persona + history.
TOOLS: dict[str, tuple[callable, dict]] = {
    "org_count_matches": (_t_count_matches, {
        "type": "function", "function": {
            "name": "org_count_matches",
            "description": "Count regex matches in org files. Anchors on [X]/DONE for event counts.",
            "parameters": {"type": "object",
                           "properties": {"pattern": {"type": "string"},
                                          "path_glob": {"type": "string"}},
                           "required": ["pattern"]}}}),
    "list_recent_nodes": (_t_list_recent_nodes, {
        "type": "function", "function": {
            "name": "list_recent_nodes", "description": "Org-roam nodes touched in the last N days.",
            "parameters": {"type": "object",
                           "properties": {"days": {"type": "integer"}}}}}),
    "recent_files": (_t_recent_files, {
        "type": "function", "function": {
            "name": "recent_files", "description": "Org files modified in the last N days.",
            "parameters": {"type": "object",
                           "properties": {"days": {"type": "integer"}}}}}),
    "list_dailies": (_t_list_dailies, {
        "type": "function", "function": {
            "name": "list_dailies", "description": "Most recent daily files; set include_content=true for body text.",
            "parameters": {"type": "object",
                           "properties": {"limit": {"type": "integer"},
                                          "include_content": {"type": "boolean"}}}}}),
    "get_fact": (_t_get_fact, {
        "type": "function", "function": {
            "name": "get_fact",
            "description": ("Read a precomputed vault fact by name. "
                            "Available: vault_stats, tag_taxonomy, routine_chores, mood_signal."),
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string",
                                                    "enum": ["vault_stats","tag_taxonomy","routine_chores","mood_signal"]}},
                           "required": ["name"]}}}),
    "org_agenda": (_t_org_agenda, {
        "type": "function", "function": {
            "name": "org_agenda", "description": "Today/upcoming/overdue items from org TODOs.",
            "parameters": {"type": "object",
                           "properties": {"window_days": {"type": "integer"}}}}}),
    "weather_for_agenda": (_t_weather_for_agenda, {
        "type": "function", "function": {
            "name": "weather_for_agenda", "description": "Forecast keyed for outdoor-flagged agenda items.",
            "parameters": {"type": "object",
                           "properties": {"days": {"type": "integer"}}}}}),
    "org_tag_index": (_t_org_tag_index, {
        "type": "function", "function": {
            "name": "org_tag_index", "description": "Top-N tags across the vault by frequency.",
            "parameters": {"type": "object",
                           "properties": {"limit": {"type": "integer"}}}}}),
    "org_grep": (_t_org_grep, {
        "type": "function", "function": {
            "name": "org_grep", "description": "Regex search across org files.",
            "parameters": {"type": "object",
                           "properties": {"pattern":     {"type": "string"},
                                          "path_glob":   {"type": "string"},
                                          "max_results": {"type": "integer"}},
                           "required": ["pattern"]}}}),
    "org_orphans": (_t_org_orphans, {
        "type": "function", "function": {
            "name": "org_orphans", "description": "Files with no in/out links and no tags.",
            "parameters": {"type": "object",
                           "properties": {"max_results": {"type": "integer"}}}}}),
    "org_property_search": (_t_org_property_search, {
        "type": "function", "function": {
            "name": "org_property_search", "description": "Find headings whose drawer has a property.",
            "parameters": {"type": "object",
                           "properties": {"prop":        {"type": "string"},
                                          "value":       {"type": "string"},
                                          "max_results": {"type": "integer"}},
                           "required": ["prop"]}}}),
    "org_id_find": (_t_org_id_find, {
        "type": "function", "function": {
            "name": "org_id_find", "description": "Fuzzy-find an org-roam node by title/alias.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"},
                                          "top_n": {"type": "integer"}},
                           "required": ["query"]}}}),
    "doom_packages": (_t_doom_packages, {
        "type": "function", "function": {
            "name": "doom_packages", "description": "User's enabled Doom Emacs packages.",
            "parameters": {"type": "object", "properties": {}}}}),
    "doom_keybinds": (_t_doom_keybinds, {
        "type": "function", "function": {
            "name": "doom_keybinds", "description": "User's Doom keybindings (leader + global).",
            "parameters": {"type": "object", "properties": {}}}}),
}


# ── cloud call ────────────────────────────────────────────────────────────────

def _ssl_ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
            if os.path.exists(p):
                return ssl.create_default_context(cafile=p)
        return ssl.create_default_context()

_SSL_CTX = _ssl_ctx()


def _cloud_post(endpoint: str, api_key: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Some providers (incl. openrouter) want a Referer / X-Title for ranked
    # listing. Optional, doesn't affect costs.
    headers["HTTP-Referer"] = "https://github.com/danielbenedict/org-llm"
    headers["X-Title"]      = "org-llm A/B recipe harness"
    url = endpoint.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def cloud_call(messages: list[dict], *, model: str, endpoint: str, api_key: str,
               tools: list[dict] | None = None, timeout: int = ROUND_TIMEOUT) -> dict:
    payload: dict = {"model": model, "messages": messages, "stream": False,
                     "temperature": 0.4}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    t0 = time.time()
    data = _cloud_post(endpoint, api_key, payload, timeout)
    elapsed = time.time() - t0
    return {"data": data, "elapsed_s": round(elapsed, 3)}


def _extract_usage(data: dict) -> dict:
    u = data.get("usage") or {}
    return {"prompt_tokens":     int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens":      int(u.get("total_tokens") or 0)}


# ── arm A (recipes on) ────────────────────────────────────────────────────────

def run_arm_a(prompt: str, *, model: str, endpoint: str, api_key: str) -> dict:
    rcp = match_recipe(prompt)
    rcp_name = rcp.name if rcp else None
    prefetch = ""
    runner_ms = 0
    advisory  = False
    if rcp:
        t0 = time.time()
        run = execute_recipe(rcp, prompt)
        runner_ms = int((time.time() - t0) * 1000)
        if run is not None:
            prefetch = format_prefetch_block(rcp.name, run, prompt)
        else:
            # Advisory fallback: inject the recipe body the way the proxy does.
            prefetch = rcp.body
            advisory = True
    system = PERSONA + (("\n\n" + prefetch) if prefetch else "")
    messages = [{"role": "system", "content": system},
                {"role": "user",   "content": prompt}]
    out = cloud_call(messages, model=model, endpoint=endpoint, api_key=api_key)
    msg = (out["data"].get("choices") or [{}])[0].get("message", {})
    return {"arm": "recipes_on",
            "recipe": rcp_name, "advisory": advisory,
            "runner_ms": runner_ms,
            "rounds":     1,
            "latency_s":  out["elapsed_s"],
            "usage":      _extract_usage(out["data"]),
            "tool_calls": [],
            "narration":  (msg.get("content") or "").strip()}


# ── arm B (recipes off — tool-use loop) ───────────────────────────────────────

def run_arm_b(prompt: str, *, model: str, endpoint: str, api_key: str) -> dict:
    messages = [{"role": "system", "content": PERSONA},
                {"role": "user",   "content": prompt}]
    tool_defs = [v[1] for v in TOOLS.values()]
    rounds: list[dict] = []
    tool_calls_log: list[dict] = []
    narration = ""
    for r in range(MAX_ROUNDS + 1):
        out = cloud_call(messages, model=model, endpoint=endpoint,
                         api_key=api_key, tools=tool_defs if r < MAX_ROUNDS else None)
        rounds.append({"latency_s": out["elapsed_s"],
                       "usage":     _extract_usage(out["data"])})
        msg = (out["data"].get("choices") or [{}])[0].get("message", {})
        tcs = msg.get("tool_calls") or []
        if not tcs:
            narration = (msg.get("content") or "").strip()
            break
        # Append assistant turn (with tool_calls) and run each tool locally.
        messages.append({"role": "assistant",
                         "content": msg.get("content"),
                         "tool_calls": tcs})
        for tc in tcs:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments")}
            tool_calls_log.append({"round": r, "name": name, "args": args})
            spec = TOOLS.get(name)
            if not spec:
                tool_result_str = json.dumps({"error": f"unknown tool: {name}"})
            else:
                try:
                    raw = spec[0](**args)
                    tool_result_str = _cap(raw)
                except Exception as e:
                    tool_result_str = json.dumps({"error": f"{type(e).__name__}: {e}"})
            messages.append({"role": "tool",
                             "tool_call_id": tc.get("id") or name,
                             "content": tool_result_str})
    total_latency = sum(r["latency_s"] for r in rounds)
    total_usage   = {"prompt_tokens":     sum(r["usage"]["prompt_tokens"]     for r in rounds),
                     "completion_tokens": sum(r["usage"]["completion_tokens"] for r in rounds),
                     "total_tokens":      sum(r["usage"]["total_tokens"]      for r in rounds)}
    return {"arm": "recipes_off",
            "recipe": None, "advisory": False, "runner_ms": 0,
            "rounds":     len(rounds),
            "latency_s":  round(total_latency, 3),
            "usage":      total_usage,
            "tool_calls": tool_calls_log,
            "narration":  narration}


# ── orchestrator ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",   action="store_true",
                    help="Validate prompt routing only — NO cloud calls.")
    ap.add_argument("--trials",    type=int, default=1,
                    help="Trials per (prompt, arm) — default 1.")
    ap.add_argument("--only-arm",  choices=["a","b","both"], default="both")
    ap.add_argument("--only-prompt", default="",
                    help="Comma-separated prompt IDs (e.g. 'A1,A2')")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    only = {x.strip() for x in args.only_prompt.split(",") if x.strip()}
    selected = [(pid, bucket, p) for (pid, bucket, p) in PROMPTS
                if not only or pid in only]

    if args.dry_run:
        print("DRY RUN — recipe routing per prompt:")
        for pid, bucket, p in selected:
            r = match_recipe(p)
            name = r.name if r else "(none)"
            target = r.target if r else "-"
            run = execute_recipe(r, p) if r else None
            runner_status = "advisory" if (r and run is None) else (
                "ok" if run else "-")
            print(f"  {pid:<3} [{bucket:<7}] recipe={name:<28} "
                  f"target={target:<11} runner={runner_status:<8} {p[:80]}")
        return 0

    target = _resolve_cloud_failover_target()
    if not target or not target.get("api_key"):
        print("ERROR: cloud not configured (cloud_endpoint_url / api_key). "
              "Run `org-llm cloud --connect` and try again.", file=sys.stderr)
        return 2
    endpoint = target["endpoint"]
    model    = target["model"]
    api_key  = target["api_key"]
    print(f"endpoint={endpoint}  model={model}  trials={args.trials}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("")  # truncate

    arms = ([] if args.only_arm == "b" else ["a"]) + \
           ([] if args.only_arm == "a" else ["b"])
    n_total = len(selected) * len(arms) * args.trials
    n_done = 0
    for trial in range(args.trials):
        for pid, bucket, p in selected:
            for arm in arms:
                n_done += 1
                t0 = time.time()
                try:
                    rec = (run_arm_a(p, model=model, endpoint=endpoint, api_key=api_key)
                           if arm == "a" else
                           run_arm_b(p, model=model, endpoint=endpoint, api_key=api_key))
                    rec.update({"prompt_id": pid, "bucket": bucket, "prompt": p,
                                "trial": trial, "model": model})
                except Exception as e:
                    rec = {"prompt_id": pid, "bucket": bucket, "prompt": p,
                           "trial": trial, "arm": arm,
                           "error": f"{type(e).__name__}: {e}"}
                rec["wall_s"] = round(time.time() - t0, 3)
                with args.out.open("a") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                err = rec.get("error", "")
                lat = rec.get("latency_s", 0)
                tok = rec.get("usage", {}).get("total_tokens", 0)
                rounds = rec.get("rounds", "?")
                print(f"  [{n_done}/{n_total}] {pid} {arm} t{trial} "
                      f"rounds={rounds} lat={lat}s tok={tok} {err}", flush=True)
    print(f"\nDONE → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
