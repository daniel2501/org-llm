"""Tests for org_llm.literate_mcp — Phase 23.6 — literate MCP tools.

Covers:
  • Parser: heading + drawer + babel block extraction; CSV / JSON /
    bool property handling; missing-file robustness; tag-stripping
    on the heading title.
  • Compiler: safe-builtins surface; import allowlist gate; dangerous
    flag two-key rule; syntax-error handling; signature derivation
    from :PARAMS:; output of a real word_count tool.
  • Public API: ``load_literate_tools`` round-trip from a real file.

No MCP-server contact — Phase 24.2 owns mcp_server.py concurrently.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from org_llm.literate_mcp import (
    compile_all,
    compile_tool,
    load_literate_tools,
    parse_file,
    parse_text,
)


# ── Sample tools ─────────────────────────────────────────────────────


WORD_COUNT_ORG = """\
* word_count
:PROPERTIES:
:DESCRIPTION: Count words in a string
:PARAMS:      {"text": {"type": "string"}}
:RETURNS:     {"type": "integer"}
:END:

#+begin_src python :tangle no
return len(text.split())
#+end_src
"""


SUM_RANGE_ORG = """\
* sum_range
:PROPERTIES:
:DESCRIPTION: Sum integers from 1..n inclusive
:PARAMS:      {"n": {"type": "integer"}}
:END:

#+begin_src python :tangle no
total = 0
for i in range(1, n + 1):
    total += i
return total
#+end_src
"""


JSON_TOOL_ORG = """\
* parse_json_keys
:PROPERTIES:
:DESCRIPTION: Return sorted top-level keys of a JSON object string
:PARAMS:      {"raw": {"type": "string"}}
:IMPORTS:     json
:END:

#+begin_src python :tangle no
data = json.loads(raw)
return sorted(data.keys())
#+end_src
"""


DANGEROUS_OS_TOOL_ORG = """\
* read_dir
:PROPERTIES:
:DESCRIPTION: List dir entries (dangerous; needs os import).
:PARAMS:      {"path": {"type": "string"}}
:IMPORTS:     os
:DANGEROUS:   yes
:END:

#+begin_src python :tangle no
return sorted(os.listdir(path))
#+end_src
"""


SYNTAX_ERROR_ORG = """\
* broken
:PROPERTIES:
:DESCRIPTION: This won't compile
:PARAMS:      {}
:END:

