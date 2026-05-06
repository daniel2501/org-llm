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

import os as _os
import re as _re
import time as _time
from pathlib import Path as _Path
from typing import NamedTuple


# ── Tiers ────────────────────────────────────────────────────────────────────
# "shared"   = ships in this module's _RECIPES list. High bar — must clear
#              the FOSS robustness check (pass A/B on sparse / missing-config
#              / errors-injected vaults).
# "personal" = user-defined in ~/org/org-llm-recipes.org. Whatever the user
#              wants. Loaded at proxy time, mtime-cached. Wins over shared
#              recipes when both match (user's intent overrides default).

class RecipeMatch(NamedTuple):
    name:    str          # short id for crew_log
    body:    str          # the recipe text injected into the prompt
    target:  str          # which agent the recipe is built for
    tier:    str = "shared"   # "shared" | "personal"


# Each entry: (compiled regex over the user text, RecipeMatch). First
# match wins. Keep this short and earned — see module docstring for the
# bar new entries must clear.
_RECIPES: list[tuple[_re.Pattern, RecipeMatch]] = [
    (
        # Weather-aware agenda — fires when the user asks about
        # weather impact on plans. The 2026-05-03 A/B found this
        # recipe lost 5/5 trials, BUT the failure mode was data-
        # starvation (weather not configured, few agenda items in
        # the user's vault), not a design flaw. Kept as latent
        # infrastructure because the user expects vault volume to
        # grow and weather-sensitive items will become routine.
        # The runner is hardened to ALWAYS return a result (with
        # explicit "weather: unavailable" markers when needed) so
        # the A7 hallucination shape — bare-body advisory mode
        # claiming "manager pre-fetched data" with no data — can't
        # recur. See docs/wiki/recipes.org for the full rationale.
        _re.compile(
            r"\b(weather|forecast|rain|snow|storm|outdoor|hike|bbq|"
            r"barbecue|garden|mow|cookout|picnic)\b.*"
            r"\b(agenda|schedule|plan|week|today|tomorrow|saturday|"
            r"sunday|monday|tuesday|wednesday|thursday|friday)\b|"
            r"\b(should i (?:reschedule|move)|will it rain|impact "
            r"my plans|weather problems)\b",
            _re.IGNORECASE | _re.DOTALL,
        ),
        RecipeMatch(
            name="weather_aware_agenda",
            target="*",
            tier="shared",
            body=(
                "RECIPE — weather-aware agenda:\n"
                "  1. The MANAGER PRE-FETCH block below contains "
                "whatever data is available — agenda items always; "
                "forecast + outdoor flags only when weather is "
                "configured.\n"
                "  2. Narrate ONLY from the pre-fetch. If "
                "`weather: unavailable` appears, say so plainly and "
                "answer from agenda alone — DO NOT invent forecasts "
                "or weather details.\n"
                "  3. Lead with the most weather-sensitive item if "
                "any are flagged; otherwise lead with the headline "
                "agenda summary.\n"
                "  4. Recommend ADJUSTMENTS, not just observations. "
                "If a morning is wet but afternoon clears, suggest "
                "the time shift.\n"
                "  5. Don't re-invoke the tool — the data above is "
                "authoritative."
            ),
        ),
    ),
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
            tier="shared",
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


# ── Personal-tier recipes ────────────────────────────────────────────────────
# Loaded from ~/org/org-llm-recipes.org if present. File shape:
#
#   * <recipe-id>                                                 :recipe:
#   :PROPERTIES:
#   :NAME:    <short id used in manager-log>
#   :TARGET:  <agent name | "*">
#   :PATTERN: <regex; case-insensitive by default>
#   :END:
#
#   <body — injected into the agent's prompt as advisory text;
#    supports the same shape as the bodies in _RECIPES above.>
#
# The body is everything between the properties drawer's :END: and the
# next heading or EOF. Patterns are compiled with re.IGNORECASE | re.DOTALL.
# Personal recipes win over shared recipes when both match.

_PERSONAL_RECIPES_PATH = _Path(
    _os.environ.get("ORG_LLM_PERSONAL_RECIPES")
    or "~/org/org-llm-recipes.org"
).expanduser()

# Cache: (mtime, [(compiled_pattern, RecipeMatch), ...])
_personal_cache: tuple[float, list[tuple[_re.Pattern, RecipeMatch]]] = (0.0, [])


def _parse_personal_recipes(text: str
                             ) -> list[tuple[_re.Pattern, RecipeMatch]]:
    """Parse the literate `~/org/org-llm-recipes.org` file shape into
    compiled (pattern, RecipeMatch) pairs.

    Tolerates missing properties (skips invalid entries silently —
    a malformed personal recipe must never crash the proxy)."""
    out: list[tuple[_re.Pattern, RecipeMatch]] = []
    # Split on top-level :recipe:-tagged headings. Tolerate level-1 or
    # level-2 nesting (= or ==).
    blocks = _re.split(
        r"(?m)^\*+\s+([^\n]*?):recipe:\s*$",
        text,
    )
    # blocks alternates: [preamble, heading1, body1, heading2, body2, ...]
    for i in range(1, len(blocks), 2):
        body_block = blocks[i + 1] if i + 1 < len(blocks) else ""
        # Pull the properties drawer.
        prop_m = _re.search(
            r":PROPERTIES:\s*\n(.*?)\n\s*:END:\s*\n?",
            body_block,
            _re.DOTALL,
        )
        if not prop_m:
            continue
        props = {}
        for line in prop_m.group(1).splitlines():
            kv = _re.match(r"\s*:([A-Z_]+):\s*(.*)$", line)
            if kv:
                props[kv.group(1)] = kv.group(2).strip()
        name    = props.get("NAME", "").strip()
        target  = props.get("TARGET", "*").strip() or "*"
        pattern = props.get("PATTERN", "").strip()
        if not name or not pattern:
            continue
        # Body is everything AFTER the :END: line, up to the next
        # heading start (already stripped by the outer split).
        body_text = body_block[prop_m.end():].strip()
        try:
            compiled = _re.compile(pattern, _re.IGNORECASE | _re.DOTALL)
        except _re.error:
            continue
        out.append((
            compiled,
            RecipeMatch(name=name, body=body_text, target=target,
                        tier="personal"),
        ))
    return out


def _load_personal_recipes() -> list[tuple[_re.Pattern, RecipeMatch]]:
    """Return the cached list of personal (user-defined) recipes,
    reloading from disk on mtime change.

    Returns [] when the file is absent or unparseable."""
    global _personal_cache
    try:
        mtime = _PERSONAL_RECIPES_PATH.stat().st_mtime
    except (FileNotFoundError, OSError):
        _personal_cache = (0.0, [])
        return []
    cached_mtime, cached_list = _personal_cache
    if mtime == cached_mtime:
        return cached_list
    try:
        text = _PERSONAL_RECIPES_PATH.read_text(encoding="utf-8")
    except OSError:
        _personal_cache = (mtime, [])
        return []
    parsed = _parse_personal_recipes(text)
    _personal_cache = (mtime, parsed)
    return parsed


def match_recipe(user_text: str) -> RecipeMatch | None:
    """Return the first matching RecipeMatch for `user_text`, or
    None if no recipe pattern fires. Cheap (~10µs) — runs at proxy
    time per request.

    *Tier order:* personal recipes (from =~/org/org-llm-recipes.org=)
    are checked first; shared recipes (from `_RECIPES`) second.
    First match wins. The user's literate config beats the default."""
    if not user_text:
        return None
    for pat, recipe in _load_personal_recipes():
        if pat.search(user_text):
            return recipe
    for pat, recipe in _RECIPES:
        if pat.search(user_text):
            return recipe
    return None
