"""SHA-499: сырой forgetful.create_memory/update_memory без allow_side_effects
получает HINT на skills.forgetful_*.memory — в обоих режимах isolation."""

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

SKILLS = {
    "forgetful_create": "forgetful.create_memory:memory",
    "forgetful_update": "forgetful.update_memory:memory",
}


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
        sessions = {"forgetful": session}
        tools = {
            "forgetful": [
                Tool(name=n, inputSchema={"type": "object"})
                for n in ("create_memory", "update_memory", "query_memory")
            ]
        }

    return FakePool(), session


def _write_skills(root):
    for name, replaces in SKILLS.items():
        d = root / name
        d.mkdir()
        (d / "script.py").write_text("async def memory(**kw):\n    return 1\n", encoding="utf-8")
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\nreplaces: [{replaces}]\n---\n",
            encoding="utf-8",
        )


def _run(isolation, root, code, **kw):
    pool, session = _pool()
    skills = SkillsNamespace(SkillLoader(root).discover())
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024, skills=skills)
    return asyncio.run(ex.execute(code, timeout=5, **kw)), session


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize(
    ("tool", "skill"),
    [("create_memory", "forgetful_create"), ("update_memory", "forgetful_update")],
)
def test_raw_forgetful_write_gets_skill_hint(isolation, tool, skill, tmp_path):
    _write_skills(tmp_path)
    result, session = _run(isolation, tmp_path, f"await forgetful.{tool}(title='x')")
    err = result["error"]
    assert "PermissionError" in err, err
    assert f"используй skills.{skill}.memory(...)" in err, err
    assert err.index("используй") < err.index("allow_side_effects"), err
    assert session.calls == []


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_raw_forgetful_write_passes_with_allow_side_effects(isolation, tmp_path):
    _write_skills(tmp_path)
    result, session = _run(
        isolation,
        tmp_path,
        "await forgetful.create_memory(title='x')",
        allow_side_effects=["forgetful.create_memory"],
    )
    assert result["success"], result["error"]
    assert session.calls == [("create_memory", {"title": "x"})]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_tool_without_replaces_keeps_plain_error(isolation, tmp_path):
    _write_skills(tmp_path)
    pool, _ = _pool()
    pool.tools["forgetful"].append(Tool(name="delete_memory", inputSchema={"type": "object"}))
    skills = SkillsNamespace(SkillLoader(tmp_path).discover())
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024, skills=skills)
    result = asyncio.run(ex.execute("await forgetful.delete_memory(id=1)", timeout=5))
    err = result["error"]
    assert "skills." not in err, err
    assert 'allow_side_effects=["forgetful.delete_memory"]' in err
