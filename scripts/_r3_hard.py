#!/usr/bin/env python3
"""PILOT R3-Hard driver — sibling pair (@data + @spock) on a multi-file
refactor where reviewer judgment can actually redirect the author.

R2-B's verdict: on a 61-LoC type-hint task the team layer didn't earn
its keep — the reviewer's clarifier was sensible but its answer was
already in the brief. R3-Hard gives the team a HARDER task with real
degrees of freedom (option choice, naming, scope) so we can measure
whether the reviewer's `mode:"btw"` redirects actually move the
author's work.

Round-2 lessons baked in:
- F1 : `idle` is terminal for spawned children.
- F4 : agor_execute_tool requires `tool_name` (snake_case).
- F8 : JWT 15-min TTL — refresh before each claude -p invocation.
- F9 amendment: ONE spawn per `claude -p` invocation. Captain runs
       2 short claude -p subprocesses, one per sibling, with
       mcp_token refresh between.
- F10: Python+subprocess only.
- BUG-8: PATCH bypassPermissions on captain pilot + each sibling.
- R2: btw children land on TARGET's genealogy.children, not caller's.
- R3: genealogy.children is list[str] — GET each to read fork_origin.
- R5: no parallel tool_use under MCP — captains serialize anyway.

Run: python3 scripts/_r3_hard.py
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
WT_NAME = f"pilot-R3Hard-{EPOCH}"
MODEL = "sonnet"
WORK_DIR = Path("/home/daniel/repos/org-llm/scripts/_r3_hard_artifacts")
WORK_DIR.mkdir(exist_ok=True)
TODAY = "2026-05-06"

# Budgets — hard cap $5.00, kill at $4.50 (per brief).
HARD_BUDGET_USD = 4.50
DATA_BUDGET_USD = "1.50"     # spawn @data ($1.50 cap on the captain spawning data)
SPOCK_BUDGET_USD = "1.50"    # spawn @spock
SPAWN_TIMEOUT_S = 360
SPAWN_MAX_RETRIES = 1
SIBLING_POLL_DEADLINE_S = 1500   # 25 min total siblings runtime
INTER_SPAWN_SLEEP_S = 6

# Wall budget: 35 min hard.
WALL_BUDGET_S = 35 * 60


def _resolve_admin_password() -> str:
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


# ─── logging ────────────────────────────────────────────────────────────────

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[r3-hard {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ─── auth + REST (R2-B style with auto-refresh) ─────────────────────────────


def _read_token_file() -> str:
    return json.load(open(TOKEN_FILE))["accessToken"]


def _login_admin() -> str:
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "AGOR_ADMIN_PASSWORD env not set + pass entry empty; "
            "cannot refresh JWT"
        )
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


# ─── personas (verbatim from agents/_builtins.py) ──────────────────────────

DATA_PERSONA = (
    "You are scribe — turn ideas into clean org notes. (For this pilot you "
    "are operating in CODER mode: the team needs a Python author for a "
    "small refactor with real degrees of freedom.) Birth-name @data. You "
    "are precise, deferential to evidence, and uncomfortable with sloppy "
    "claims. Strong defaults: always read the file before editing. Make "
    "the smallest change that satisfies the task. Preserve existing style "
    "and indentation. If another agent (your reviewer @spock on this same "
    "boardId) asks a clarifier mid-task, answer briefly and adjust if "
    "their question reveals a real problem with your approach.\n"
)

SPOCK_PERSONA = (
    "You are an org-llm researcher. Birth-name @spock. Logical, exacting, "
    "anti-confabulation. Always lead with a tool call when the question is "
    "about file content. (For this pilot you are operating in REVIEWER "
    "mode: review @data's option-choice + diff for correctness, scope, "
    "and project-fit. You SHOULD ask redirecting questions via "
    "`mode:\"btw\"` — questions that, if @data hadn't considered them, "
    "would change their approach. Your job is not to rubber-stamp; it's "
    "to make the work better.)\n"
)


# ─── prompts ────────────────────────────────────────────────────────────────


def _data_task_prompt(spock_session_id: str | None = None) -> str:
    """@data is the author. Picks ONE of 3 refactor options on
    tests/test_perf.py, justifies, implements, responds to @spock's btw
    questions, commits when both satisfied."""
    spock_hint = (
        f"Your reviewer @spock has session_id {spock_session_id}. "
        f"Their btw clarifiers will arrive as ephemeral child sessions on "
        f"YOUR genealogy."
        if spock_session_id else
        "Your reviewer @spock will spawn after you. Their btw clarifiers "
        "will arrive as ephemeral child sessions on YOUR genealogy."
    )
    return f"""You are @data, code author for a multi-agent refactor pilot.

