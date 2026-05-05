# [[file:../../../org/20260425230731-org_llm.org::*test_db.py][test_db.py:1]]
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from org_llm.db import (
    Config, ConfigOverride, File, Node, History, MODEL_DEFAULTS, get_session,
    current_env, current_device, get_config_value, set_config_value,
    ANY_SCOPE,
)


class TestSchema:
    def test_all_tables_created(self, db_engine):
        tables = set(inspect(db_engine).get_table_names())
        assert {"files", "nodes", "history", "config", "skills"} <= tables

    def test_config_defaults_all_present(self, session):
        for key, val in MODEL_DEFAULTS.items():
            row = session.get(Config, key)
            assert row is not None, f"Missing config key: {key}"
            assert row.value == val

    def test_init_idempotent(self, db_engine):
        from org_llm.db import init_db
        init_db(db_engine)   # second call — should not raise or duplicate rows
        with get_session(db_engine) as s:
            count = s.query(Config).filter_by(key="db_version").count()
        assert count == 1


class TestCRUD:
    def test_config_update(self, session):
        row = session.get(Config, "chat_model")
        row.value = "my-model"
        session.commit()
        assert session.get(Config, "chat_model").value == "my-model"

    def test_node_unique_id_constraint(self, session):
        f = File(path="/tmp/u.org", indexed_at="now", node_count=1, mtime=1.0)
        session.add(f)
        session.flush()
        session.add(Node(file_id=f.id, node_id="dup", title="A",
                         body="", tags="", mtime=1.0))
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(Node(file_id=f.id, node_id="dup", title="B",
                             body="", tags="", mtime=1.0))
            session.commit()
        session.rollback()

    def test_cascade_delete_removes_nodes(self, session):
        f = File(path="/tmp/c.org", indexed_at="now", node_count=2, mtime=1.0)
        session.add(f)
        session.flush()
        for title in ("X", "Y"):
            session.add(Node(file_id=f.id, title=title, body="",
                             tags="", mtime=1.0))
        session.commit()
        assert session.query(Node).count() == 2

        session.delete(f)
        session.commit()
        assert session.query(Node).count() == 0

    def test_history_insert(self, session):
        from org_llm.db import History
        session.add(History(timestamp="now", command="ask",
                            query="q", response="r"))
        session.commit()
        assert session.query(History).count() == 1


# ── (device, env) scoped Config — DEC-016 candidate ──────────────────────────
#
# The Config table keeps its singular `key` PK so existing call sites using
# `s.get(Config, key)` keep working. Per-(device, env) overrides live in the
# new ConfigOverride table; get_config_value() walks specific -> any with the
# precedence pinned by the tests below.

class TestConfigScopeSchema:
    def test_config_overrides_table_exists(self, db_engine):
        tables = set(inspect(db_engine).get_table_names())
        assert "config_overrides" in tables

    def test_existing_rows_treated_as_any_scope(self, session):
        """Pre-existing Config rows are treated as the (any, any) scope —
        no migration writes needed because the lookup falls back to the
        base Config row when no scoped override matches."""
        # Plant a base row, then look it up with a non-default scope —
        # it should still resolve via the base-row fallback.
        session.add(Config(key="phase16_test_key", value="base-value"))
        session.commit()
        v = get_config_value(session, "phase16_test_key",
                              device="laptop", env="dev")
        assert v == "base-value"


