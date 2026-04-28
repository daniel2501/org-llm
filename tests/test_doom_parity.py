"""Doom keybinding parity guard.

Ensures every (non-blocked) CLI verb is reachable from the Doom Emacs
integration. The check is structural: each registered Typer verb must
appear somewhere in doom/org-llm.el — either as part of an
`(org-llm-foo …)` invocation or as a literal "verb" string fed to
the binary. This catches the failure mode where a new CLI feature
ships and quietly stops being reachable from `SPC l`.

Doesn't validate keybinding choices, prefix structure, or argument
forwarding — those are taste questions. Only catches "this verb
exists in the CLI but no path to it from doom".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from org_llm.cli import app


# Verbs we deliberately don't expose from the doom integration:
#   - mcp / completion: shell-only, never useful from emacs
#   - install-tools / install / setup: long interactive flows that
#     don't fit in a vterm side-window UX
#   - grant* / revoke* : security-gated (intentionally CLI-only)
#   - log-show / db-info / etc don't appear here because Typer
#     names them by their decorator name = the verb itself.
_BLOCKED = {
    "mcp", "completion",
    "install-tools", "install", "setup",
    "grant", "grant-root", "grant-browser",
    "revoke", "revoke-root", "revoke-browser",
}


@pytest.fixture(scope="module")
def doom_text() -> str:
    p = Path(__file__).resolve().parent.parent / "doom" / "org-llm.el"
    assert p.exists(), f"missing doom integration at {p}"
    return p.read_text()


def _registered_verbs() -> set[str]:
    return set(typer.main.get_command(app).commands.keys())


def test_doom_integration_file_exists(doom_text):
    """Sanity: the file is present and has the keybindings block."""
    assert ":leader" in doom_text and ":prefix" in doom_text
    assert "org-llm-binary" in doom_text


def test_every_non_blocked_verb_has_doom_path(doom_text):
    """Each CLI verb that's safe to expose from emacs MUST appear in
    doom/org-llm.el — either as a wrapper function (org-llm-<verb>),
    a literal "verb" passed to the binary, or as a sub-prefix label.
    """
    missing: list[str] = []
    for verb in sorted(_registered_verbs() - _BLOCKED):
        # org-llm-foo / org-llm-foo-bar / "foo" / " foo " in shell calls.
        patterns = [
            rf"org-llm-{re.escape(verb)}\b",
            rf"\"{re.escape(verb)}\"",            # bare "foo"
            rf"\"{re.escape(verb)}\\s",           # "foo "
            rf"\"{re.escape(verb)} ",
            rf'\b{re.escape(verb)}-',              # part of a function name
        ]
        if not any(re.search(p, doom_text) for p in patterns):
            missing.append(verb)
    assert not missing, (
        f"CLI verbs missing from doom/org-llm.el: {missing}\n"
        "Add a wrapper function and a key under SPC l so the new "
        "verb stays reachable from Doom Emacs."
    )


def test_keybinding_section_groups_by_prefix(doom_text):
    """Soft check: top-level SPC l keys + at least 4 sub-prefixes.
    Pins the ergonomic shape against accidental flattening."""
    leader = doom_text.split(":leader", 1)[1]
    sub_prefixes = re.findall(r"\(:prefix \(\"([A-Z])\" \. \"[^\"]+\"\)", leader)
    assert len(sub_prefixes) >= 4, (
        f"expected ≥4 sub-prefixes (log/dbt/skills/config/etc), "
        f"got: {sub_prefixes}")


def test_high_value_new_verbs_are_bound(doom_text):
    """Specific keybindings the user asked for explicitly:
    log / dbt / launch-cloud / watch / config-tangle / power-boost."""
    must_appear = [
        "org-llm-log",
        "org-llm-log-reflect",
        "org-llm-dbt-status",
        "org-llm-dbt-build",
        "org-llm-launch-cloud",
        "org-llm-watch",
        "org-llm-config-tangle",
        "org-llm-config-apply-from-org",
        "org-llm-doctor-power-boost",
        "org-llm-models-set",
    ]
    missing = [n for n in must_appear if n not in doom_text]
    assert not missing, f"high-value bindings missing: {missing}"
