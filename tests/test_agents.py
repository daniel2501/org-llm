"""Phase 23.1 — agent dataclass + alias system."""

from __future__ import annotations

import pytest
from pathlib import Path


def test_agent_dataclass_smoke():
    from org_llm.agents import Agent
    a = Agent(
        birth_name="testagent",
        description="test",
        persona="you are a test",
        aliases=("alias1", "alias2"),
    )
    assert a.birth_name == "testagent"
    assert a.all_names() == ("testagent", "alias1", "alias2")
    assert a.origin == "builtin"
    assert a.pack == "starfleet-core"
    assert a.addressable is True
    # Phase 23.2-23.4 slots default to empty.
    assert a.capabilities == ()
    assert a.recipes == ()
    assert a.hygiene_scan is None
    legacy = a.to_legacy_dict()
    assert legacy["description"] == "test"
    assert legacy["prompt"] == "you are a test"
    assert legacy["model_role"] == "chat_model"


def test_capability_constants_match_wiki_spec():
    """Canonical capability set documented in
    docs/wiki/agent-framework.org. Locking the spelling here so
    typos in capability declarations get caught early."""
    from org_llm.agents._types import (
        ALL_CAPABILITIES,
        CAPABILITY_READ_VAULT, CAPABILITY_WRITE_VAULT,
        CAPABILITY_READ_CONFIG, CAPABILITY_WRITE_CONFIG,
        CAPABILITY_SHELL, CAPABILITY_EXTERNAL_NETWORK,
    )
    assert CAPABILITY_READ_VAULT       == "read.vault"
    assert CAPABILITY_WRITE_VAULT      == "write.vault"
    assert CAPABILITY_READ_CONFIG      == "read.config"
    assert CAPABILITY_WRITE_CONFIG     == "write.config"
    assert CAPABILITY_SHELL            == "shell"
    assert CAPABILITY_EXTERNAL_NETWORK == "external.network"
    assert len(ALL_CAPABILITIES) == 6


def test_core_agents_capabilities_populated():
    """Each core agent declares its capabilities. Picard
    (manager) intentionally has none — it delegates instead of
    calling tools directly."""
    from org_llm.agents import get_builtins
    by_name = {a.birth_name: a for a in get_builtins()}

    assert by_name["spock"].capabilities   == ("read.vault",)
    assert by_name["data"].capabilities    == ("read.vault", "write.vault")
    assert by_name["geordi"].capabilities  == ("read.vault", "write.vault", "shell")
    assert by_name["riker"].capabilities   == ("read.vault",)
    assert by_name["janeway"].capabilities == ("read.vault", "external.network")
    assert by_name["scotty"].capabilities  == ("read.vault", "read.config",
                                                "write.config", "shell")
    assert by_name["soong"].capabilities   == ("read.vault", "write.vault", "shell")
    assert by_name["keiko"].capabilities   == ("read.vault", "shell")
    assert by_name["picard"].capabilities  == ()  # delegates only

    # has_capability helper.
    assert by_name["geordi"].has_capability("shell") is True
    assert by_name["spock"].has_capability("shell")  is False


def test_agent_recipes_declared_per_owner():
    """Recipes declare ownership per-agent. The matcher in
    orchestration._RECIPES is still authoritative; these
    declarations are documentation until Phase 23.4 rewires."""
    from org_llm.agents import get_builtins
    by_name = {a.birth_name: a for a in get_builtins()}
    assert "count_across_vault"   in by_name["spock"].recipes
    assert "weather_aware_agenda" in by_name["janeway"].recipes
    # Picard owns no recipes — the captain dispatches; the reflex
    # layer fires from any specialist's owned set.
    assert by_name["picard"].recipes == ()


def test_capability_declarations_use_canonical_set():
    """Every declared capability matches the canonical set —
    catches typos like 'read_vault' (underscore) at test time."""
    from org_llm.agents        import get_builtins
    from org_llm.agents._types import ALL_CAPABILITIES
    for a in get_builtins():
        for cap in a.capabilities:
            assert cap in ALL_CAPABILITIES, (
                f"{a.birth_name} declares unknown capability {cap!r}"
            )


