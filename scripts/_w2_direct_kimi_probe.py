#!/usr/bin/env python3
"""W2 — direct OpenRouter API probe for Kimi K2.6's tool-use capability.

Per the round-12 partial findings: opencode + non-qwen FOSS silently
fails to produce output. This test bypasses opencode entirely:
  1. Read a small target file directly.
  2. POST chat/completions to OpenRouter with the file content + an
     `edit_file` tool definition.
  3. Parse the model's response — does it emit a well-formed tool_call?
  4. Apply the edit to a copy + verify.

If Kimi K2.6 emits a clean tool_call here → capability confirmed; opencode
is the bottleneck. If it fails the same way → model-level issue.

Probes (3 model-task pairs, sequential):
  P1 — Kimi K2.6 + simple cross-link wrap (B1-shape, single edit)
  P2 — Kimi K2.6 + LICENSE year bump (B13-shape, mechanical)
  P3 — Qwen3-coder-30b + same cross-link wrap (control — known-good model)
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path("/home/daniel/repos/org-llm")
ARTIFACTS = REPO / "scripts/_w2_direct_kimi_probe_artifacts"
ARTIFACTS.mkdir(exist_ok=True)
EPOCH = int(time.time())


def call_openrouter(model: str, messages: list, tools: list | None = None,
                     temperature: float = 0.1, max_tokens: int = 2000) -> dict:
    or_key = subprocess.run(["pass", "org-llm/cloud/openrouter/api-key"],
                              capture_output=True, text=True, check=True).stdout.strip()
    body: dict = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {or_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try: err_body = e.read().decode()
        except Exception: err_body = "(no body)"
        return {"_http_error": e.code, "_err_body": err_body}


EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": ("Edit a file by replacing one exact occurrence of "
                         "old_string with new_string. Both strings must match "
                         "the file content exactly, including whitespace."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}


def assess_response(resp: dict) -> dict:
    """Classify the model's response."""
    if "_http_error" in resp:
        return {"status": "http_error", "code": resp["_http_error"],
                 "body": resp.get("_err_body", "")[:300]}
    choices = resp.get("choices") or []
    if not choices:
        return {"status": "no_choices", "raw": json.dumps(resp)[:300]}
    msg = choices[0].get("message") or {}
    finish_reason = choices[0].get("finish_reason")
    text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    usage = resp.get("usage") or {}
    return {
        "status": "ok" if (tool_calls or text) else "empty",
        "finish_reason": finish_reason,
        "text_length": len(text or ""),
        "text_preview": (text or "")[:300],
        "tool_calls_count": len(tool_calls),
        "tool_calls": [
            {"name": tc.get("function", {}).get("name"),
             "arguments": tc.get("function", {}).get("arguments", "")[:500]}
            for tc in tool_calls
        ],
        "cost": float(usage.get("cost") or 0),
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def apply_edit_call(workdir: Path, args: dict) -> tuple[bool, str]:
    """Apply an edit_file tool_call to a file under workdir. Returns (ok, msg)."""
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not path:
        return False, "missing path"
    target = Path(path)
    if not target.exists():
        return False, f"file not found: {path}"
    text = target.read_text()
    if old not in text:
        return False, f"old_string not found in file (len {len(old)} chars)"
    if text.count(old) > 1:
        return False, f"old_string matches {text.count(old)} times — ambiguous"
    new_text = text.replace(old, new, 1)
    target.write_text(new_text)
    return True, "applied"


# ── Probe definitions ────────────────────────────────────────────────────
def make_probe_workdir(name: str) -> Path:
    """Set up a clean workdir with the target files."""
    wd = ARTIFACTS / f"workdir-{name}-{EPOCH}"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def build_p1_prompt(workdir: Path) -> tuple[list, list]:
    """P1: cross-link wrap on a small synthetic page."""
    page = workdir / "test-page.org"
    page.write_text("""\
:PROPERTIES:
:ID:       0123abcd-0000-0000-0000-000000000001
:CREATED:  [2026-05-07]
:END:
#+TITLE: Test page

* Summary

This page mentions agents.org as a related concept and also
references DEC-014 in passing.

* See also

- agents.org — the agent surface
- DEC-014 — the cull rubric
""")
    user_msg = (
        f"You are a wiki concept-graph specialist. The file at "
        f"`{page}` mentions `agents.org` on line 9 (in the body prose).\n\n"
        f"Use the `edit_file` tool to wrap that single mention in an "
        f"org-mode id-link: replace `agents.org` with "
        f"`[[id:a8f4c2e1-9b3d-4e5f-87a6-c1d2e3f4b5a6][agents.org]]`. "
        f"Make exactly ONE edit (the body-prose mention, not the See-also "
        f"line). Use the `edit_file` tool to apply your change.\n\n"
        f"FILE CONTENT (line-numbered for your reference):\n"
        + "\n".join(f"{i+1}: {ln}" for i, ln in enumerate(page.read_text().splitlines()))
    )
    return [{"role": "user", "content": user_msg}], [EDIT_TOOL]


def build_p2_prompt(workdir: Path) -> tuple[list, list]:
    """P2: LICENSE year bump (mechanical edit)."""
    license_path = workdir / "LICENSE"
    license_path.write_text("Copyright (C) 2025 Daniel.\n\nGNU AGPL v3.0...\n")
    user_msg = (
        f"Use the `edit_file` tool to update the copyright year in "
        f"`{license_path}` from 2025 to 2026. Make exactly one edit.\n\n"
        f"FILE CONTENT:\n{license_path.read_text()}"
    )
    return [{"role": "user", "content": user_msg}], [EDIT_TOOL]


# ── Main ─────────────────────────────────────────────────────────────────
print(f"=== W2 PROBE — direct OpenRouter API; can FOSS models do tool-use? ===\n")

probes = [
    ("P1-kimi-k2.6-crosslink", "moonshotai/kimi-k2.6", build_p1_prompt),
    ("P2-kimi-k2.6-license",   "moonshotai/kimi-k2.6", build_p2_prompt),
    ("P3-qwen30-crosslink",    "qwen/qwen3-coder-30b-a3b-instruct", build_p1_prompt),
]

results = []
for probe_name, model, build_fn in probes:
    print("─" * 60)
    print(f"PROBE {probe_name}  ({model})")
    print("─" * 60)
    wd = make_probe_workdir(probe_name)
    messages, tools = build_fn(wd)
    print(f"  prompt: {len(messages[0]['content'])} chars")
    print(f"  tool defined: {tools[0]['function']['name']}")
    print(f"  POST chat/completions...")
    t0 = time.time()
    resp = call_openrouter(model, messages, tools=tools)
    elapsed = time.time() - t0
    (wd / "raw_response.json").write_text(json.dumps(resp, indent=2))
    summary = assess_response(resp)
    summary["wall_seconds"] = round(elapsed, 1)
    print(f"  rc: {summary['status']}  finish_reason: {summary.get('finish_reason')}")
    print(f"  wall: {elapsed:.1f}s  cost: ${summary.get('cost', 0):.4f}")
    print(f"  in/out tokens: {summary.get('input_tokens')}/{summary.get('output_tokens')}")
    print(f"  text_length: {summary.get('text_length')}")
    print(f"  tool_calls_count: {summary.get('tool_calls_count')}")
    if summary.get("tool_calls"):
        for tc in summary["tool_calls"]:
            print(f"    tool_call: {tc['name']}  args(first 200): {tc['arguments'][:200]}")
            # Try to apply
            try:
                args = json.loads(tc["arguments"])
                ok, msg = apply_edit_call(wd, args)
                print(f"    apply_edit_call: ok={ok} msg={msg}")
                summary["edit_applied"] = ok
                summary["edit_msg"] = msg
            except json.JSONDecodeError as e:
                print(f"    arguments not valid JSON: {e}")
                summary["edit_applied"] = False
                summary["edit_msg"] = f"json decode err: {e}"
    elif summary.get("text_preview"):
        print(f"  text preview: {summary['text_preview']}")
    print()
    results.append({"probe": probe_name, "model": model, "summary": summary,
                     "workdir": str(wd)})

(ARTIFACTS / f"summary-{EPOCH}.json").write_text(json.dumps(results, indent=2))

print("=" * 60)
print("W2 SUMMARY")
print("=" * 60)
for r in results:
    s = r["summary"]
    edit_applied = s.get("edit_applied", False)
    tc_count = s.get("tool_calls_count", 0)
    print(f"  {r['probe']:35} status={s.get('status'):>10} "
          f"tools={tc_count}  applied={edit_applied}  ${s.get('cost',0):.4f}")
print()

# Verdict
kimi_p1 = next(r for r in results if r["probe"] == "P1-kimi-k2.6-crosslink")
kimi_p2 = next(r for r in results if r["probe"] == "P2-kimi-k2.6-license")
qwen_p3 = next(r for r in results if r["probe"] == "P3-qwen30-crosslink")

if kimi_p1["summary"].get("edit_applied") or kimi_p2["summary"].get("edit_applied"):
    print("✓ Kimi K2.6 CAN do tool-use file-edits via direct API")
    print("  → opencode is the bottleneck (matches the round-12 finding)")
    print("  → W1 (direct-API specialist harness) is a viable workaround")
elif kimi_p1["summary"].get("text_length", 0) > 50:
    print("? Kimi K2.6 produces text but didn't issue tool_calls")
    print("  → may need different prompt format / system message / tool spec")
else:
    print("✗ Kimi K2.6 produced nothing via direct API")
    print("  → suggests model-level issue (or OpenRouter endpoint issue)")

if qwen_p3["summary"].get("edit_applied"):
    print("✓ qwen30 control: tool-use works (sanity check)")
elif qwen_p3["summary"].get("tool_calls_count", 0) > 0:
    print("? qwen30 issued tool_call but apply failed")
else:
    print("✗ qwen30 control failed too — broader API/auth issue")

print()
print(f"artifacts: {ARTIFACTS}")
