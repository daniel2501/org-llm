# [[file:../../../org/20260425230731-org_llm.org::*test_db.py][test_db.py:1]]
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from org_llm.db import Config, File, Node, History, MODEL_DEFAULTS, get_session


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
# test_db.py:1 ends here