PERSONA:
{DATA_PERSONA}

TEAM CONTEXT:
- You and @spock (reviewer) are sibling sessions on the SAME Agor
  worktree (boardId). The board IS the registry.
- {spock_hint}
- @spock will ask you redirecting clarifiers via `mode:"btw"`. Treat
  these as REAL questions: if @spock's question reveals a problem with
  your approach (wrong option, wrong naming, wrong scope), ADJUST.
- You may need to wait for @spock to register before they can review;
  poll once via the bash snippet below, but do not block forever.

TARGET FILE: tests/test_perf.py (in your worktree)

TASK:
The file tests/test_perf.py was just committed. It tests pure helpers
in org_llm.perf (`_normalize_model`, `_row_tok_s`). Your job is to
PICK ONE of these three improvement options, justify it, and
implement it across all affected files:

  (a) MODULE RENAME: rename tests/test_perf.py → tests/test_perf_helpers.py
      (more accurately reflects scope: only tests pure helpers, not the
      full perf module). Update any pytest discovery config or marker
      references that name the file directly.

  (b) TEST CLASS → FUNCTION: the test classes inside are
      TestNormalizeModel and TestRowTokS. Survey 3 other test files in
      tests/ to determine the project's test convention. If functions
      are the convention, refactor classes → flat module-level
      functions. If classes are equally common, justify whichever
      choice you make.

  (c) PROPERTY-BASED COVERAGE: add 1-2 hypothesis-based property tests
      for `_normalize_model` (e.g. "any input → output is lowercase" or
      "running normalize twice = once"). Add `hypothesis` to dev deps
      in pyproject.toml if needed. Existing tests stay.

WORKFLOW (this is the experimental loop — follow it):

  1. READ tests/test_perf.py and survey ~3 sibling test files
     (tests/test_*.py). Pick ONE option (a/b/c) and justify in 2-3
     lines why it's the highest-value improvement for THIS file in
     THIS project.

  2. WRITE your option choice + justification + plan to
     `.agor-assistants/data/memory/{TODAY}.md` BEFORE editing code
     (heading: `# @data — option choice + plan {TODAY}`). This signals
     to @spock that you have a draft ready for review.

  3. WAIT briefly (sleep ~30s) for @spock to react. @spock may send
     you a btw clarifier — it arrives as a normal user-style message
     in your inbox. Read it; if it raises a real concern, REVISE your
     plan (write a `## Revision` section to your memory file).

  4. IMPLEMENT your (possibly revised) option. Touch only the files
     your option requires. Run the venv pytest to verify:
       /home/daniel/repos/org-llm/.venv/bin/pytest tests/test_perf*.py -q
     OR (if you renamed the file) the new path. Tests must pass.

  5. COMMIT on this worktree's branch:
       git add -A && git commit -m "refactor(test_perf): <option> — <one-line summary>"
     The commit message should mention the option and reflect any
     redirection from @spock.

  6. APPEND to your memory file: option finally chosen, files touched,
     pytest result (pass count + summary), commit SHA, and (if @spock
     redirected you) what their btw question changed.

  7. Print exactly the line `DATA_DONE` on its own and stop.

CONSTRAINTS:
- DO NOT run any agor MCP tools. (The captain handles all Agor work.)
- DO NOT modify files outside tests/test_perf*.py (and pyproject.toml
  IF option c needs hypothesis added).
- DO NOT add new external runtime dependencies; hypothesis-as-dev-dep
  is the only allowance, and only for option (c).
- Use bash for filesystem + git + pytest. You are normal Claude Code.

