"""Phase 22 orchestration v2 — recipe-based.

Manager-pattern alternative that adds ZERO cloud round-trips by
matching the user's message against known task-shapes at proxy
time and injecting a deterministic RECIPE into the agent's system
prompt. The LLM follows the recipe in a single turn.

This is the lightest of the three candidates from the Phase 22
roadmap (router / parallel-delegate / recipe). It buys
orchestration's quality benefits — predictable tool-call order,
inline filter rules, format guidance — without paying for an
extra LLM pass.

Activated by `proxy_orchestration_mode = "recipe"` in config.
Default `"off"`.

Adding a recipe is a 4-line entry in `_RECIPES`. No prompt-edit
or specialist-rewrite required.
"""

from __future__ import annotations

import re as _re
from typing import NamedTuple


class RecipeMatch(NamedTuple):
    name:    str          # short id for crew_log
    body:    str          # the recipe text injected into the prompt
    target:  str          # which agent the recipe is built for


# Each entry: (compiled regex over the user text, recipe target,
# recipe body). First match wins — order matters; put more specific
# patterns first.
_RECIPES: list[tuple[_re.Pattern, RecipeMatch]] = [
    (
        # Counting questions across the vault — "how many times
        # have I X", "count X in dailies", etc. Routes to researcher
        # with a 3-step plan that uses the deterministic
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
    (
        # Match BOTH a "dailies" word AND a routine-keyword in any
        # order via lookaheads — earlier the regex required
        # `dailies.*routine` and missed "pull RECURRING items from
        # recent DAILIES" where the order is reversed.
        _re.compile(
            r"(?=.*\b(?:dailies|daily files?|recent dailies)\b)"
            r"(?=.*\b(?:routine|recurring|typically|regular)\b)",
            _re.IGNORECASE | _re.DOTALL,
        ),
        RecipeMatch(
            name="dailies_routine_filter",
            target="scribe",
            body=(
                "RECIPE — dailies + routine:\n"
                "  1. `org-llm_list_dailies(limit=5, include_content=True)`.\n"
                "  2. Filter (REJECT first, never let frequency "
                "override):\n"
                "     - any proper noun mid-task (Cory, RMA, "
                "Postgres, Wiki, Google Drive, etc.) → REJECT\n"
                "     - learning/tutorial/finish-X/follow-X → REJECT\n"
                "     - one-off verb (post, repost, follow up, "
                "send, meeting) → REJECT\n"
                "     - date/event-tied → REJECT\n"
                "     KEEP only universal lowercase chores (make "
                "bed, clean bathroom, take out trash, laundry, "
                "exercise, juice, dishes, groceries, shower, "
                "video games).\n"
                "  3. Draft flat list per CAPTURE STYLE.\n"
                "  4. Ask 'Save to <file>? [y/N]'; skip if "
                "scribe_confirm_before_capture=false.\n"
                "  5. capture_note → offer open_in_emacs.\n"
                "Start step 1 now."
            ),
        ),
    ),
    (
        _re.compile(
            r"\b(dailies|daily files?|recent dailies)\b",
            _re.IGNORECASE,
        ),
        RecipeMatch(
            name="dailies_general",
            target="scribe",
            body=(
                "ORCHESTRATION RECIPE — dailies general:\n"
                "  STEP 1: call "
                "`org-llm_list_dailies(limit=5, include_content=True)` "
                "ONCE — paths + bodies in one call.\n"
                "  STEP 2: answer using the returned content. "
                "Don't re-read files. If the answer requires more "
                "context, broaden with search_notes.\n"
                "Run STEP 1 IMMEDIATELY."
            ),
        ),
    ),
    (
        # Weather-aware agenda: fires when the user asks about
        # weather impact on plans, OR mentions an outdoor activity
        # that's on the agenda. Routes to @agenda (Phase 21.x).
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
            # `*` = fires for any agent the user @-tagged. The
            # weather subsystem is a SHARED capability — researcher
            # and scribe should also benefit from the pre-fetch
            # when the user asks them a weather-flavoured question.
            # The agenda agent is the natural narrator but isn't
            # the only valid target.
            target="*",
            body=(
                "RECIPE — weather-aware agenda:\n"
                "  1. Manager has pre-fetched forecast + agenda + "
                "outdoor-flagged items (see MANAGER PRE-FETCH "
                "below).\n"
                "  2. Lead with the most weather-sensitive item or "
                "the headline summary — never a chronological "
                "dump.\n"
                "  3. Recommend ADJUSTMENTS, not just observations. "
                "If a morning is wet but afternoon clears, suggest "
                "the time shift. Personalise via vault_profile.\n"
                "  4. Don't re-invoke the tool — the data is "
                "authoritative. If you need MORE detail (specific "
                "hour breakdowns), call weather_for_agenda again "
                "with a narrower window."
            ),
        ),
    ),
    (
        _re.compile(
            r"\b(summari[sz]e|recent activity|what.{0,15}been (working|"
            r"doing|up to))\b",
            _re.IGNORECASE,
        ),
        RecipeMatch(
            name="recent_activity_summary",
            target="researcher",
            body=(
                "ORCHESTRATION RECIPE — recent activity:\n"
                "  STEP 1: call "
                "`org-llm_list_recent_nodes(days=14)` AND "
                "`org-llm_recent_files(days=7)` IN PARALLEL "
                "(one tool_calls array, two entries).\n"
                "  STEP 2: synthesise a 1-paragraph summary "
                "grouped by topic. Cite specific note titles. "
                "Don't fabricate."
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
