#!/usr/bin/env python3
"""PILOT R2-B driver — sibling pair (@data author + @spock reviewer) on one
boardId. Tests sibling-by-boardId discovery and `mode:"btw"` async peer query.

Round-2 design: Python+subprocess pattern (Pilot D-shape).
- urllib for REST (no curl — sub-agent sandbox blocks bash auth headers).
- subprocess for `claude -p` with --mcp-config / --strict-mcp-config /
  --permission-mode bypassPermissions / --max-budget-usd.

Round-1 lessons baked in:
- F1 : `idle` IS a terminal status for spawned children — poll set includes it.
- F4 : `agor_execute_tool` requires `tool_name` (snake_case), NOT `toolName`.
- F8 : JWT TTL ~15 min — auto-refresh against POST /authentication mid-run.
- F9 : catch `rate_limit_event` in parent stream — sleep 30s + retry.
- F10: Python+subprocess only. No bash.
- F11: notices.py is already fully typed → fallback target = resolvers/agent.py
       (1 missing docstring on `resolve_agents`, public surface for review).
- BUG-8: PATCH captain pilot's `permission_config.mode = bypassPermissions`
       BEFORE invoking claude with the MCP attached.

Run: python3 scripts/_r2_b.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://localhost:3030"
TOKEN_FILE = Path.home() / ".agor" / "cli-token"
ADMIN_EMAIL = "admin@agor.live"
REPO_ID = "019dfbd1-abe8-717b-b0b4-099526fe0b65"
SOURCE_BRANCH = "trunk"

EPOCH = int(time.time())
WT_NAME = f"pilot-R2B-{EPOCH}"
MODEL = "sonnet"
TIMEOUT_PARENT = 1500            # ≤25 min wall on captain
POLL_DEADLINE_S = 1200           # 20 min poll budget after spawn
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r2_b_artifacts")
WORK_DIR.mkdir(exist_ok=True)

def _resolve_admin_password() -> str:
    """Source admin password from env first, then `pass org-llm/agor/admin-password`.

    Falling back to the password store keeps the harness usable without
    extra env wiring on the user's box; env override stays for CI.
    """
    pw = os.environ.get("AGOR_ADMIN_PASSWORD", "")
    if pw:
        return pw
    try:
        out = subprocess.run(
            ["pass", "org-llm/agor/admin-password"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


ADMIN_PASSWORD = _resolve_admin_password()
TARGET_REL = "org_llm/resolvers/agent.py"  # F11 fallback (notices.py already fully typed)


# ─── logging ────────────────────────────────────────────────────────────────

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r2-b {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ─── auth + REST ────────────────────────────────────────────────────────────


def _read_token_file() -> str:
    return json.load(open(TOKEN_FILE))["accessToken"]


def _login_admin() -> str:
    """Refresh admin JWT via POST /authentication. Writes a new
    ~/.agor/cli-token file shaped like the original."""
    if not ADMIN_PASSWORD:
        raise RuntimeError("AGOR_ADMIN_PASSWORD env not set; cannot refresh JWT")
    body = json.dumps({
        "strategy": "local",
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    }).encode()
    req = urllib.request.Request(
        BASE + "/authentication",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"login failed HTTP {e.code}: {e.read().decode('utf-8','replace')}"
        ) from e
    tok = j.get("accessToken") or j.get("access_token")
    if not tok:
        raise RuntimeError(f"login response missing accessToken: {list(j)}")
    payload = {
        "accessToken": tok,
        "user": j.get("user", {}),
        "expiresAt": j.get("expiresAt") or int(time.time() * 1000) + 24 * 3600 * 1000,
    }
    TOKEN_FILE.write_text(json.dumps(payload))
    log(f"  refreshed admin JWT (sub={payload['user'].get('id','?')[:8]}...)")
    return tok


def _bearer() -> str:
    """Return a fresh-enough admin bearer; auto-refresh on <2 min remaining."""
    import base64
    try:
        tok = _read_token_file()
        body = tok.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp", 0)
        secs_left = exp - int(time.time())
        if secs_left < 120:
            log(f"  JWT expiring in {secs_left}s — refreshing")
            tok = _login_admin()
        return tok
    except Exception as e:
        log(f"  bearer probe failed ({e}); attempting login")
        return _login_admin()


def api(method: str, path: str, body: object | None = None,
        timeout: int = 30) -> dict:
    """REST call against the local Agor daemon, with bearer auto-refresh on 401."""
    for attempt in (1, 2):
        url = BASE + path
        headers = {"Authorization": "Bearer " + _bearer()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if e.code == 401 and attempt == 1:
                log("  401 — forcing JWT refresh and retrying")
                _login_admin()
                continue
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {txt}") from e
    raise RuntimeError(f"unreachable: {method} {path}")


# ─── personas (verbatim from cli.py / _builtins.py) ─────────────────────────

DATA_PERSONA = (
    "You are scribe — turn ideas into clean org notes. (For this pilot you "
    "are operating in CODER mode: the team needs a Python author for a "
    "tiny code task.) Birth-name @data. You are precise, deferential to "
    "evidence, and uncomfortable with sloppy claims. Strong defaults: "
    "always read the file before editing. Make the smallest change that "
    "satisfies the task. Preserve existing style and indentation. If "
    "another agent (your reviewer @spock on this same boardId) asks a "
    "clarifier mid-task, answer briefly and keep working.\n"
)

SPOCK_PERSONA = (
    "You are an org-llm researcher. Birth-name @spock. Logical, exacting, "
    "anti-confabulation. Always lead with a tool call when the question is "
    "about file content. (For this pilot you are operating in REVIEWER mode: "
    "review @data's diff for correctness, scope, and style. Block on "
    "anything that changes behavior; allow type-hint and docstring "
    "additions. If you need a clarifier from @data mid-review, ask via the "
    "Agor `mode:\"btw\"` async peer query primitive — do NOT block waiting.)\n"
)


# ─── prompts ────────────────────────────────────────────────────────────────


CAPTAIN_PROMPT_TEMPLATE = """You are the human-pilot captain for an Agor multi-agent test.