If after ~3 minutes no btw arrives from @spock, proceed without it
and note "no clarifier received" in your memory file — DON'T deadlock.

POLLING SNIPPET (for sibling discovery, optional):
    python3 -c "import urllib.request, json, os; \\
      tok=json.load(open(os.path.expanduser('~/.agor/cli-token')))['accessToken']; \\
      req=urllib.request.Request('http://localhost:3030/sessions', \\
          headers={{'Authorization':'Bearer '+tok}}); \\
      data=json.loads(urllib.request.urlopen(req,timeout=15).read()); \\
      sessions=data if isinstance(data,list) else data.get('data',data.get('sessions',[])); \\
      [print(s.get('id'), s.get('title','')) for s in sessions[-10:]]"
"""


def _spock_task_prompt(data_session_id: str) -> str:
    """@spock is the reviewer. Asks redirecting btw questions targeting
    @data's option choice / naming / scope."""
    return f"""You are @spock, code reviewer for a multi-agent refactor pilot.

PERSONA:
{SPOCK_PERSONA}

TEAM CONTEXT:
- Your sibling @data has session_id {data_session_id} on this same
  Agor worktree.
- @data is picking ONE of 3 refactor options on tests/test_perf.py:
    (a) module rename → test_perf_helpers.py
    (b) test class → flat function refactor
    (c) add hypothesis property-based coverage
- @data will write their option choice + plan to
  `.agor-assistants/data/memory/{TODAY}.md` BEFORE coding. THAT is
  your signal to review.
- YOUR JOB IS TO REDIRECT IF NEEDED. Don't rubber-stamp. Ask the
  ONE clarifier question that — if @data hadn't considered it —
  would meaningfully change their approach. This pilot specifically
  measures whether reviewer questions on a non-trivial task can
  redirect the author. So make a question that COULD redirect.

TASK:

  1. POLL for `.agor-assistants/data/memory/{TODAY}.md` in your
     worktree (sleep 20s loop, max 6 minutes). Once present, READ
     @data's option choice + justification.

  2. SURVEY for yourself: read tests/test_perf.py + 3 sibling
     test files in tests/ to form your own opinion of which option
     is highest value. (~2 minutes.)

  3. ASK @data ONE redirecting clarifier via mode:"btw". The
     question should target a real ambiguity in their plan —
     examples (use whichever fits, or invent your own):
       - "Did you check that hypothesis is even installable in this
         repo's dev env before promising option c?"
         (option c)
       - "You picked classes-stay; my survey shows tests/test_*.py
         is split ~50/50 between classes and functions. What's your
         tiebreaker?"
         (option b)
       - "Renaming the file changes pytest discovery for anyone
         running by full path; did you grep for hardcoded
         'test_perf.py' references in scripts/CI?"
         (option a)
     Use exactly:
       mcp__agor__agor_execute_tool(
         tool_name = "agor_sessions_prompt",
         arguments = {{
           "mode": "btw",
           "target": "{data_session_id}",
           "prompt": "<your one redirecting question>"
         }}
       )
     Print the returned child session id as `BTW_CHILD_ID=<uuid>`
     on its own line.

  4. WAIT for @data's revised plan (look for a "## Revision"
     section in their memory file, or for the actual diff to land).
     Sleep-poll up to 6 minutes.

  5. REVIEW the final diff:
       git diff HEAD -- tests/  pyproject.toml
     Decide APPROVE / REVISIONS_NEEDED / BLOCK. Use APPROVE only if:
       - the chosen option is implemented correctly
       - tests pass (you can run
         `/home/daniel/repos/org-llm/.venv/bin/pytest tests/test_perf*.py -q`)
       - scope is contained (no surprise files touched)
       - a commit landed on the branch (run `git log -1 --oneline`)

  6. WRITE your verdict to
     `.agor-assistants/spock/memory/{TODAY}.md` (heading:
     `# @spock — review verdict {TODAY}`). Include:
       - the diff stat summary
       - APPROVE / REVISIONS_NEEDED / BLOCK + 2-3 line rationale
       - the EXACT btw question you asked
       - whether @data's response actually changed their approach
         (yes/no/partial — quote what changed)
       - did mode:"btw" land end-to-end?
       - the BTW_CHILD_ID

  7. Print exactly the line `SPOCK_APPROVED` (only if APPROVE) or
     `SPOCK_BLOCKED` on its own and stop.

CONSTRAINTS:
- DO NOT modify any source/test/config file (review only).
- DO NOT modify anything outside `.agor-assistants/spock/memory/`.
- DO use mcp__agor__agor_execute_tool for the btw call.
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


def refresh_mcp_token(sid: str) -> str | None:
    try:
        got = api("GET", f"/sessions/{sid}")
        return got.get("mcp_token")
    except Exception as e:
        log(f"  refresh_mcp_token({sid}) failed: {e}")
        return None


def run_claude(prompt: str, mcp_config: str, out_path: str,
               budget: str = "1.50", timeout: int = SPAWN_TIMEOUT_S) -> int:
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


def parse_stream(text: str) -> dict:
    rate_limits = 0
    cost = 0.0
    has_result = False
    tool_uses: list[tuple[str, dict]] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "rate_limit_event":
            rate_limits += 1
        if t == "result":
            has_result = True
            cost += float(obj.get("total_cost_usd") or 0)
        if t == "assistant":
            for c in obj.get("message", {}).get("content", []) or []:
                if c.get("type") == "tool_use":
                    tool_uses.append((c.get("name", ""), c.get("input") or {}))
    return {"rate_limits": rate_limits, "cost": cost,
            "has_result": has_result, "tool_uses": tool_uses}


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
    log("  PATCH bypassPermissions on captain pilot (BUG-8)")
    api("PATCH", f"/sessions/{sid}",
        {"permission_config": {"mode": "bypassPermissions"}})
    return sid, tok


def spawn_one_sibling(handle: str, sib_prompt: str, mcp_config: str,
                      attempt_label: str, budget: str) -> dict:
    """One captain claude -p call → one agor_sessions_spawn for `handle`.
    Per F9 amendment: one-spawn-per-claude-p prevents stalls under any
    rate-limit pressure."""
    spawn_args = json.dumps({
        "prompt": sib_prompt,
        "title": f"r3hard-{handle}",
    })
    captain_prompt = f"""You have one MCP server "agor" with tools