def test_agent_is_frozen():
    from org_llm.agents import Agent
    a = Agent(birth_name="x", description="", persona="")
    with pytest.raises(Exception):
        a.birth_name = "y"   # type: ignore[misc]


def test_builtins_count_and_packs():
    from org_llm.agents import get_builtins
    agents = get_builtins()
    core   = [a for a in agents if a.pack == "starfleet-core"]
    legacy = [a for a in agents if a.pack == "legacy-extras"]

    # 9 OOB (locked 2026-05-03): picard + 8 specialists
    assert len(core) == 9
    assert {a.birth_name for a in core} == {
        "picard", "spock", "data", "geordi", "riker",
        "janeway", "scotty", "soong", "keiko",
    }

    # legacy survives in code (not removed) so re-enabling via
    # the agents_include_legacy knob is just a config flip.
    assert len(legacy) >= 1


def test_alias_resolution():
    from org_llm.agents import resolve_alias
    # Birth-name resolves to itself.
    assert resolve_alias("picard") == "picard"
    assert resolve_alias("spock")  == "spock"

    # Functional aliases resolve to the Trek birth-name.
    assert resolve_alias("crew")       == "picard"
    assert resolve_alias("captain")    == "picard"
    assert resolve_alias("researcher") == "spock"
    assert resolve_alias("scribe")     == "data"
    assert resolve_alias("engineer")   == "geordi"
    assert resolve_alias("planner")    == "riker"
    assert resolve_alias("agenda")     == "janeway"
    assert resolve_alias("ops")        == "scotty"
    assert resolve_alias("agentsmith") == "soong"
    assert resolve_alias("gardener")   == "keiko"

    # Case-insensitive.
    assert resolve_alias("Crew")    == "picard"
    assert resolve_alias("PICARD")  == "picard"
    assert resolve_alias(" spock ") == "spock"

    # Unknown → None (not an exception).
    assert resolve_alias("ohura")    is None
    assert resolve_alias("")         is None


def test_route_prompt_returns_birth_name(cli_org):
    """`route_prompt` returns the canonical birth-name now."""
    from org_llm.cli import route_prompt
    # "find" / "search" are in researcher's triggers → spock.
    name, score, hits = route_prompt("find me phase 18 notes",
                                       org_dir=cli_org)
    assert name == "spock"
    assert score >= 1
    assert "find" in hits

    # "schedule this week" → janeway (agenda).
    name, _, _ = route_prompt("what's on my schedule this week",
                                org_dir=cli_org)
    assert name == "janeway"

    # No triggers → 'chat' fallback.
    name, score, hits = route_prompt("hello there",
                                       org_dir=cli_org)
    assert name == "chat"
    assert score == 0


def test_resolve_active_agents_filters_to_core_by_default(cli_org):
    """Default: only starfleet-core registers; legacy is opt-in."""
    from org_llm.cli import _resolve_active_agents
    active = _resolve_active_agents(cli_org)
    assert {a.pack for a in active} == {"starfleet-core"}
    assert len(active) == 9


def test_resolve_active_agents_include_legacy(cli_org):
    """Knob `agents_include_legacy=true` re-enables legacy
    agents during the transition window."""
    from org_llm.cli import _resolve_active_agents
    active = _resolve_active_agents(cli_org, include_legacy=True)
    packs = {a.pack for a in active}
    assert "starfleet-core"  in packs
    assert "legacy-extras"   in packs
    assert len(active) == 17


def test_user_override_via_org_file_auto_applies(cli_org):
    """File-edit UX: write a :agent:-tagged heading, run
    resolution, see the change land. Under the hood the file is
    auto-applied into the DB; the resolver reads from DB.

    `spock` is a built-in being overridden — origin stays builtin
    (round-trip preserves the source classification). `uhura` is
    new in the file — smart-default flips origin/pack to 'user'.
    """
    from org_llm.cli import _resolve_active_agents
    import os, time
    user_file = cli_org / "org-llm-agents.org"
    user_file.write_text(
        "* spock                                            :agent:\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: Custom researcher persona\n"
        ":ALIASES:    researcher, science\n"
        ":END:\n"
        "\n"
        "you are spock, the science officer. customised body.\n"
        "\n"
        "* uhura                                            :agent:\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: Comms officer (user-added)\n"
        ":END:\n"
        "\n"
        "you are uhura, communications. user-defined.\n"
    )
    # Force mtime newer than any DB row so auto-apply fires.
    future = time.time() + 5
    os.utime(user_file, (future, future))

    active = _resolve_active_agents(cli_org)
    by_name = {a.birth_name: a for a in active}
    assert "spock" in by_name
    spock = by_name["spock"]
    assert "Custom researcher" in spock.description
    assert "science" in spock.aliases

    assert "uhura" in by_name
    uhura = by_name["uhura"]
    # Smart default for new agents not in built-ins.
    assert uhura.origin == "user"
    assert uhura.pack   == "user"


