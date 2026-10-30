
import pytest

from code_runner.skills import (
    SkillLoader,
    SkillsNamespace,
    validate_skill_name,
    write_skill_files,
)


def test_validate_skill_name_accepts_good():
    validate_skill_name("foo")
    validate_skill_name("foo_bar_2")
    validate_skill_name("a")


def test_validate_skill_name_rejects_bad():
    bad_names = [
        "",
        "Foo",       # uppercase
        "1foo",      # leading digit
        "foo-bar",   # hyphen
        "foo bar",   # space
        "x" * 41,    # too long
        "foo.bar",   # dot
        "_foo",      # leading underscore
    ]
    for bad in bad_names:
        with pytest.raises(ValueError):
            validate_skill_name(bad)


def test_write_skill_files_creates_both(tmp_path):
    target = write_skill_files(tmp_path, "demo", "def f(): return 1", "demo skill")
    assert (target / "script.py").read_text() == "def f(): return 1"
    md = (target / "SKILL.md").read_text()
    assert "name: demo" in md
    assert "description: demo skill" in md
    assert md.startswith("---\n") and md.rstrip().endswith("---")


def test_write_then_discover_then_call(tmp_path):
    write_skill_files(tmp_path, "demo", "def f(): return 42", "the answer")
    specs = SkillLoader(tmp_path).discover()
    assert "demo" in specs
    ns = SkillsNamespace(specs)
    assert ns.demo.f() == 42


def test_overwrite_existing_skill(tmp_path):
    write_skill_files(tmp_path, "demo", "def f(): return 1", "v1")
    write_skill_files(tmp_path, "demo", "def f(): return 2", "v2")
    ns = SkillsNamespace(SkillLoader(tmp_path).discover())
    assert ns.demo.f() == 2


def test_write_skill_files_rejects_invalid_syntax(tmp_path):
    with pytest.raises(ValueError, match="invalid Python syntax"):
        write_skill_files(tmp_path, "bad", "def f(:\n    pass\n", "broken")
    # Nothing should be on disk
    assert not (tmp_path / "bad").exists()


def test_write_skill_files_rejects_invalid_syntax_before_writing(tmp_path):
    # Make sure we don't half-create the dir before failing.
    write_skill_files(tmp_path, "good", "def f(): return 1", "good skill")
    assert (tmp_path / "good").is_dir()
    with pytest.raises(ValueError, match="invalid Python syntax"):
        write_skill_files(tmp_path, "good", "this is not python!!!", "broken update")
    # Existing skill is unchanged
    assert (tmp_path / "good" / "script.py").read_text() == "def f(): return 1"


# --- save_skill(example=): the example runs against the new version first ---

import asyncio  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from code_runner import server as server_mod  # noqa: E402
from code_runner.executor import CodeExecutor  # noqa: E402

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


class _Pool:
    sessions = {}
    tools = {}


def _ctx(tmp_path, monkeypatch, isolation):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(server_mod, "SKILLS_DIR", skills_dir)
    executor = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context={
        "executor": executor, "skills_loader": SkillLoader(skills_dir),
    }))
    return ctx, skills_dir, executor


def _save(ctx, name, code, example):
    return asyncio.run(server_mod.save_skill(name, code, "d", ctx, example=example))


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_example_passes_skill_saved_with_example(tmp_path, monkeypatch, isolation):
    ctx, skills_dir, executor = _ctx(tmp_path, monkeypatch, isolation)
    msg = _save(ctx, "calc", "def inc(x):\n    return x + 1\n",
                "print(skills.calc.inc(41))")
    assert "example ok" in msg and "42" in msg
    assert "skills.calc.inc(41)" in (skills_dir / "calc" / "SKILL.md").read_text()
    out = asyncio.run(executor.execute("print(skills.calc.inc(1))", timeout=5))
    assert out["success"] and out["output"].strip() == "2"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_example_fails_skill_not_saved(tmp_path, monkeypatch, isolation):
    ctx, skills_dir, _ = _ctx(tmp_path, monkeypatch, isolation)
    with pytest.raises(ValueError, match="not saved: example failed"):
        _save(ctx, "calc", "def inc(x):\n    return x - 1\n",
              "assert skills.calc.inc(41) == 42")
    assert not (skills_dir / "calc").exists()


def test_failed_update_keeps_old_version_live(tmp_path, monkeypatch):
    ctx, skills_dir, executor = _ctx(tmp_path, monkeypatch, "inprocess")
    _save(ctx, "calc", "def inc(x):\n    return x + 1\n", "skills.calc.inc(1)")
    with pytest.raises(ValueError):
        _save(ctx, "calc", "def inc(x):\n    raise RuntimeError('v2')\n",
              "skills.calc.inc(1)")
    assert "x + 1" in (skills_dir / "calc" / "script.py").read_text()
    out = asyncio.run(executor.execute("print(skills.calc.inc(1))", timeout=5))
    assert out["output"].strip() == "2"


def test_example_sees_other_skills(tmp_path, monkeypatch):
    ctx, _, _ = _ctx(tmp_path, monkeypatch, "inprocess")
    _save(ctx, "base", "def one():\n    return 1\n", None)
    msg = _save(ctx, "calc", "def two():\n    return 2\n",
                "print(skills.base.one() + skills.calc.two())")
    assert "3" in msg