mcp__agor__agor_search_tools and mcp__agor__agor_execute_tool.

Spawn ONE child session by issuing exactly ONE
mcp__agor__agor_execute_tool call with these arguments:

  tool_name: "agor_sessions_spawn"
  arguments: {spawn_args}

Note: the field name inside agor_execute_tool is `tool_name` (snake_case),
NOT `toolName`. Do NOT pass a `worktree_id` — the child must inherit your
worktree (sibling-by-boardId pattern).

Once the spawn response comes back, print exactly this single line and
then STOP (do not poll, do not call any other tool, do not narrate):

{handle.upper()}_SESSION_ID=<session_id from the spawn response>
"""
    out_path = str(WORK_DIR / f"spawn-{handle}-{EPOCH}-{attempt_label}.jsonl")
    rc = run_claude(captain_prompt, mcp_config, out_path,
                    budget=budget, timeout=SPAWN_TIMEOUT_S)
    text = Path(out_path).read_text() if Path(out_path).exists() else ""
    info = parse_stream(text)
    m = re.search(rf"{handle.upper()}_SESSION_ID=([A-Za-z0-9_-]+)", text)
    sid = m.group(1) if m else None
    log(f"  spawn[{handle}] rc={rc} rate_limits={info['rate_limits']} "
        f"tool_uses={len(info['tool_uses'])} cost=${info['cost']:.4f} sid={sid}")
    return {"rc": rc, "sid": sid, "out_path": out_path, **info}


def spawn_with_retry(handle: str, sib_prompt: str, pilot_sid: str,
                     mcp_config_path: str, budget: str) -> dict:
    rl_total = 0
    cost_total = 0.0
    last = None
    for attempt in range(1, SPAWN_MAX_RETRIES + 2):
        new_tok = refresh_mcp_token(pilot_sid)
        if new_tok:
            write_mcp_config(new_tok, mcp_config_path)
        log(f"  spawn[{handle}] attempt {attempt}/{SPAWN_MAX_RETRIES + 1}")
        last = spawn_one_sibling(handle, sib_prompt, mcp_config_path,
                                 f"a{attempt}", budget)
        rl_total += last["rate_limits"]
        cost_total += last["cost"]
        if last["sid"]:
            last["rl_total"] = rl_total
            last["cost_total"] = cost_total
            return last
        if attempt > SPAWN_MAX_RETRIES:
            break
        log(f"  spawn[{handle}] no sid — sleeping 30s before retry "
            f"(rate_limits={last['rate_limits']})")
        time.sleep(30)
    last = last or {"sid": None, "out_path": "", "rate_limits": rl_total,
                    "cost": cost_total, "tool_uses": []}
    last["rl_total"] = rl_total
    last["cost_total"] = cost_total
    return last


def step_lift_permission(sid: str | None) -> None:
    if not sid:
        return
    try:
        api("PATCH", f"/sessions/{sid}",
            {"permission_config": {"mode": "bypassPermissions"}})
        log(f"  PATCHed bypassPermissions on {sid[:8]}...")
    except Exception as e:
        log(f"  PATCH {sid[:8]}... failed: {e}")


def step_poll_siblings(sids: list[str],
                       deadline_s: int = SIBLING_POLL_DEADLINE_S) -> dict:
    log(f"step 5 — polling 2 siblings to terminal (≤ {deadline_s}s)")
    deadline = time.time() + deadline_s
    terminal = {"idle", "completed", "stopped", "archived",
                "failed", "errored"}
    states = {sid: "?" for sid in sids if sid}
    if not states:
        return states
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
                     data_sid: str | None) -> tuple[list[str], list[dict]]:
    """Find btw children. R2 canon: btw lands on TARGET's
    genealogy.children. R3 canon: genealogy.children is list[str], must
    GET each. Returns (ids, full child dicts for content sampling)."""
    btw_ids: list[str] = []
    btw_children: list[dict] = []
    for label, sid in (("data", data_sid), ("spock", spock_sid)):
        if not sid:
            continue
        try:
            got = api("GET", f"/sessions/{sid}")
            gen = got.get("genealogy", {}) or {}
            kids = gen.get("children", []) or []
            log(f"  {label}.genealogy.children: {len(kids)} total")
            for k in kids:
                if isinstance(k, str):
                    cid = k
                    try:
                        child = api("GET", f"/sessions/{cid}")
                    except Exception:
                        continue
                else:
                    cid = k.get("id") or k.get("session_id") or ""
                    child = k
                origin = (child.get("fork_origin") or
                          child.get("forkOrigin") or "")
                if "btw" in str(origin).lower() and cid:
                    btw_ids.append(cid)
                    btw_children.append(child)
        except Exception as e:
            log(f"  inspect_btw({label}) failed: {e}")
    log(f"  btw children found: {len(btw_ids)}")
    return btw_ids, btw_children


def fetch_btw_messages(child_id: str) -> list[dict]:
    """R4 canon: messages live at /messages?session_id={id}, NOT
    /sessions/{id}/messages."""
    try:
        # Feathers list-with-filter shape — try $limit if supported.
        j = api("GET", f"/messages?session_id={child_id}&$limit=20")
        if isinstance(j, list):
            return j
        return j.get("data", []) or []
    except Exception as e:
        log(f"  fetch_btw_messages({child_id[:8]}) failed: {e}")
        return []


def step_verify(wt_path: str) -> dict:
    log("step 6 — verify worktree state")
    out: dict = {}
    # diff stat
    try:
        p = subprocess.run(
            ["git", "diff", "--stat", f"origin/{SOURCE_BRANCH}..HEAD"],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["branch_diff_stat"] = (p.stdout or "").strip()
    except Exception as e:
        out["branch_diff_stat"] = f"err: {e}"
    try:
        p = subprocess.run(
            ["git", "diff", f"origin/{SOURCE_BRANCH}..HEAD"],
            cwd=wt_path, capture_output=True, text=True, timeout=30)
        out["branch_diff_full"] = (p.stdout or "")[:6000]
    except Exception as e:
        out["branch_diff_full"] = f"err: {e}"
    # last commit on branch
    try:
        p = subprocess.run(
            ["git", "log", "-1", "--oneline", "--no-merges"],
            cwd=wt_path, capture_output=True, text=True, timeout=10)
        out["last_commit"] = (p.stdout or "").strip()
    except Exception as e:
        out["last_commit"] = f"err: {e}"
    # commit SHAs ahead of source branch
    try:
        p = subprocess.run(
            ["git", "log", f"origin/{SOURCE_BRANCH}..HEAD", "--oneline"],
            cwd=wt_path, capture_output=True, text=True, timeout=10)
        out["commits_ahead"] = (p.stdout or "").strip()
    except Exception as e:
        out["commits_ahead"] = f"err: {e}"
    # pytest run on test_perf*.py
    try:
        p = subprocess.run(
            ["/home/daniel/repos/org-llm/.venv/bin/pytest",
             "-q", "--tb=line"],
            cwd=wt_path, capture_output=True, text=True, timeout=180)
        out["pytest_rc"] = p.returncode
        out["pytest_summary"] = (
            (p.stdout or "").splitlines()[-3:] if p.stdout else []
        )
        out["pytest_tail"] = (p.stdout or "")[-1500:]
    except Exception as e:
        out["pytest_rc"] = -1
        out["pytest_summary"] = [f"err: {e}"]
        out["pytest_tail"] = ""
    # memory files
    data_mem = (Path(wt_path) / ".agor-assistants" / "data"
                / "memory" / f"{TODAY}.md")
    spock_mem = (Path(wt_path) / ".agor-assistants" / "spock"
                 / "memory" / f"{TODAY}.md")
    out["data_memory_exists"] = data_mem.exists()
    out["spock_memory_exists"] = spock_mem.exists()
    out["data_memory_text"] = (
        data_mem.read_text() if data_mem.exists() else "")
    out["spock_memory_text"] = (
        spock_mem.read_text() if spock_mem.exists() else "")

    log(f"  branch diff stat: {out['branch_diff_stat'][:200] or '<empty>'}")
    log(f"  last commit: {out['last_commit']}")
    log(f"  pytest rc={out['pytest_rc']} tail={out['pytest_summary']}")
    log(f"  data_memory={out['data_memory_exists']} "
        f"spock_memory={out['spock_memory_exists']}")
    return out


# ─── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    t0 = time.time()
    if not ADMIN_PASSWORD:
        log("FATAL: AGOR_ADMIN_PASSWORD env not set + pass entry empty")
        return 2

    wt = step_create_worktree()
    wt_id = wt["worktree_id"]
    wt_path = wt["path"]

    captain_sid, mcp_token = step_create_pilot_session(wt_id)
    mcp_config = str(WORK_DIR / f"mcp-{EPOCH}.json")
    write_mcp_config(mcp_token, mcp_config)

    # ── Spawn order: @data first (author needs to register before
    # @spock can target btw at it), then @spock (with data's session_id
    # injected into the prompt).
    log("step 3 — spawn @data (author) via dedicated claude -p")
    data_prompt = _data_task_prompt(spock_session_id=None)
    data_res = spawn_with_retry("data", data_prompt, captain_sid,
                                mcp_config, DATA_BUDGET_USD)
    data_sid = data_res.get("sid")
    if data_sid:
        step_lift_permission(data_sid)
    else:
        log("  WARN: @data spawn returned no sid — continuing anyway")

    elapsed = int(time.time() - t0)
    cost_so_far = data_res.get("cost_total", 0.0)
    log(f"  intermediate: cost=${cost_so_far:.4f} wall={elapsed}s")
    if cost_so_far > HARD_BUDGET_USD:
        log(f"  ABORT: cost ${cost_so_far:.4f} > hard cap ${HARD_BUDGET_USD}")
        return 3

    time.sleep(INTER_SPAWN_SLEEP_S)

    log("step 4 — spawn @spock (reviewer) via dedicated claude -p")
    spock_prompt = _spock_task_prompt(data_session_id=data_sid or "<unknown>")
    spock_res = spawn_with_retry("spock", spock_prompt, captain_sid,
                                 mcp_config, SPOCK_BUDGET_USD)
    spock_sid = spock_res.get("sid")
    if spock_sid:
        step_lift_permission(spock_sid)
    else:
        log("  WARN: @spock spawn returned no sid — continuing anyway")

    cost_so_far += spock_res.get("cost_total", 0.0)
    log(f"  intermediate after spock spawn: cost=${cost_so_far:.4f}")

    sids = [s for s in (data_sid, spock_sid) if s]

    # Poll deadline reduced by elapsed wall.
    elapsed = int(time.time() - t0)
    poll_budget = max(60, WALL_BUDGET_S - elapsed - 60)
    states = step_poll_siblings(sids,
                                deadline_s=min(SIBLING_POLL_DEADLINE_S, poll_budget))

    btw_ids, btw_children = step_inspect_btw(spock_sid, data_sid)

    # Sample btw exchange content (per brief: quote 1-2 actual btw
    # exchanges to judge if reviewer questions mattered).
    btw_samples: list[dict] = []
    for cid, child in zip(btw_ids[:2], btw_children[:2]):
        msgs = fetch_btw_messages(cid)
        # Extract first user prompt (the btw question) + first
        # assistant response (data's reply).
        question = ""
        answer = ""
        for m in msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            if not isinstance(content, str):
                content = str(content)
            if role == "user" and not question:
                question = content[:600]
            elif role == "assistant" and not answer:
                answer = content[:600]
        # Also try the child's stored prompt in fork_origin metadata.
        if not question:
            initial = (child.get("initial_prompt") or
                       child.get("prompt") or "")
            if isinstance(initial, str):
                question = initial[:600]
        btw_samples.append({
            "child_id": cid,
            "question": question or "<no question recovered>",
            "answer": answer or "<no answer recovered>",
        })

    fs = step_verify(wt_path)

    # Aggregate sibling REST cost (per F7 likely null but try).
    sibling_cost_rest = 0.0
    for sid in sids:
        try:
            got = api("GET", f"/sessions/{sid}")
            c = got.get("cost", {}) or {}
            sibling_cost_rest += float(c.get("total_usd")
                                       or got.get("total_cost_usd") or 0)
        except Exception:
            pass
    total_cost = cost_so_far + sibling_cost_rest

    elapsed = int(time.time() - t0)

    # ── verdict
    sib_states = {h: states.get(s, "?") for h, s in
                  [("data", data_sid), ("spock", spock_sid)]}
    commit_landed = bool(fs.get("commits_ahead") and
                         fs["commits_ahead"].strip())
    pytest_pass = (fs.get("pytest_rc") == 0)
    sessions_terminal = all(
        states.get(s) in {"idle", "completed", "stopped", "archived"}
        for s in sids
    ) if sids else False
    files_landed = (fs["data_memory_exists"] and fs["spock_memory_exists"])
    btw_landed = len(btw_ids) > 0

    pass_signals = sum([commit_landed, pytest_pass, btw_landed,
                        sessions_terminal, files_landed])
    if pass_signals >= 4 and commit_landed and pytest_pass:
        verdict = "PASS"
    elif pass_signals >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    # ── option chosen detection (heuristic from data memory)
    option_chosen = "unknown"
    dm = fs.get("data_memory_text", "").lower()
    if "option (a)" in dm or "option a" in dm or "rename" in dm:
        option_chosen = "a"
    if "option (b)" in dm or "option b" in dm or "class" in dm and "function" in dm:
        # break ties by which is mentioned first
        if dm.find("option a") >= 0 and (dm.find("option b") < 0 or dm.find("option a") < dm.find("option b")):
            option_chosen = "a"
        else:
            option_chosen = "b"
    if "option (c)" in dm or "hypothesis" in dm:
        # if hypothesis mentioned but a/b also mentioned, prefer if commit touches deps
        if "hypothesis" in fs.get("branch_diff_full", "").lower():
            option_chosen = "c"
    if option_chosen == "unknown" and fs.get("branch_diff_full"):
        diff = fs["branch_diff_full"].lower()
        if "test_perf_helpers" in diff or "rename" in diff:
            option_chosen = "a"
        elif "hypothesis" in diff or "@given" in diff:
            option_chosen = "c"
        elif "def test_" in diff and "class test" in diff:
            option_chosen = "b"

    # ── team-value heuristic
    team_value = "unknown"
    sm = fs.get("spock_memory_text", "").lower()
    if btw_landed:
        # Did @data revise after the btw?
        if ("revision" in dm or "revised" in dm or
                "after spock" in dm or "@spock" in dm and "changed" in dm):
            team_value = "yes — @data wrote a Revision section after btw"
        elif "no clarifier" in dm or "no btw" in dm:
            team_value = "no — @data noted no btw received"
        else:
            team_value = "partial — btw landed but @data memory shows no clear revision"
    else:
        team_value = "no — no btw children observed"

    primitives = [
        "sibling-by-boardId (shared worktree, omit worktree_id)",
        "one-spawn-per-claude-p (F9 amendment)",
        "agor_execute_tool (snake_case tool_name, F4)",
        "permission_config.bypassPermissions PATCH (BUG-8)",
        "mcp_token after-get refresh (F8 + R2-D2 pattern)",
    ]
    if btw_landed:
        primitives.append('mode:"btw" (verified end-to-end via target genealogy)')
    else:
        primitives.append('mode:"btw" (ATTEMPTED — no btw children observed)')

    btw_sample_txt = ""
    for i, s in enumerate(btw_samples, 1):
        btw_sample_txt += (
            f"  --- btw exchange {i} (child {s['child_id'][:12]}…)\n"
            f"  Q (@spock → @data): {s['question'][:400]}\n"
            f"  A (@data → @spock): {s['answer'][:400]}\n"
        )
    if not btw_sample_txt:
        btw_sample_txt = "  <no btw exchanges to sample>"

    report = f"""