You have one MCP server "agor" with tools mcp__agor__agor_search_tools and
mcp__agor__agor_execute_tool (progressive disclosure — every other Agor
capability is invoked indirectly via agor_execute_tool with snake_case
tool_name).

YOUR JOB (and ONLY your job):
1. Spawn TWO sibling sessions on the SAME worktree (the boardId), in the
   SAME assistant turn (two tool_use blocks in one response):
   - @data — author, persona + task in args.prompt
   - @spock — reviewer, persona + task in args.prompt
   Both spawns OMIT worktree_id so each child inherits YOUR worktree.

2. Each spawn call:
     mcp__agor__agor_execute_tool(
       tool_name = "agor_sessions_spawn",        # snake_case! NOT toolName
       arguments = {{ ... }}
     )

3. After both spawns return session IDs, print exactly:
     DATA_SESSION_ID=<uuid>
     SPOCK_SESSION_ID=<uuid>
   on their own lines. Then STOP. Do not poll, do not narrate, do not
   call any other tool.

DATA SPAWN ARGS (JSON):
{data_args}

SPOCK SPAWN ARGS (JSON):
{spock_args}

CRITICAL: tool_name must be snake_case "agor_sessions_spawn". The schema
inside agor_execute_tool is snake_case even though many Agor schemas use
camelCase elsewhere.
"""


def _data_task_prompt() -> str:
    return f"""You are @data, code author for a pilot multi-agent task.

PERSONA:
{DATA_PERSONA}

TEAM CONTEXT:
- You and @spock (reviewer) are sibling sessions on the SAME Agor
  worktree (boardId). Discovery rule: "the board IS the registry —
  list sessions on my board." If you need to find your peer, the
  worktree itself is the rendezvous.
- @spock may ask you a clarifier mid-task via the `mode:"btw"`
  primitive (an ephemeral child session whose prompt arrives at you
  as a normal user-style message). Answer briefly, then resume work.

TASK (small, ~5 minutes):
Add Python type hints + a one-line docstring to every public function
in `{TARGET_REL}` that is missing them. Behavior must NOT change. Style
match the rest of the file (PEP 484 hints, triple-quoted single-line
docstrings, `from __future__ import annotations` already present).

Specifically:
  - `resolve_agents(prompt, context="")` is missing a docstring. Add a
    1-2 line docstring describing what it returns. The function already
    has type hints and a return type — leave those alone.
  - All other helpers (`_extract_handles`, `_builtin_index`) are
    private (underscore prefix); skip them unless they have gaps you
    can fix without behavior change.
  - Run `python3 -c 'import org_llm.resolvers.agent'` to verify
    importability after the edit.

