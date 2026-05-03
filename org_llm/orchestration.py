"""Recipe-based proxy-time pre-fetch.

Phase 22 v2 shipped six recipes; the 2026-05-03 A/B (90 judged pairs,
qwen-2.5-72b cloud, blinded Claude Opus 4.7 judge) found that ONLY
=count_across_vault= consistently beat the agent-tool-use baseline on
quality. The other five (=recent_activity_summary=, =dailies_general=,
=dailies_routine_filter=, =weekly_mood_review=, =weather_aware_agenda=)
either lost or tied to recipes-off; the latency/token wins they showed
were partly inflated by the recipes-on arm short-circuiting on
incomplete data. See docs/wiki/recipes.org for the full breakdown.

The catalog is now intentionally small: when the user asks an
event-counting question (=how many times have I…=, =how often did I…=,
=count of …=, etc.), the manager pre-fetches the count itself in
~10ms of pure-Python regex work and the agent narrates over the
result in ONE cloud call. For everything else, the agent uses MCP
tools normally — recipes have been verified to add no value there.

Activated by `proxy_orchestration_mode = "recipe"` (default).

Adding a recipe is a 4-line entry in `_RECIPES` + (optionally) a
runner in `recipe_runner.py`. Before adding one, run the A/B harness
(scripts/recipe_ab_harness.py) on it — the bar is ≥55% judge wins on
the recipe's own prompt shape AND no quality regression on neighbours.
"""

from __future__ import annotations

import re as _re
from typing import NamedTuple


class RecipeMatch(NamedTuple):
    name:    str          # short id for crew_log
    body:    str          # the recipe text injected into the prompt
    target:  str          # which agent the recipe is built for


# Each entry: (compiled regex over the user text, RecipeMatch). First
# match wins. Keep this short and earned — see module docstring for the
# bar new entries must clear.
_RECIPES: list[tuple[_re.Pattern, RecipeMatch]] = [
    (
        # Counting questions across the vault — "how many times have
        # I X", "count X in dailies", etc. Routes to researcher with
        # a 3-step plan that uses the deterministic
        # org_count_matches helper.
        #
        # NB: `frequency` was originally part of this trigger but
        # over-fired on "top 10 tags by frequency" / "exercise
        # frequency" — the tokeniser then built a nonsense regex
        # and the runner returned 0 hits. `count_across_vault` is
        # for counting EVENTS (DONE / [X] checkbox occurrences),
        # not aggregations. If the user says "frequency" without
        # "times" / "count" / "often", route through the normal
        # MCP path. (finding-2, 2026-05-03)
        _re.compile(
            r"\b(how many times|how often|count(?:ed)?|tally|total)\b",
            _re.IGNORECASE,
        ),
        RecipeMatch(
            name="count_across_vault",
            target="researcher",
            body=(
                "RECIPE — count across vault:\n"
                "  1. Translate the user's phrase to a regex. "
                "Examples:\n"
                "     'made my bed'    → r'\\[X\\].*make.{0,4}bed'\n"
                "     'exercised'      → r'\\[X\\].*(exercis|workout|gym)'\n"
                "     'mentioned cory' → r'(?i)cory'\n"
                "  2. Call `org-llm_org_count_matches(pattern=<regex>, "
                "path_glob='daily/**/*.org')` for dailies-only "
                "questions, or default `**/*.org` for whole vault.\n"
                "  3. Answer with `total_hits` and a 1-line breakdown "
                "from `top_files`. Don't fabricate. If 0, say so.\n"
                "Run STEP 1 → STEP 2 in this turn."
            ),
        ),
    ),
]


def match_recipe(user_text: str) -> RecipeMatch | None:
    """Return the first matching RecipeMatch for `user_text`, or
    None if no recipe pattern fires. Cheap (~10µs) — runs at proxy
    time per request."""
    if not user_text:
        return None
    for pat, recipe in _RECIPES:
        if pat.search(user_text):
            return recipe
    return None