#+begin_src python :tangle no
return 1 +
#+end_src
"""


# ── Parser tests ─────────────────────────────────────────────────────


def test_parse_word_count_basic():
    [tool] = parse_text(WORD_COUNT_ORG)
    assert tool.name == "word_count"
    assert tool.description == "Count words in a string"
    assert tool.params == {"text": {"type": "string"}}
    assert tool.returns == {"type": "integer"}
    assert tool.dangerous is False
    assert tool.imports == ()
    assert "return len(text.split())" in tool.body


def test_parse_multiple_tools():
    text = WORD_COUNT_ORG + "\n" + SUM_RANGE_ORG
    parsed = parse_text(text)
    names = [p.name for p in parsed]
    assert names == ["word_count", "sum_range"]


def test_parse_imports_csv_and_dangerous_truthy():
    [tool] = parse_text(DANGEROUS_OS_TOOL_ORG)
    assert tool.imports == ("os",)
    assert tool.dangerous is True


def test_parse_drops_org_tags_from_heading():
    text = (
        "* my_tool                                              :tool:\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: yo\n"
        ":PARAMS:      {}\n"
        ":END:\n\n"
        "#+begin_src python :tangle no\n"
        "return 1\n"
        "#+end_src\n"
    )
    [tool] = parse_text(text)
    assert tool.name == "my_tool"


def test_parse_skips_heading_without_python_block():
    text = (
        "* not_a_tool\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: just a section header\n"
        ":END:\n\n"
        "Some prose, no src block.\n"
    )
    assert parse_text(text) == []


def test_parse_skips_invalid_python_identifier_heading():
    text = (
        "* 123-bad-name\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: ignored\n"
        ":END:\n\n"
        "#+begin_src python :tangle no\n"
        "return 1\n"
        "#+end_src\n"
    )
    assert parse_text(text) == []


def test_parse_malformed_params_json_softfails():
    text = (
        "* tool_with_bad_json\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: oops\n"
        ":PARAMS:      {not valid json,\n"
        ":END:\n\n"
        "#+begin_src python :tangle no\n"
        "return 1\n"
        "#+end_src\n"
    )
    [tool] = parse_text(text)
    assert tool.params == {}
    assert tool.name == "tool_with_bad_json"


def test_parse_file_missing_path_returns_empty(tmp_path: Path):
    assert parse_file(tmp_path / "does-not-exist.org") == []


def test_parse_file_round_trip(tmp_path: Path):
    p = tmp_path / "tools.org"
    p.write_text(WORD_COUNT_ORG)
    [tool] = parse_file(p)
    assert tool.name == "word_count"


def test_parse_timeout_property():
    text = (
        "* slow_tool\n"
        ":PROPERTIES:\n"
        ":DESCRIPTION: takes a while\n"
        ":TIMEOUT:     12.5\n"
        ":END:\n\n"
        "#+begin_src python :tangle no\n"
        "return 'hi'\n"
        "#+end_src\n"
    )
    [tool] = parse_text(text)
    assert tool.timeout == 12.5


# ── Compiler tests ───────────────────────────────────────────────────


def test_compile_word_count_runs():
    [parsed] = parse_text(WORD_COUNT_ORG)
    tool, err = compile_tool(parsed)
    assert err is None
    assert tool is not None
    assert tool.callable("hello world from org") == 4
    assert tool.callable("") == 0


def test_compile_sum_range_with_locals():
    [parsed] = parse_text(SUM_RANGE_ORG)
    tool, err = compile_tool(parsed)
    assert err is None
    assert tool.callable(5) == 15
    assert tool.callable(0) == 0


def test_compile_with_safe_import_json():
    [parsed] = parse_text(JSON_TOOL_ORG)
    tool, err = compile_tool(parsed)
    assert err is None
    assert tool.callable('{"b": 1, "a": 2}') == ["a", "b"]


def test_compile_rejects_unsafe_import_by_default():
    [parsed] = parse_text(DANGEROUS_OS_TOOL_ORG)
    tool, err = compile_tool(parsed, dangerous_enabled=False)
    assert tool is None
    assert err is not None
    assert err.tool == "read_dir"
    # marked dangerous → host hasn't enabled dangerous tools.
    assert "dangerous" in err.reason.lower()


def test_compile_allows_unsafe_import_with_two_keys(tmp_path: Path):
    [parsed] = parse_text(DANGEROUS_OS_TOOL_ORG)
    tool, err = compile_tool(parsed, dangerous_enabled=True)
    assert err is None, err
    # Smoke-run against the temp dir.
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    out = tool.callable(str(tmp_path))
    assert out == ["a.txt", "b.txt"]


def test_compile_unsafe_import_without_dangerous_flag_rejected():
    """Even with dangerous_enabled=True at the host level, a tool that
    doesn't *itself* flip :DANGEROUS: yes can't pull arbitrary modules."""
    text = textwrap.dedent("""\
        * sneaky
        :PROPERTIES:
        :DESCRIPTION: missing dangerous flag
        :IMPORTS:     os
        :END:

        #+begin_src python :tangle no
        return os.getcwd()
        #+end_src
    """)
    [parsed] = parse_text(text)
    tool, err = compile_tool(parsed, dangerous_enabled=True)
    assert tool is None
    assert err is not None
    assert "allowlist" in err.reason or "DANGEROUS" in err.reason


def test_compile_syntax_error_returns_compile_error():
    [parsed] = parse_text(SYNTAX_ERROR_ORG)
    tool, err = compile_tool(parsed)
    assert tool is None
    assert err is not None
    assert "syntax" in err.reason.lower()


def test_compile_no_open_in_safe_builtins():
    """The sandbox refuses ``open(...)`` even on a tool with no
    declared imports."""
    text = textwrap.dedent("""\
        * try_open
        :PROPERTIES:
        :DESCRIPTION: should fail at runtime
        :PARAMS:      {"path": {"type": "string"}}
        :END:

        #+begin_src python :tangle no
        return open(path).read()
        #+end_src
    """)
    [parsed] = parse_text(text)
    tool, err = compile_tool(parsed)
    assert err is None  # compiles fine (NameError happens at call time)
    with pytest.raises(NameError):
        tool.callable("/etc/passwd")


def test_compile_no_dunder_import_in_safe_builtins():
    text = textwrap.dedent("""\
        * try_dunder_import
        :PROPERTIES:
        :DESCRIPTION: should fail at runtime
        :END:

        #+begin_src python :tangle no
        m = __import__('os')
        return m.getcwd()
        #+end_src
    """)
    [parsed] = parse_text(text)
    tool, err = compile_tool(parsed)
    assert err is None
    with pytest.raises(NameError):
        tool.callable()


def test_compile_all_partitions_ok_and_bad():
    text = WORD_COUNT_ORG + "\n" + SYNTAX_ERROR_ORG
    parsed = parse_text(text)
    ok, bad = compile_all(parsed)
    assert [t.name for t in ok] == ["word_count"]
    assert [e.tool for e in bad] == ["broken"]


def test_compile_callable_carries_docstring():
    [parsed] = parse_text(WORD_COUNT_ORG)
    tool, _ = compile_tool(parsed)
    assert tool.callable.__doc__ == "Count words in a string"
    assert tool.callable.__name__ == "word_count"


# ── Public API tests ─────────────────────────────────────────────────


def test_load_literate_tools_from_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "org-llm-tools.org"
    p.write_text(WORD_COUNT_ORG + "\n" + SUM_RANGE_ORG)
    tools = load_literate_tools(p)
    names = [t.name for t in tools]
    assert names == ["word_count", "sum_range"]
    by_name = {t.name: t for t in tools}
    assert by_name["word_count"].callable("a b c") == 3
    assert by_name["sum_range"].callable(4) == 10


def test_load_literate_tools_env_path(tmp_path: Path, monkeypatch):
    p = tmp_path / "user-tools.org"
    p.write_text(WORD_COUNT_ORG)
    monkeypatch.setenv("ORG_LLM_LITERATE_TOOLS_PATH", str(p))
    tools = load_literate_tools()
    assert [t.name for t in tools] == ["word_count"]


def test_load_literate_tools_skips_dangerous_by_default(tmp_path: Path):
    p = tmp_path / "tools.org"
    p.write_text(WORD_COUNT_ORG + "\n" + DANGEROUS_OS_TOOL_ORG)
    tools = load_literate_tools(p, dangerous_enabled=False)
    assert [t.name for t in tools] == ["word_count"]


def test_load_literate_tools_includes_dangerous_when_enabled(tmp_path: Path):
    p = tmp_path / "tools.org"
    p.write_text(WORD_COUNT_ORG + "\n" + DANGEROUS_OS_TOOL_ORG)
    tools = load_literate_tools(p, dangerous_enabled=True)
    names = sorted(t.name for t in tools)
    assert names == ["read_dir", "word_count"]