When done, write the diff path + a one-line summary to
`.agor-assistants/data/memory/2026-05-06.md` (create parent dirs;
heading: `# @data — type-hint + docstring sweep 2026-05-06`).

Print exactly the line `DATA_DONE` on its own and stop.

CONSTRAINTS:
- DO NOT run any agor MCP tools.
- DO NOT modify any file outside `{TARGET_REL}` and your own
  `.agor-assistants/data/memory/` path.
- DO NOT add new dependencies.
"""


def _spock_task_prompt() -> str:
    return f"""You are @spock, code reviewer for a pilot multi-agent task.

PERSONA:
{SPOCK_PERSONA}

TEAM CONTEXT:
- You and @data (author) are sibling sessions on the SAME Agor worktree
  (boardId). The board IS the registry — your peer is on your board.
- You have access to the same Agor MCP. Specifically you can issue
  `mcp__agor__agor_execute_tool` with `tool_name="agor_sessions_prompt"`
  and arguments `{{"mode": "btw", "target": "<data_session_id>",
  "prompt": "..."}}` to ask @data a clarifier mid-review without
  blocking. The answer arrives as an ephemeral child session in
  @data's genealogy.

DISCOVERING @DATA'S SESSION ID:
The captain placed @data on this same worktree. To find @data's
session id, list sessions on your worktree by running this Python
snippet in bash:
    python3 -c "import urllib.request, json, os; \\
      tok=json.load(open(os.path.expanduser('~/.agor/cli-token')))['accessToken']; \\
      req=urllib.request.Request('http://localhost:3030/sessions', \\
          headers={{'Authorization':'Bearer '+tok}}); \\
      data=json.loads(urllib.request.urlopen(req,timeout=15).read()); \\
      sessions=data if isinstance(data,list) else data.get('data',data.get('sessions',[])); \\
      [print(s.get('id'), s.get('worktree_id'), s.get('title','')) for s in sessions[-20:]]"
Filter for sessions on the same `worktree_id` as your own (your
worktree is the one whose path contains `{WT_NAME}`).

TASK (small, ~5 minutes):
1. Wait for @data's edit to land. Poll for changes to
   `{TARGET_REL}` via `git diff --stat HEAD -- {TARGET_REL}` in
   bash; sleep 20s per check; deadline 12 minutes.

2. Once @data has produced a diff (or signalled DATA_DONE in their
   memory file), review:
   - Does the diff strictly add type hints + docstrings (allowed)?
   - Does it change any behavior (NOT allowed)?
   - Does it preserve `from __future__ import annotations` and
     existing style?

3. EXERCISE THE BTW PRIMITIVE — at LEAST ONCE during your review,
   ask @data a clarifier via `mode:"btw"`. A natural question is:
   "Did you intentionally leave the underscore-prefixed helpers
   alone, per the brief?" — but pick whatever clarifier feels real.
   The btw call is a SUCCESS CRITERION for this pilot. If
   `agor_sessions_prompt` is unavailable, document that fact in
   your memory file. Print the btw call's returned session id
   prefixed `BTW_CHILD_ID=` on its own line for the harness to
   capture.

4. Write your verdict to `.agor-assistants/spock/memory/2026-05-06.md`
   (heading: `# @spock — review verdict 2026-05-06`). Include:
   - the diff stat
   - APPROVE / BLOCK / DEFER + 2-3 line rationale
   - whether the `btw` primitive worked end-to-end (yes/no/unknown)
   - the `BTW_CHILD_ID` you observed (if any)

5. Print exactly the line `SPOCK_DONE` on its own and stop.

