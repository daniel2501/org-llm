# [[file:../../../org/20260425230731-org_llm.org::*skills.py][skills.py:1]]
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import orgparse
from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import Session

from .db import Base


class Skill(Base):
    __tablename__ = "skills"
    __allow_unmapped__ = True

    id         = Column(Integer, primary_key=True)
    name       = Column(Text, nullable=False, unique=True)
    lang       = Column(Text, nullable=False, default="python")
    model_key  = Column(Text, nullable=False, default="text_model")
    source     = Column(Text, nullable=False)    # the raw src block body
    file_path  = Column(Text, nullable=False)
    heading    = Column(Text, nullable=False, default="")


def _iter_skill_blocks(path: Path):
    """Yield (name, lang, model_key, source, heading) for each :skill: block."""
    org = orgparse.load(str(path))
    for node in org:
        if "skill" not in (node.tags or []):
            continue
        name      = node.get_property("SKILL_NAME") or node.heading.lower().replace(" ", "_")
        model_key = node.get_property("SKILL_MODEL") or "text_model"
        lang      = node.get_property("SKILL_LANG")  or "python"
        # extract first src block body from node body
        body = node.body or ""
        m = re.search(r"#\+begin_src\s+\S+.*?\n(.*?)#\+end_src", body,
                      re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        source = m.group(1).rstrip()
        yield name, lang, model_key, source, node.heading


def index_skills(session: Session, org_dir: Path) -> int:
    """Scan org_dir for skill blocks and upsert them into the DB."""
    count = 0
    for path in org_dir.rglob("*.org"):
        for name, lang, model_key, source, heading in _iter_skill_blocks(path):
            existing = session.query(Skill).filter_by(name=name).first()
            if existing:
                existing.lang = lang
                existing.model_key = model_key
                existing.source = source
                existing.file_path = str(path)
                existing.heading = heading
            else:
                session.add(Skill(
                    name=name, lang=lang, model_key=model_key,
                    source=source, file_path=str(path), heading=heading,
                ))
            count += 1
    session.commit()
    return count


def run_skill(
    skill: Skill,
    input_text: str,
    cfg: dict[str, str],
    base_url: str,
) -> str:
    """Execute a skill: substitute {{input}} and {{cfg.*}}, run via llm or exec."""
    from . import llm as llm_mod

    src = skill.source
    src = src.replace("{{input}}", input_text)
    for k, v in cfg.items():
        src = src.replace(f"{{{{cfg.{k}}}}}", v)

    if skill.lang in ("python",):
        # Inject helpers and run
        model = cfg.get(skill.model_key, "llama3.3")
        header = (
            f"import sys\n"
            f"class _LLM:\n"
            f"    def chat(self, p, model='{model}'):\n"
            f"        import ollama\n"
            f"        c = ollama.Client(host='{base_url}')\n"
            f"        return c.chat(model=model, messages=[{{'role':'user','content':p}}]).message.content\n"
            f"llm = _LLM()\n"
            f"cfg = {cfg!r}\n\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(header + src)
            tmp = f.name
        result = subprocess.run(["python3", tmp], capture_output=True, text=True)
        Path(tmp).unlink(missing_ok=True)
        return result.stdout.strip() or result.stderr.strip()

    elif skill.lang in ("sh", "shell", "bash"):
        with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", delete=False) as f:
            f.write("#!/usr/bin/env sh\n" + src)
            tmp = f.name
        Path(tmp).chmod(0o755)
        result = subprocess.run([tmp], capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin:/home/daniel/.local/bin",
                                     **cfg})
        Path(tmp).unlink(missing_ok=True)
        return result.stdout.strip()

    else:
        # Treat as a prompt template — send directly to the model
        model = cfg.get(skill.model_key, "llama3.3")
        return llm_mod.chat(src, model=model, base_url=base_url)
# skills.py:1 ends here
