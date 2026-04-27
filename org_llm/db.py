# [[file:../../../org/20260425230731-org_llm.org::*Models (SQLAlchemy — mirrors schema.sql)][Models (SQLAlchemy — mirrors schema.sql):1]]
from __future__ import annotations

from pathlib import Path

import sqlite_vec
from sqlalchemy import (
    Column, Float, ForeignKey, Index, Integer, LargeBinary, Text,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

DB_PATH = Path("~/.local/share/org-llm/org-llm.db").expanduser()


class Base(DeclarativeBase):
    __allow_unmapped__ = True


class File(Base):
    __tablename__ = "files"

    id         = Column(Integer, primary_key=True)
    path       = Column(Text, nullable=False, unique=True)
    indexed_at = Column(Text, nullable=False)
    node_count = Column(Integer, nullable=False, default=0)
    mtime      = Column(Float, nullable=False)

    nodes      = relationship("Node", back_populates="file",
                              cascade="all, delete-orphan")


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        Index("idx_nodes_file",  "file_id"),
        Index("idx_nodes_title", "title"),
        Index("idx_nodes_tags",  "tags"),
    )

    id        = Column(Integer, primary_key=True)
    file_id   = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    node_id   = Column(Text, unique=True)
    title     = Column(Text, nullable=False)
    body      = Column(Text, nullable=False, default="")
    # Two-bucket tag provenance:
    #   tags       — source-of-truth tags from the org file. The indexer
    #                rewrites this column on every `index` run. Never
    #                written by the auto-tagger; safe to clobber.
    #   auto_tags  — LLM-generated tags layered on top. The auto-tagger
    #                writes here; the indexer never touches it. Reads
    #                that want "all tags" should use merged_tags() below.
    # This split lets `tag --redo` re-tag previously-auto-tagged nodes
    # without clobbering hand-curated org-file tags.
    tags             = Column(Text, nullable=False, default="")
    auto_tags        = Column(Text, nullable=False, default="")
    auto_tagged_at   = Column(Float)            # epoch seconds; NULL = never
    auto_tagger_model = Column(Text)            # model name at time of tagging
    mtime     = Column(Float, nullable=False)
    embedding = Column(LargeBinary)

    file       = relationship("File", back_populates="nodes")


class History(Base):
    __tablename__ = "history"

    id        = Column(Integer, primary_key=True)
    timestamp = Column(Text, nullable=False)
    command   = Column(Text, nullable=False)
    query     = Column(Text, nullable=False)
    response  = Column(Text, nullable=False)


class Config(Base):
    __tablename__ = "config"

    key   = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)


def _load_sqlite_vec(dbapi_conn, _):
    dbapi_conn.enable_load_extension(True)
    sqlite_vec.load(dbapi_conn)
    dbapi_conn.enable_load_extension(False)
    dbapi_conn.execute("PRAGMA foreign_keys = ON")


def make_engine(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=False)
    event.listen(engine, "connect", _load_sqlite_vec)
    _migrate_in_place(engine)
    return engine


def _migrate_in_place(engine) -> None:
    """Apply additive schema migrations on existing DBs.

    SQLAlchemy's create_all() adds tables but never columns. New columns
    introduced over time (auto_tags / auto_tagged_at / auto_tagger_model)
    must be added here so old DBs keep working without a manual reindex.
    Safe to call on a freshly-created DB — the existence checks no-op.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if not insp.has_table("nodes"):
        return  # init_db hasn't run yet — create_all will add everything.
    cols = {c["name"] for c in insp.get_columns("nodes")}
    additions: list[str] = []
    if "auto_tags" not in cols:
        additions.append(
            "ALTER TABLE nodes ADD COLUMN auto_tags TEXT NOT NULL DEFAULT ''"
        )
    if "auto_tagged_at" not in cols:
        additions.append("ALTER TABLE nodes ADD COLUMN auto_tagged_at REAL")
    if "auto_tagger_model" not in cols:
        additions.append("ALTER TABLE nodes ADD COLUMN auto_tagger_model TEXT")
    if not additions:
        return
    with engine.begin() as conn:
        for stmt in additions:
            conn.execute(text(stmt))


def merged_tags(node) -> str:
    """Union of file-source tags and LLM auto-tags as a space-joined string.

    Order: file tags first (preserves user's authored order), then any
    auto-tags not already present. Use this anywhere a "what tags does
    this node have" question is asked from Python — for SQL filters,
    use `merged_tags_sql()` so the DB does the union itself.
    """
    file_tags = (getattr(node, "tags", "") or "").split()
    auto      = (getattr(node, "auto_tags", "") or "").split()
    seen = set()
    out: list[str] = []
    for t in file_tags + auto:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def merged_tags_sql(alias: str = "n") -> str:
    """SQL fragment for the merged tag string of a row aliased as `alias`.

    Use in raw SQL: `WHERE lower(<merged>) LIKE :pat`. The COALESCE guards
    rows from before the auto_tags migration ran (where auto_tags is ''
    by DEFAULT, but defensive doesn't hurt).
    """
    return f"({alias}.tags || ' ' || COALESCE({alias}.auto_tags, ''))"


MODEL_DEFAULTS = {
    "org_dir":        "~/org",
    "ollama_url":     "http://localhost:11434",
    # Defaults are tuned to fit a laptop CPU/16 GB RAM out of the box. Use
    # `org-llm models --tune` once you've got real hardware to scale up.
    "embed_model":    "nomic-embed-text",   # 137 MB — semantic search
    "chat_model":     "llama3.2",           # 2.0 GB — ask / Q&A (first-flight friendly)
    "code_model":     "qwen2.5-coder",      # 4.7 GB — code generation
    "reason_model":   "deepseek-r1:7b",     # 4.7 GB — planning (full deepseek-r1 is 40 GB)
    "fast_model":     "phi3.5",             # 2.2 GB — tagging, classification
    "instruct_model": "mistral-nemo",       # 7.1 GB — capture, instruction following
    "text_model":     "gemma3",             # 5.4 GB — summarization, text analysis
    "embed_dim":      "768",
    "theme":          "dark",               # dark | light  (UI color mode)
    "code_dirs":      "~/repos",            # comma-sep paths for `code-index`
    "db_version":     "1",
}


def init_db(engine) -> None:
    from org_llm.skills import Skill  # ensure table is registered before create_all
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for k, v in MODEL_DEFAULTS.items():
            if not s.get(Config, k):
                s.add(Config(key=k, value=v))
        s.commit()


def get_session(engine) -> Session:
    return sessionmaker(bind=engine)()
# Models (SQLAlchemy — mirrors schema.sql):1 ends here
