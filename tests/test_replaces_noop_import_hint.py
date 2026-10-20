"""Три класса падений execute_code, закрытые в источнике:
1. `replaces:` во frontmatter SKILL.md -> HINT на skill в PermissionError гейта;
2. `import json` / `from re import sub` -> no-op (AST-переписывание в присваивание);
3. NameError на dir/globals/locals/vars -> HINT, что использовать вместо них.
"""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor
from code_runner.skills import SkillLoader, SkillsNamespace

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


class _Content:
    text = "ok"


class _Result:
    isError = False
    content = [_Content()]


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return _Result()


def _pool():
    session = _Session()

    class FakePool:
        sessions = {"srv": session}
        tools = {
            "srv": [
                Tool(name="create_thing", inputSchema={"type": "object"}),
                Tool(name="delete_thing", inputSchema={"type": "object"}),
            ]
        }

    return FakePool(), session


def _skill(root, name, replaces=None):
    d = root / name
    d.mkdir()
    (d / "script.py").write_text(
        "from pathlib import Path\n\nasync def thing(**kw):\n    return 1\n",
        encoding="utf-8",
    )
    front = f"replaces: [{replaces}]\n" if replaces else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{front}---\n", encoding="utf-8"
    )


def _run(isolation, code, skills=None, **kw):
    pool, session = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024, skills=skills)
    return asyncio.run(ex.execute(code, timeout=5, **kw)), session


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_replaces_frontmatter_adds_skill_hint_to_gate_error(isolation, tmp_path):
    _skill(tmp_path, "thing_skill", replaces="srv.create_thing")
    _skill(tmp_path, "other_skill")
    skills = SkillsNamespace(SkillLoader(tmp_path).discover())
    result, session = _run(isolation, "await srv.create_thing(a=1)", skills)
    err = result["error"]
    assert "PermissionError" in err, err
    first = next(line for line in err.splitlines() if "skills.thing_skill.thing" in line)
    second = next(line for line in err.splitlines() if "allow_side_effects" in line)
    assert err.index(first) < err.index(second), err
    assert session.calls == []


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_gate_error_without_replaces_has_no_skill_hint(isolation, tmp_path):
    _skill(tmp_path, "thing_skill", replaces="srv.create_thing")
    skills = SkillsNamespace(SkillLoader(tmp_path).discover())
    result, _ = _run(isolation, "await srv.delete_thing(a=1)", skills)
    assert "PermissionError" in result["error"], result["error"]
    assert "skills." not in result["error"], result["error"]
    assert 'allow_side_effects=["srv.delete_thing"]' in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_import_of_preloaded_module_is_noop(isolation):
    code = (
        "import json\nimport re as rx\nfrom re import sub, compile as comp\n"
        "print(json.dumps({}), rx.sub('a', 'b', 'a'), sub('x', 'y', 'x'), "
        "comp('z').pattern)"
    )
    result, _ = _run(isolation, code)
    assert result["success"], result["error"]
    assert result["output"].strip() == "{} b y z"


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import subprocess",
        "import json, os",
        "from json import __loader__",
        "from json import _default_encoder",
        "from re import *",
        "import os.path",
        "import json.decoder",
        "from . import json",
    ],
)
def test_other_imports_still_blocked(isolation, code):
    result, _ = _run(isolation, code)
    assert result["success"] is False
    assert "import statements are not allowed" in result["error"], result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_noop_import_keeps_module_wrapper(isolation):
    result, _ = _run(isolation, "import uuid\nuuid.os")
    assert result["success"] is False
    assert "module" in result["error"].lower() or "AttributeError" in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("name", ["dir", "globals", "locals", "vars"])
def test_introspection_names_still_undefined_but_hinted(isolation, name):
    result, _ = _run(isolation, f"{name}()")
    err = result["error"]
    assert f"NameError: name '{name}' is not defined" in err, err
    assert "HINT" in err and "list_available_tools" in err, err
    assert "sandbox_introspect" in err, err
