# Contributing to org-llm

Welcome. This file is a routing doc — the substantive content
lives in the org-mode wiki at [`docs/wiki/`](docs/wiki/), which
is the project's source of truth for concepts, decisions, and
conventions.

> Why .md here? Per
> [`docs/wiki/decisions.org`](docs/wiki/decisions.org) DEC-002
> ("org-native first"), `.md` is reserved for cross-tool
> surfaces — the GitHub Contributing banner, AGENTS.md primers,
> PR templates. Everything durable lives in `.org`.

## Read first

Before touching the repo, skim these in order:

1. **[`docs/wiki/00-index.org`](docs/wiki/00-index.org)** — the
   concept map. Walk the graph; no need to read everything.
2. **[`docs/wiki/now.org`](docs/wiki/now.org)** — single-view
   dashboard of in-flight + recently shipped work. "You are here."
3. **[`docs/wiki/coordination.org`](docs/wiki/coordination.org)**
   — *the parallel-instance protocol.* Read this even if
   you're solo today; multiple agents and humans work on this
   repo concurrently.
4. **[`docs/wiki/decisions.org`](docs/wiki/decisions.org)** —
   the directional record. Read before proposing changes that
   contradict a DEC-NNN entry.
5. **[`AGENTS.md`](AGENTS.md)** — if you're an agent runtime
   (opencode, Claude Code, Cursor, Codex, etc.) loading this
   project, start here.
6. **[`docs/wiki/wiki-conventions.org`](docs/wiki/wiki-conventions.org)**
   — how the wiki updates itself; the always-notified rule;
   summary+expanded format.

## Coordinate before you touch

The repo has multiple humans and agent instances working in
parallel. Before opening an `Edit` or `Write`:

1. Append a `STARTED` heading under "Active claims" in
   [`docs/wiki/active-claims.org`](docs/wiki/active-claims.org)
   naming who you are, what files you'll touch, and your
   branch.
2. Work on your own branch (`<who>/<short-slug>`). The branch
   named `trunk` is the maintainer's primary stream; PRs
   target `main`.
3. Re-Read every file immediately before editing — parallel
   streams invalidate older snapshots.
4. Never edit the wiki silently. Announce in
   conversation/commit/PR.

The four rules are spelled out in
[`docs/wiki/coordination.org`](docs/wiki/coordination.org).
They are deliberately light — we are not Scrum.

## Dev setup

```sh
git clone git@github.com:daniel2501/org-llm.git
cd org-llm
uv sync                 # deps + dev tools
uv run pytest -q        # 660+ tests
```

Run the full suite plus typecheck before opening a PR. See
README.md `## Development` for layout details.

## Filing a PR

Use the template at
[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).
The checklist exists so reviewers (human or agent) can verify
the same things every time.

Keep PRs scoped to one logical arc. If a refactor and a
feature are tangled, split them — bundled PRs are accepted only
when splitting would be churn (judgment call; flag in the PR
description).

## Proposing a decision

If your change embeds a *directional* choice — a default future
contributors will follow, an alternative ruled out, a deferral
with a trigger — add a `DEC-NNN` heading to
[`docs/wiki/decisions.org`](docs/wiki/decisions.org) in the
same PR. The entry format and "when to write" triggers live in
that file.

If unsure, write the decision and let review push back. A
five-line entry is cheap; re-litigating a forgotten decision is
expensive.

## Agent contributors

Agent runtimes contributing to this repo:

- Read [`AGENTS.md`](AGENTS.md) on session start.
- Honor coordination.org Rule 1 (claim before touch). The
  always-notified rule applies across instances, not just
  within a conversation.
- Wiki edits use [`docs/wiki/wiki-conventions.org`](docs/wiki/wiki-conventions.org)
  Rule 2a (file-link mentions) and Rule 2b (summary+expanded
  on new concepts).

## Questions

Open an issue, or write a captain's-log entry inside org-llm
itself if you're already running it. The
[`docs/wiki/captains-log.org`](docs/wiki/captains-log.org)
concept page describes the event-stream surface.
