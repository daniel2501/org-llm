"""End-to-end specialist dispatch smoke for P1-18.

Calls _dispatch_tool_call(name='mcp_emacs_*', ...) for each new tool
and verifies the routing works through specialist.py → mcp_emacs.py
→ rhblind emacs-mcp-server daemon.

NOTE: This script forces the trunk org_llm onto sys.path[0] because a
user-site editable install (.pth) points an org-llm worktree, which
shadows the trunk in `python3 scripts/foo.py` invocations.
"""
import sys
sys.path.insert(0, '/home/daniel/repos/org-llm')
for _k in list(sys.modules):
    if _k.startswith('org_llm'):
        del sys.modules[_k]

import subprocess
from pathlib import Path


def main() -> int:
    # Make sure a stale daemon doesn't poison the test.
    subprocess.run(
        ['emacsclient', '-s', 'org-llm-mcp', '-e', '(kill-emacs)'],
        capture_output=True, timeout=5,
    )

    from org_llm.specialist import _dispatch_tool_call

    failures: list[str] = []

    ok, out = _dispatch_tool_call(
        'mcp_emacs_eval_elisp', {'code': '(* 6 7)'}, Path('/tmp'))
    print(f'mcp_emacs_eval_elisp: ok={ok} out={out!r}')
    if not (ok and '42' in out):
        failures.append('eval_elisp not 42')

    ok, out = _dispatch_tool_call('mcp_emacs_list_buffers', {}, Path('/tmp'))
    print(f'mcp_emacs_list_buffers: ok={ok} count={len(out.splitlines())}')
    if not ok:
        failures.append('list_buffers failed')

    ok, out = _dispatch_tool_call(
        'mcp_emacs_read_buffer', {'name': '*Messages*'}, Path('/tmp'))
    print(f'mcp_emacs_read_buffer(*Messages*): ok={ok} bytes={len(out)}')
    if not ok:
        failures.append('read_buffer failed')

    # Unknown name routing
    ok, out = _dispatch_tool_call(
        'mcp_emacs_bogus', {}, Path('/tmp'))
    print(f'mcp_emacs_bogus: ok={ok} out={out!r}')
    if ok or 'unknown' not in out:
        failures.append('unknown tool not rejected')

    from org_llm.mcp_emacs import reset_client, shutdown_daemon
    reset_client()
    shutdown_daemon()
    print('teardown complete')

    if failures:
        print('FAIL:', failures)
        return 1
    print('OK: all dispatch smokes passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