PILOT: R3-Hard
TEAM_SHAPE: sibling pair on multi-file refactor (@data + @spock + btw clarifiers) — Python+subprocess, sequential one-spawn-per-claude-p
HARNESS_SCRIPT: scripts/_r3_hard.py
AGOR_PRIMITIVES_USED: {primitives}
TASK_OUTCOME: {verdict} — commit_landed={commit_landed} pytest_pass={pytest_pass} btw_landed={btw_landed} sessions_terminal={sessions_terminal} files_landed={files_landed}
OPTION_CHOSEN: {option_chosen} — heuristic from @data memory + diff
WORKTREE_NAME: {WT_NAME}
WORKTREE_ID: {wt_id}
WORKTREE_PATH: {wt_path}
CAPTAIN_SESSION_ID: {captain_sid}
DATA_SESSION_ID: {data_sid}
SPOCK_SESSION_ID: {spock_sid}
SIBLING_STATES: {sib_states}
BTW_CHILD_IDS: {btw_ids if btw_ids else 'none observed'}
BTW_CONTENT_SAMPLE:
{btw_sample_txt}
COMMIT_LANDED: {fs.get('commits_ahead') or 'none'}
LAST_COMMIT: {fs.get('last_commit') or 'none'}
DIFF_STAT: {fs.get('branch_diff_stat') or '<empty>'}
PYTEST_RESULT: rc={fs.get('pytest_rc')} {fs.get('pytest_summary')}
COST_USD: {total_cost:.4f} (captain stream-json sum ${cost_so_far:.4f} + siblings REST ${sibling_cost_rest:.4f})
DURATION_SECONDS: {elapsed}
TEAM_VALUE_ON_HARDER_TASK: {team_value}
"""
    print(report, flush=True)
    (WORK_DIR / f"report-{EPOCH}.txt").write_text(report)
    (WORK_DIR / f"log-{EPOCH}.txt").write_text("\n".join(LOG))
    (WORK_DIR / f"diff-{EPOCH}.txt").write_text(
        fs.get("branch_diff_full", "") or "")
    (WORK_DIR / f"pytest-tail-{EPOCH}.txt").write_text(
        fs.get("pytest_tail", "") or "")
    if btw_samples:
        (WORK_DIR / f"btw-samples-{EPOCH}.json").write_text(
            json.dumps(btw_samples, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
