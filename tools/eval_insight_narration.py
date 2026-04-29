#!/usr/bin/env python3
"""Narration quality eval harness — Phase 12.

Hand-curated test cases where we know the "good" answer.
For each case: feed the deterministic InsightCard to narrate_via_llm,
score the result, report per-case PASS/FAIL.

Scoring is heuristic (no LLM-as-judge yet — that adds eval cost
and stochasticity). Per-case dimensions:

  truth_recall    — fraction of expected keywords present in body
  banned_clean    — no must-avoid phrases leaked
  validation_pass — _validation_safe() returned True (no hallucination)
  scorer_chose    — which path the 12.2 scorer picked

Run:
    uv run python tools/eval_insight_narration.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from org_llm.insights import (
    InsightCard, narrate_via_llm,
    _validation_safe, _pick_better_body, _term_count, _evidence_terms,
)


# ── test cases ───────────────────────────────────────────────────────────
#
# Each case has:
#   id            — short human-readable name
#   card          — the deterministic InsightCard (input to narration)
#   truth_kw      — keywords/phrases a GOOD narration should contain
#                   (one is enough — heuristic recall scoring)
#   must_avoid    — phrases a BAD narration would leak (markdown,
#                   metalanguage, generic flourish)
#   expect_winner — "narrated" or "deterministic" — which path the
#                   12.2 scorer SHOULD pick. Some cases (e.g. when
#                   the deterministic body already cites the
#                   important specifics) we expect deterministic
#                   to win even with good narration.

TEST_CASES = [
    {
        "id": "thematic_bugs",
        "card": InsightCard(
            kind="topic_cluster",
            title="Emerging topic :phase11: (12 recent / 12 total)",
            body=("Tag :phase11: has 12 captures since the window "
                   "started, out of 12 all-time (100% recent). Likely "
                   "an active focus."),
            evidence={
                "tag":           "phase11",
                "recent_count":  12,
                "all_count":     12,
                "share":         1.0,
                "first_titles":  [
                    "nomic prefix retrieval bug fixed",
                    "auto_embedder env-var isolation breach",
                    "tag --apply was a no-op stub",
                    "search results displayed unsorted",
                    "capture polish invented content",
                ],
            },
            suggested_command="/explore phase11",
            score=0.85,
        ),
        # A good narration weaves at least one specific title in.
        "truth_kw":      ["phase11", "bug", "12"],
        "must_avoid":    ["**", "evidence", "node_count", "first_titles"],
        # Det body already says count + tag + share; LLM has chance to add THEME
        "expect_winner": "either",
    },
    {
        "id": "orphan_inbox",
        "card": InsightCard(
            kind="orphan_growth",
            title="15 new headings in inbox.org are unlinked",
            body=("15 recent capture(s) in inbox.org contain no "
                   "[[id:...]] links to other notes. Stitching them "
                   "into the graph makes them retrievable from "
                   "related queries."),
            evidence={
                "file":      "inbox.org",
                "new_count": 15,
                "path":      "/home/user/org/inbox.org",
            },
            suggested_command="/stitch inbox.org",
            score=0.7,
        ),
        # Good narration mentions inbox.org + the count + something
        # about linking
        "truth_kw":      ["inbox.org", "15"],
        "must_avoid":    ["**", "evidence", "card", "observation"],
        "expect_winner": "either",
    },
    {
        "id": "stale_with_explanation",
        "card": InsightCard(
            kind="stale_candidates",
            title="3 stale-tagged note(s) pending review",
            body=("3 note(s) carry :stale: or :drift: tags. Sample: "
                   "Pomodoro at 50/10 was the lever, Old project plan "
                   "Q1 2024, Dependency on package X."),
            evidence={
                "count":          3,
                "first_titles":   [
                    "Pomodoro at 50/10 was the lever",
                    "Old project plan Q1 2024",
                    "Dependency on package X",
                ],
                "tag_categories": ["stale", "drift"],
            },
            suggested_command="/stale review",
            score=0.6,
        ),
        # Sparse-evidence case — we EXPECT narration to add value
        # by paraphrasing or grouping the titles.
        "truth_kw":      ["stale", "Pomodoro"],
        "must_avoid":    ["**", "evidence", "node_count"],
        "expect_winner": "either",
    },
    {
        "id": "doctor_warnings",
        "card": InsightCard(
            kind="doctor_warnings",
            title="4 unresolved doctor warning(s) since last run",
            body=("Last `org-llm doctor` produced 4 warning marker(s). "
                   "Re-run doctor for the current state, or fold the "
                   "fixes via `doctor --fix`."),
            evidence={
                "warning_count":    4,
                "last_run":         "2026-04-29T03:00:00",
                "response_preview": ("⚠ Stale DB records: 1 file "
                                       "missing on disk. ⚠ Embeddings "
                                       "partial: 92% complete. ⚠ "
                                       "Unindexed files: 8 .org. ⚠ "
                                       "ANTHROPIC_API_KEY not set."),
            },
            suggested_command="/doctor --fix",
            score=0.65,
        ),
        "truth_kw":      ["4", "doctor"],
        "must_avoid":    ["**", "evidence"],
        "expect_winner": "either",
    },
    {
        "id": "diminishing_topic_TRAP",
        "card": InsightCard(
            kind="topic_cluster",
            title="Recent activity on :books: (3 recent / 80 total)",
            body=("Tag :books: has 3 captures since the window "
                   "started, out of 80 all-time (4% recent). "
                   "Possibly cooling."),
            evidence={
                "tag":          "books",
                "recent_count": 3,
                "all_count":    80,
                "share":        0.0375,
            },
            suggested_command="/explore books",
            score=0.4,
        ),
        # TRAP case: a small model might call this "emerging" because
        # there ARE 3 recent. Good narration recognizes 3/80 = COOLING.
        "truth_kw":      ["books"],
        "must_avoid":    ["emerging", "active focus", "**"],
        "expect_winner": "either",
    },
]


def evaluate_one(case: dict, narrated_card: InsightCard) -> dict:
    body_low = narrated_card.body.lower()
    truth_hits = sum(1 for kw in case["truth_kw"]
                       if kw.lower() in body_low)
    truth_recall = truth_hits / len(case["truth_kw"])
    banned_hits = [v for v in case["must_avoid"] if v.lower() in body_low]
    valid = _validation_safe(narrated_card.body, case["card"].evidence)
    chose = ("deterministic" if narrated_card.narration_model == "deterministic"
              else "narrated")
    expect = case["expect_winner"]
    expect_ok = (expect == "either" or expect == chose)

    pass_ = (truth_recall >= 0.5
                and not banned_hits
                and valid
                and expect_ok)

    return {
        "id":             case["id"],
        "pass":           pass_,
        "truth_recall":   truth_recall,
        "banned_hits":    banned_hits,
        "validation_pass": valid,
        "scorer_chose":   chose,
        "expected":       expect,
        "expect_ok":      expect_ok,
        "body":           narrated_card.body,
        "model":          narrated_card.narration_model,
    }


def main():
    base_url = os.environ.get("ORG_LLM_OLLAMA_URL",
                                "http://localhost:11434")
    # Use the user's configured fast_model; fallback to llama3.2:1b.
    model = os.environ.get("ORG_LLM_NARRATION_MODEL", "llama3.2:1b")
    print(f"Eval — narration model: {model}")
    print(f"      Ollama endpoint: {base_url}")
    print(f"      Cases: {len(TEST_CASES)}")
    print()

    results = []
    t_start = time.time()
    for case in TEST_CASES:
        narrated = narrate_via_llm(
            [case["card"]],
            model=model, base_url=base_url, voice="plain",
        )
        result = evaluate_one(case, narrated[0])
        results.append(result)

        ok = "PASS" if result["pass"] else "FAIL"
        chose = result["scorer_chose"]
        recall = result["truth_recall"]
        banned = (", banned=" + ",".join(result["banned_hits"])
                    if result["banned_hits"] else "")
        valid = "" if result["validation_pass"] else " INVALID"
        print(f"  [{ok}] {result['id']:30s}  "
                f"recall={recall:.2f}  scorer→{chose}{banned}{valid}")
        print(f"         body: {result['body'][:120]}")
        print()

    elapsed = time.time() - t_start
    n_pass = sum(1 for r in results if r["pass"])
    n_total = len(results)
    print(f"  ───  {n_pass}/{n_total} passed  ({elapsed:.1f}s total)  ───")
    out = ROOT / "tools" / ".eval_narration_last_run.json"
    out.write_text(json.dumps(results, default=str, indent=2))
    print(f"  Detailed results: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
