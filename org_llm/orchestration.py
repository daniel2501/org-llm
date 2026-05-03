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
        _re.compile(
            r"\b(dailies|daily files?|recent dailies)\b.*"
            r"\b(routine|recurring|typically|regular)\b",
            _re.IGNORECASE | _re.DOTALL,
        ),
        RecipeMatch(
            name="dailies_routine_filter",
            target="scribe",
            body=(
                "RECIPE — dailies + routine:\n"
                "  1. `org-llm_list_dailies(limit=5, include_content=True)`.\n"
                "  2. Extract tasks from bodies. APPLY ALL "
                "REJECTIONS BEFORE KEEPING ANYTHING:\n"
                "     - ANY proper noun (capitalised name): person "
                "(Cory, Jamie, Karen…), org/project (RMA, IDEXX, "
                "Postgres, dbt, Riverton Mutual Aid…), brand "
                "(Google Drive, Wiki…). If the task contains a "
                "capital-letter name that isn't the start of the "
                "sentence, REJECT.\n"
                "     - room/place names ('Cory and Jamie's room'). "
                "REJECT.\n"
                "     - learning/tutorial/'continue X'/'finish X'/"
                "'follow X'. REJECT.\n"
                "     - one-off verb (post, repost, follow up, "
                "send, reach out, meeting, organise). REJECT.\n"
                "     - date/event-tied ('next meeting', 'this "
                "Saturday'). REJECT.\n"
                "     - 'wiki', 'docs', 'links' references unless "
                "the user habitually edits them weekly. REJECT.\n"
                "     Frequency does NOT override rejection. A "
                "task appearing 5 times that has a proper noun is "
                "STILL rejected.\n"
                "     KEEP only universal lowercase chores: make "
                "bed, clean bathroom, take out trash, do laundry, "
                "exercise, juice, dishes, cook, groceries (no "
                "'Whole Foods'), shower, video games. Anything "
                "needing a capital letter beyond sentence start = "
                "reject.\n"
                "  3. Draft flat list per CAPTURE STYLE.\n"
                "  4. Ask 'Save to <file>? [y/N]', wait for y "
                "(skip if scribe_confirm_before_capture=false).\n"
                "  5. capture_note → offer open_in_emacs.\n"
                "Start step 1 immediately."
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