class TestEnvHelper:
    def test_default_env_is_dev(self, monkeypatch):
        monkeypatch.delenv("ORG_LLM_ENV", raising=False)
        assert current_env() == "dev"

    def test_env_var_acpt(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_ENV", "acpt")
        assert current_env() == "acpt"

    def test_env_var_prod(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_ENV", "prod")
        assert current_env() == "prod"

    def test_unknown_env_falls_back_to_dev(self, monkeypatch):
        """A typo (e.g. ORG_LLM_ENV=production) must not silently switch
        the user into a phantom scope — collapse to 'dev' so the lookup
        stays predictable."""
        monkeypatch.setenv("ORG_LLM_ENV", "production")
        assert current_env() == "dev"

    def test_env_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_ENV", "PROD")
        assert current_env() == "prod"


class TestDeviceHelper:
    def test_explicit_device_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("ORG_LLM_DEVICE", "framework-13")
        assert current_device() == "framework-13"

    def test_falls_back_to_hostname(self, monkeypatch):
        monkeypatch.delenv("ORG_LLM_DEVICE", raising=False)
        d = current_device()
        # We can't assert the exact hostname, but it should be non-empty
        # and not the literal sentinel unless socket failed.
        assert d and isinstance(d, str)


class TestConfigOverrideCRUD:
    def test_set_then_get_roundtrip(self, session):
        set_config_value(session, "chat_model", "gpt-9000",
                            device="laptop", env="dev")
        v = get_config_value(session, "chat_model",
                              device="laptop", env="dev")
        assert v == "gpt-9000"

    def test_set_with_no_scope_writes_base_row(self, session):
        set_config_value(session, "phase16_base_key", "base-only")
        assert session.get(Config, "phase16_base_key").value == "base-only"
        # And no override row should have been created
        assert (session.query(ConfigOverride)
                       .filter_by(key="phase16_base_key").count() == 0)

    def test_set_is_upsert(self, session):
        set_config_value(session, "k", "v1", device="d", env="dev")
        set_config_value(session, "k", "v2", device="d", env="dev")
        rows = (session.query(ConfigOverride)
                       .filter_by(key="k", device="d", env="dev").all())
        assert len(rows) == 1
        assert rows[0].value == "v2"


class TestLookupPrecedence:
    """Pin the precedence ladder. Each test plants exactly the rows it
    cares about and asserts the lookup picks the most-specific match."""

    def test_full_specific_beats_device_only(self, session):
        # device-only override
        set_config_value(session, "k", "device-only",
                            device="laptop", env=ANY_SCOPE)
        # full-specific override
        set_config_value(session, "k", "specific",
                            device="laptop", env="prod")
        v = get_config_value(session, "k", device="laptop", env="prod")
        assert v == "specific"

    def test_device_only_beats_env_only(self, session):
        set_config_value(session, "k", "env-only",
                            device=ANY_SCOPE, env="prod")
        set_config_value(session, "k", "device-only",
                            device="laptop", env=ANY_SCOPE)
        v = get_config_value(session, "k", device="laptop", env="prod")
        assert v == "device-only"

    def test_env_only_beats_base_row(self, session):
        session.add(Config(key="k", value="base"))
        session.commit()
        set_config_value(session, "k", "env-only",
                            device=ANY_SCOPE, env="prod")
        v = get_config_value(session, "k", device="laptop", env="prod")
        assert v == "env-only"

    def test_base_row_beats_model_defaults(self, session):
        # `chat_model` is in MODEL_DEFAULTS — base Config row should win
        # over the code default.
        row = session.get(Config, "chat_model")
        row.value = "user-override"
        session.commit()
        v = get_config_value(session, "chat_model",
                              device="laptop", env="prod")
        assert v == "user-override"

    def test_model_defaults_when_nothing_else(self, session):
        # Pick a key that does NOT exist as either a base row or override
        # but DOES exist in MODEL_DEFAULTS.
        # Delete the seeded chat_model row to simulate a missing base.
        row = session.get(Config, "chat_model")
        if row:
            session.delete(row)
            session.commit()
        v = get_config_value(session, "chat_model",
                              device="laptop", env="dev")
        assert v == MODEL_DEFAULTS["chat_model"]

    def test_unknown_key_returns_supplied_default(self, session):
        v = get_config_value(session, "nope_does_not_exist",
                              device="laptop", env="dev",
                              default="sentinel")
        assert v == "sentinel"

    def test_uses_runtime_env_when_unspecified(self, session, monkeypatch):
        monkeypatch.setenv("ORG_LLM_ENV", "acpt")
        monkeypatch.setenv("ORG_LLM_DEVICE", "laptop")
        set_config_value(session, "k", "acpt-laptop",
                            device="laptop", env="acpt")
        # Look up without specifying device/env — should pick up runtime
        v = get_config_value(session, "k")
        assert v == "acpt-laptop"
# test_db.py:1 ends here