def test_render_agent_block_emits_one_entry_per_alias(cli_org):
    """Launcher writes opencode.json with birth_name + every
    alias as separate entries, all carrying x_org_llm_birth_name."""
    from org_llm.cli import (_render_agent_block_for_opencode,
                              _resolve_active_agents)
    active = _resolve_active_agents(cli_org)
    block = _render_agent_block_for_opencode(
        active, cfg_rows={}, default_model="chat",
        provider_id="local",
    )
    # Picard registers 3 names: birth + 2 aliases.
    for name in ("picard", "crew", "captain"):
        assert name in block
        assert block[name]["x_org_llm_birth_name"] == "picard"
        # Same prompt across all three.
        assert block[name]["prompt"] == block["picard"]["prompt"]

    # Spock + researcher both point at spock.
    for name in ("spock", "researcher"):
        assert name in block
        assert block[name]["x_org_llm_birth_name"] == "spock"


def test_db_seed_is_idempotent(cli_org):
    """`agents --seed` populates the agent table from built-ins.
    Running it twice doesn't duplicate."""
    from typer.testing      import CliRunner
    from org_llm.cli        import app
    from org_llm.db         import AgentRow, make_engine, get_session
    from org_llm.agents     import get_builtins
    runner = CliRunner()

    r1 = runner.invoke(app, ["agents", "--seed"])
    assert r1.exit_code == 0
    r2 = runner.invoke(app, ["agents", "--seed"])
    assert r2.exit_code == 0
    assert "already seeded" in r2.output or "no rows added" in r2.output

    import os
    engine = make_engine(Path(os.environ["ORG_LLM_DB"]))
    with get_session(engine) as s:
        rows = list(s.query(AgentRow).all())
    # 17 built-ins → 17 rows.
    assert len(rows) == len(get_builtins())
    assert {r.birth_name for r in rows} == {a.birth_name for a in get_builtins()}


def test_db_row_overrides_builtin(cli_org):
    """A DB row replaces the matching Python default at
    resolution time."""
    from typer.testing  import CliRunner
    from org_llm.cli    import app, _resolve_active_agents
    runner = CliRunner()
    runner.invoke(app, ["agents", "--seed"])
    runner.invoke(app, ["agent", "set", "spock", "description",
                          "Custom researcher description"])

    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    assert by_name["spock"].description == "Custom researcher description"


def test_db_row_can_add_new_agent(cli_org):
    """A DB row with a birth_name not in built-ins becomes a
    new agent, origin/pack=user."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    runner = CliRunner()
    r = runner.invoke(app, ["agent", "set", "uhura",
                              "persona",
                              "you are uhura, comms officer."])
    assert r.exit_code == 0, r.output
    runner.invoke(app, ["agent", "set", "uhura",
                         "description", "Comms officer (custom)"])

    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    assert "uhura" in by_name
    assert by_name["uhura"].origin == "user"
    assert by_name["uhura"].pack   == "user"
    assert "comms officer" in by_name["uhura"].persona


def test_db_disable_drops_agent_from_resolved_set(cli_org):
    """`agent disable <name>` flags enabled=0; the resolver
    drops it. `agent enable` flips back."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    runner = CliRunner()
    runner.invoke(app, ["agents", "--seed"])
    runner.invoke(app, ["agent", "disable", "spock"])

    names = {a.birth_name for a in _resolve_active_agents(cli_org)}
    assert "spock" not in names

    runner.invoke(app, ["agent", "enable", "spock"])
    names = {a.birth_name for a in _resolve_active_agents(cli_org)}
    assert "spock" in names


