# [[file:../../../org/20260425230731-org_llm.org::*test_skills.py][test_skills.py:1]]
from __future__ import annotations

import pytest
from pathlib import Path

from org_llm.skills import Skill, _iter_skill_blocks, index_skills, run_skill


class TestIterSkillBlocks:
    def test_finds_python_skill(self, skill_file):
        blocks = list(_iter_skill_blocks(skill_file))
        assert len(blocks) == 1
        name, lang, model_key, source, heading = blocks[0]
        assert name == "echo_test"
        assert lang == "python"
        assert "{{input}}" in source

    def test_finds_shell_skill(self, shell_skill_file):
        blocks = list(_iter_skill_blocks(shell_skill_file))
        assert blocks[0][1] == "sh"

    def test_ignores_untagged_headings(self, tmp_path):
        p = tmp_path / "plain.org"
        p.write_text("* Normal heading\n\n#+begin_src python\nprint('hi')\n#+end_src\n")
        assert list(_iter_skill_blocks(p)) == []

    def test_default_name_from_heading(self, tmp_path):
        p = tmp_path / "noname.org"
        p.write_text(
            "* My Cool Skill                        :skill:\n\n"
            "#+begin_src python\npass\n#+end_src\n"
        )
        blocks = list(_iter_skill_blocks(p))
        assert blocks[0][0] == "my_cool_skill"


class TestIndexSkills:
    def test_registers_skill(self, skill_file, session):
        count = index_skills(session, skill_file.parent)
        assert count == 1
        sk = session.query(Skill).filter_by(name="echo_test").first()
        assert sk is not None
        assert sk.lang == "python"

    def test_upserts_on_rerun(self, tmp_path, session):
        p = tmp_path / "upd.org"
        p.write_text(
            "* Upd                                  :skill:\n"
            ":PROPERTIES:\n:SKILL_NAME: upd\n:END:\n\n"
            "#+begin_src python\nprint('v1')\n#+end_src\n"
        )
        index_skills(session, tmp_path)
        p.write_text(
            "* Upd                                  :skill:\n"
            ":PROPERTIES:\n:SKILL_NAME: upd\n:END:\n\n"
            "#+begin_src python\nprint('v2')\n#+end_src\n"
        )
        index_skills(session, tmp_path)
        assert session.query(Skill).count() == 1
        assert "v2" in session.query(Skill).filter_by(name="upd").first().source


class TestRunSkill:
    def _skill(self, **kw):
        defaults = dict(name="t", lang="python", model_key="text_model",
                        source="", file_path="/tmp/t.org", heading="T")
        defaults.update(kw)
        return Skill(**defaults)

    def test_python_echo(self):
        sk = self._skill(source="print('{{input}}')")
        out = run_skill(sk, input_text="solidarity ✊", cfg={}, base_url="")
        assert "solidarity" in out

    def test_python_cfg_substitution(self):
        sk = self._skill(source="print('{{cfg.greeting}}')")
        out = run_skill(sk, input_text="", cfg={"greeting": "hello comrade"}, base_url="")
        assert "hello comrade" in out

    def test_shell_execution(self):
        sk = self._skill(lang="sh", source="echo 'trans rights now'")
        out = run_skill(sk, input_text="", cfg={}, base_url="")
        assert "trans rights now" in out

    def test_shell_input_substitution(self):
        sk = self._skill(lang="sh", source="echo '{{input}}'")
        out = run_skill(sk, input_text="queer", cfg={}, base_url="")
        assert "queer" in out
# test_skills.py:1 ends here