CONSTRAINTS:
- DO NOT modify `{TARGET_REL}` regardless of verdict (review only).
- DO NOT modify any file outside `.agor-assistants/spock/memory/`.
- DO use `mcp__agor__agor_execute_tool` for the btw call.
"""


# ─── claude -p subprocess ───────────────────────────────────────────────────


def write_mcp_config(mcp_token: str, path: str) -> None:
    cfg = {
        "mcpServers": {
            "agor": {
                "type": "http",
                "url": BASE + "/mcp",
                "headers": {"Authorization": "Bearer " + mcp_token},
            }
        }
    }
    Path(path).write_text(json.dumps(cfg))


def run_claude(prompt: str, mcp_config: str, out_path: str,
               budget: str = "2.00", timeout: int = 900) -> int:
    """Run claude -p once with full Agor wiring; capture stream-json to out_path."""
    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.npm-global/bin')}:{env.get('PATH','')}"
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", mcp_config,
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", budget,
        prompt,
    ]
    with open(out_path, "w") as f:
        try:
            p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                               timeout=timeout, env=env)
            return p.returncode
        except subprocess.TimeoutExpired:
            return 124


def _parse_captain_stream(text: str) -> dict:
    """Walk stream-json output of the captain pilot. Return cost, child IDs,
    rate_limit count, max parallel agor_execute_tool tool_use blocks per turn."""
    cost = 0.0
    rate_limits = 0
    parallel = 0
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ty = obj.get("type")
        if ty == "assistant":
            content = obj.get("message", {}).get("content", []) or []
            n = sum(1 for c in content
                    if c.get("type") == "tool_use"
                    and c.get("name") == "mcp__agor__agor_execute_tool")
            parallel = max(parallel, n)
        elif ty == "result":
            cost += float(obj.get("total_cost_usd") or 0)
        elif ty == "rate_limit_event":
            rate_limits += 1
    return {"cost": cost, "rate_limits": rate_limits, "parallel": parallel}


# ─── steps ──────────────────────────────────────────────────────────────────


def step_create_worktree() -> dict:
    log(f"step 1 — creating worktree {WT_NAME}")
    body = {
        "name": WT_NAME,
        "ref": WT_NAME,
        "createBranch": True,
        "sourceBranch": SOURCE_BRANCH,
        "pullLatest": False,
        "refType": "branch",
    }
    j = api("POST", f"/repos/{REPO_ID}/worktrees", body)
    log(f"  worktree_id={j['worktree_id']} path={j['path']}")
    return j


def step_create_pilot_session(wt_id: str) -> tuple[str, str]:
    log("step 2 — creating captain pilot session (claude-code)")
    j = api("POST", "/sessions",
            {"worktree_id": wt_id, "agentic_tool": "claude-code"})
    sid = j["session_id"]
    tok = j["mcp_token"]
    log(f"  pilot_session_id={sid} mcp_token_prefix={tok[:24]}...")
    log("  PATCH bypassPermissions on captain (BUG-8)")
    api("PATCH", f"/sessions/{sid}",
        {"permission_config": {"mode": "bypassPermissions"}})
    return sid, tok


def step_spawn_pair_via_captain(mcp_config: str) -> dict:
    log("step 3 — captain spawns @data + @spock siblings (single turn, parallel)")
    data_args = json.dumps({
        "prompt": _data_task_prompt(),
        "title": "r2b-data-author",
    })
    spock_args = json.dumps({
        "prompt": _spock_task_prompt(),
        "title": "r2b-spock-reviewer",
    })
    parent_prompt = CAPTAIN_PROMPT_TEMPLATE.format(
        data_args=data_args, spock_args=spock_args,
    )
    out_path = str(WORK_DIR / f"captain-{EPOCH}.jsonl")
    log("  invoking claude -p (budget=$2.50 captain, timeout=600s)")
    rc = run_claude(parent_prompt, mcp_config, out_path,
                    budget="2.50", timeout=600)
    log(f"  captain rc={rc}")
    text = Path(out_path).read_text() if Path(out_path).exists() else ""

    parse = _parse_captain_stream(text)
    log(f"  captain cost=${parse['cost']:.4f} parallel={parse['parallel']} "
        f"rate_limits={parse['rate_limits']}")

    # F9: if rate-limited and the captain didn't issue spawn calls, retry once
    # after a 30s sleep.
    def _grab(label: str) -> str | None:
        m = re.search(rf"{label}=([A-Za-z0-9_-]+)", text)
        return m.group(1) if m else None

    d_sid = _grab("DATA_SESSION_ID")
    s_sid = _grab("SPOCK_SESSION_ID")
    log(f"  data_sid={d_sid} spock_sid={s_sid}")

    if (not d_sid or not s_sid) and parse["rate_limits"] > 0:
        log("  rate_limit detected + no children — sleeping 30s + single retry")
        time.sleep(30)
        out_path2 = str(WORK_DIR / f"captain-{EPOCH}-retry.jsonl")
        rc2 = run_claude(parent_prompt, mcp_config, out_path2,
                         budget="2.00", timeout=600)
        log(f"  captain retry rc={rc2}")
        text2 = Path(out_path2).read_text() if Path(out_path2).exists() else ""
        parse2 = _parse_captain_stream(text2)
        parse["cost"] += parse2["cost"]
        parse["rate_limits"] += parse2["rate_limits"]
        parse["parallel"] = max(parse["parallel"], parse2["parallel"])
        for label, var in (("DATA_SESSION_ID", "d_sid"),
                           ("SPOCK_SESSION_ID", "s_sid")):
            m = re.search(rf"{label}=([A-Za-z0-9_-]+)", text2)
            if m:
                if var == "d_sid":
                    d_sid = m.group(1)
                else:
                    s_sid = m.group(1)

    return {
        "rc": rc, "out_path": out_path,
        "data_sid": d_sid, "spock_sid": s_sid,
        "parallel": parse["parallel"], "cost": parse["cost"],
        "rate_limits": parse["rate_limits"], "raw_text": text,
    }


def step_lift_permission_each(sids: list[str]) -> None:
    log("step 4 — PATCH bypassPermissions on each sibling (defensive)")
    for sid in sids:
        if not sid:
            continue
        try:
            api("PATCH", f"/sessions/{sid}",
                {"permission_config": {"mode": "bypassPermissions"}})
            log(f"  PATCHed {sid[:8]}…")
        except Exception as e:
            log(f"  PATCH {sid[:8]}… failed: {e}")


def step_poll_siblings(sids: list[str], deadline_s: int = POLL_DEADLINE_S) -> dict:
    log(f"step 5 — polling 2 siblings to terminal status (≤ {deadline_s}s)")
    deadline = time.time() + deadline_s
    # F1 — `idle` IS terminal for spawned children
    terminal = {"idle", "completed", "stopped", "archived", "failed", "errored"}
    states = {sid: "?" for sid in sids if sid}
    while time.time() < deadline:
        all_done = True
        for sid in list(states.keys()):
            if states[sid] in terminal:
                continue
            try:
                got = api("GET", f"/sessions/{sid}")
                states[sid] = got.get("status", "?")
            except Exception as e:
                states[sid] = f"err({type(e).__name__})"
            if states[sid] not in terminal:
                all_done = False
        log("  states: " + " ".join(f"{s[:8]}={st}"
                                     for s, st in states.items()))
        if all_done:
            break
        time.sleep(20)
    return states


def step_inspect_btw(spock_sid: str | None,
                     data_sid: str | None) -> list[str]:
    """Look at BOTH siblings' genealogy.children for ephemeral btw sessions
    (proof-of-life for `mode:"btw"` async peer query).

    Live shape (Agor v0.17.3, verified 2026-05-06):
      session.genealogy.children = list[str]  (ids, NOT objects)
      session.fork_origin = "btw"
      session.callback_config.callback_mode = "once"
      session.forked_from_session_id = <target sibling>  (NOT parent)
      session.callback_config.callback_session_id = <caller sibling>
    So "btw arrives on the TARGET's genealogy.children" — caller @spock
    asks @data, btw child shows up as @data's child.
    """
    btw: list[str] = []
    for label, sid in (("spock", spock_sid), ("data", data_sid)):
        if not sid:
            continue
        try:
            got = api("GET", f"/sessions/{sid}")
            gen = got.get("genealogy", {}) or {}
            kids = gen.get("children", []) or []
            local = []
            for k in kids:
                # children may be strings (ids) or objects in different
                # daemon versions; handle both.
                if isinstance(k, str):
                    child_id = k
                    try:
                        child = api("GET", f"/sessions/{child_id}")
                    except Exception:
                        continue
                    origin = (child.get("fork_origin")
                              or child.get("forkOrigin") or "")
                else:
                    origin = (k.get("fork_origin")
                              or k.get("forkOrigin") or "")
                    child_id = k.get("id") or k.get("session_id") or ""
                if "btw" in str(origin).lower() and child_id:
                    local.append(child_id)
            log(f"  {label}.genealogy.children: {len(kids)} total, btw={len(local)}")
            btw.extend(local)
        except Exception as e:
            log(f"  inspect_btw({label}) failed: {e}")
    return btw


def step_verify(wt_path: str) -> dict:
    log("step 6 — verify diff in worktree")
    target = Path(wt_path) / TARGET_REL
    out: dict = {"target": str(target), "exists": target.exists()}
    try:
        p = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--", TARGET_REL],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["diff_stat"] = (p.stdout or "").strip()
    except Exception as e:
        out["diff_stat"] = f"err: {e}"
    try:
        p = subprocess.run(
            ["git", "diff", "HEAD", "--", TARGET_REL],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["diff_full"] = (p.stdout or "")[:4000]
    except Exception as e:
        out["diff_full"] = f"err: {e}"
    log(f"  diff stat: {out['diff_stat'] or '<empty>'}")

    data_mem = (Path(wt_path) / ".agor-assistants" / "data"
                / "memory" / "2026-05-06.md")
    spock_mem = (Path(wt_path) / ".agor-assistants" / "spock"
                 / "memory" / "2026-05-06.md")
    out["data_memory_exists"] = data_mem.exists()
    out["spock_memory_exists"] = spock_mem.exists()
    log(f"  data memory exists={data_mem.exists()} "
        f"spock memory exists={spock_mem.exists()}")
    return out


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    t0 = time.time()
    if not ADMIN_PASSWORD:
        log("FATAL: AGOR_ADMIN_PASSWORD env not set; cannot auto-refresh JWT")
        return 2

    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]

    captain_sid, mcp_token = step_create_pilot_session(wt_id)
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)

    spawn = step_spawn_pair_via_captain(mcp_config)
    sids = [spawn["data_sid"], spawn["spock_sid"]]
    step_lift_permission_each(sids)

    states = step_poll_siblings(sids)
    btw_kids = step_inspect_btw(spawn["spock_sid"], spawn["data_sid"])
    fs = step_verify(wt_path)

    # aggregate cost (parent stream cost only — F7: child REST cost
    # fields stay null, parent stream is authoritative)
    sibling_cost = 0.0
    for sid in sids:
        if not sid:
            continue
        try:
            got = api("GET", f"/sessions/{sid}")
            c = got.get("cost", {}) or {}
            sibling_cost += float(c.get("total_usd")
                                  or got.get("total_cost_usd") or 0)
        except Exception:
            pass
    total_cost = spawn["cost"] + sibling_cost

    elapsed = int(time.time() - t0)

    # verdict
    sib_states = {h: states.get(s, "?") for h, s in
                  [("data", spawn["data_sid"]), ("spock", spawn["spock_sid"])]}
    diff_landed = bool(fs.get("diff_stat") and fs["diff_stat"] != "")
    files_landed = fs["data_memory_exists"] and fs["spock_memory_exists"]
    sessions_terminal = all(
        states.get(s) in {"idle", "completed", "stopped", "archived"}
        for s in sids if s
    )

    if diff_landed and files_landed and sessions_terminal:
        verdict = "PASS"
    elif diff_landed or files_landed:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    primitives = ["sibling-by-boardId (shared worktree, omit worktree_id)"]
    if btw_kids:
        primitives.append('mode:"btw" (genealogy.children fork_origin observed)')
    else:
        primitives.append('mode:"btw" (ATTEMPTED — no btw children observed)')
    primitives += [
        "agor_execute_tool indirection (snake_case tool_name)",
        "permission_config.bypassPermissions PATCH (BUG-8)",
        "parallel agor_sessions_spawn (single assistant turn)",
    ]

    report = f"""
