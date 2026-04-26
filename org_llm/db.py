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
    tags      = Column(Text, nullable=False, default="")
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
    return engine


MODEL_DEFAULTS = {
    "org_dir":        "~/org",
    "ollama_url":     "http://localhost:11434",
    "embed_model":    "nomic-embed-text",   # semantic search
    "chat_model":     "llama3.3",           # ask / Q&A
    "code_model":     "qwen2.5-coder",      # code generation
    "reason_model":   "deepseek-r1",        # planning, complex reasoning
    "fast_model":     "phi4",               # tagging, classification
    "instruct_model": "mistral-nemo",       # capture, instruction following
    "text_model":     "gemma3",             # summarization, text analysis
    "embed_dim":      "768",
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