def test_db_reset_falls_back_to_builtin(cli_org):
    """`agent reset <name>` drops the DB row and the Python
    default re-takes effect."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    from org_llm.agents import get_builtins
    runner = CliRunner()
    builtin_desc = next(a for a in get_builtins()
                          if a.birth_name == "spock").description

    runner.invoke(app, ["agent", "set", "spock", "description", "EDITED"])
    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    assert by_name["spock"].description == "EDITED"

    runner.invoke(app, ["agent", "reset", "spock"])
    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    assert by_name["spock"].description == builtin_desc


def test_auto_apply_when_file_newer_than_db(cli_org):
    """Shape A — when the org file's mtime is newer than the
    newest DB row's updated_at, auto-apply lands the file's
    edits into the DB before resolution. The 'edit file → launch
    picks it up' UX still works."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    import os, time
    runner = CliRunner()
    runner.invoke(app, ["agent", "set", "spock", "description", "FROM-DB"])

    user_path = cli_org / "org-llm-agents.org"
    user_path.write_text(
        "* spock                                    :agent:\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: FROM-ORG-FILE\n"
        ":END:\n"
        "\n"
        "you are spock\n"
    )
    # Force file mtime to be 5 seconds in the future so the
    # ordering is unambiguous (avoids same-second tie).
    future = time.time() + 5
    os.utime(user_path, (future, future))

    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    # Auto-apply fired → file's value lands in DB → resolver reads
    # the freshly-applied row.
    assert by_name["spock"].description == "FROM-ORG-FILE"


def test_no_auto_apply_when_db_newer(cli_org):
    """Inverse of above — when DB has been edited more recently
    than the file, auto-apply skips and DB wins."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    import os, time
    runner = CliRunner()

    user_path = cli_org / "org-llm-agents.org"
    user_path.write_text(
        "* spock                                    :agent:\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: FROM-OLD-FILE\n"
        ":END:\n"
        "\n"
        "you are spock\n"
    )
    # Force file mtime to 1h in the past.
    past = time.time() - 3600
    os.utime(user_path, (past, past))

    runner.invoke(app, ["agent", "set", "spock", "description", "FROM-DB"])
    by_name = {a.birth_name: a for a in _resolve_active_agents(cli_org)}
    assert by_name["spock"].description == "FROM-DB"


def test_db_can_promote_legacy_agent_to_core(cli_org):
    """A user can change an agent's pack via the DB layer to
    flip it from legacy-extras → starfleet-core (so it shows
    without setting agents_include_legacy)."""
    from typer.testing import CliRunner
    from org_llm.cli   import app, _resolve_active_agents
    runner = CliRunner()
    # Default: include_legacy off → analyst is filtered out.
    names = {a.birth_name for a in _resolve_active_agents(cli_org)}
    assert "analyst" not in names

    runner.invoke(app, ["agent", "set", "analyst", "pack", "starfleet-core"])
    names = {a.birth_name for a in _resolve_active_agents(cli_org)}
    assert "analyst" in names


def test_tangle_apply_roundtrip_is_identity(cli_org):
    """Tangle the resolved set, immediately apply — diff should
    be empty (or limited to legacy add/remove since the legacy
    knob is off by default)."""
    from org_llm.cli      import _resolve_preconfigured_agents
    from org_llm.cli      import _load_agents_from_org
    from typer.testing    import CliRunner
    from org_llm.cli      import app
    runner = CliRunner()
    runner.invoke(app, ["agents", "--tangle"])
    user_path = cli_org / "org-llm-agents.org"
    assert user_path.exists()

    on_disk = _load_agents_from_org(user_path)
    assert on_disk is not None
    # Every active built-in roundtrips by birth_name.
    resolved_now = _resolve_preconfigured_agents(cli_org)
    for birth_name in resolved_now:
        assert birth_name in on_disk, f"{birth_name} missing post-tangle"
        # Description preserved.
        assert (on_disk[birth_name]["description"]
                 == resolved_now[birth_name]["description"])
        # Persona body preserved.
        assert (on_disk[birth_name]["prompt"]
                 == resolved_now[birth_name]["prompt"])