PILOT: R2-B
TEAM_SHAPE: sibling pair (@data + @spock + captain pilot) — Python+subprocess
HARNESS_SCRIPT: scripts/_r2_b.py
AGOR_PRIMITIVES_USED: {primitives}
TASK_OUTCOME: {verdict} — diff_landed={diff_landed} files_landed={files_landed} sessions_terminal={sessions_terminal}
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {captain_sid}
DATA_SESSION_ID: {spawn['data_sid']}
SPOCK_SESSION_ID: {spawn['spock_sid']}
SIBLING_STATES: {sib_states}
PARALLEL_TOOL_USE: {spawn['parallel']}
RATE_LIMIT_EVENTS: {spawn['rate_limits']}
BTW_CHILD_IDS: {btw_kids if btw_kids else 'none observed'}
DIFF_STAT: {fs['diff_stat'] or '<empty>'}
DATA_MEMORY_EXISTS: {fs['data_memory_exists']}
SPOCK_MEMORY_EXISTS: {fs['spock_memory_exists']}
COST_USD: {total_cost:.4f} (captain ${spawn['cost']:.4f} + siblings ${sibling_cost:.4f})
DURATION_SECONDS: {elapsed}
"""
    print(report)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    (WORK_DIR / f"diff-{EPOCH}.txt").write_text(fs.get("diff_full", "") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
