import ast

import pytest

from code_runner.executor import _rewrite_noop_imports, validate_code
from code_runner.metrics import caller_of, summarize_errors


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import skills.forgetful_compact", "skills = skills"),
        ("import skills.forgetful_compact as fc", "fc = skills.forgetful_compact"),
        ("from skills.forgetful_compact import query", "query = skills.forgetful_compact.query"),
    ],
)
def test_dotted_skills_import_is_noop(code, expected):
    rewritten = _rewrite_noop_imports(code)
    validate_code(rewritten)
    assert rewritten.strip() == expected


@pytest.mark.parametrize(
    "code",
    [
        "import skills._private",
        "from skills.a import _x",
        "from skills.a import *",
        "import skills.a, os",
    ],
)
def test_dotted_skills_forms_still_blocked(code):
    with pytest.raises(ValueError, match="import statements are not allowed"):
        validate_code(_rewrite_noop_imports(code))


@pytest.mark.parametrize(
    "mod",
    [
        "os",
        "sys",
        "subprocess",
        "socket",
        "importlib",
        "inspect",
        "ctypes",
        "pickle",
        "builtins",
        "io",
        "pathlib",
        "shutil",
        "glob",
    ],
)
def test_dangerous_modules_blocked(mod):
    for code in (f"import {mod}", f"from {mod} import x"):
        with pytest.raises(ValueError, match="import statements are not allowed"):
            validate_code(_rewrite_noop_imports(code))


def test_fs_modules_hint_points_to_open():
    with pytest.raises(ValueError, match=r"open\(\)"):
        validate_code("import pathlib")


def test_caller_of():
    assert caller_of("main-topic-x") == "main"
    assert caller_of("haiku-scan") == "haiku"
    assert caller_of("plain") == "plain"
    assert caller_of(None) == "unknown"


def test_summary_by_caller():
    events = [
        {"kind": "execute_code", "success": True, "session_id": "main-a"},
        {"kind": "execute_code", "success": False, "session_id": "main-a", "error": "boom"},
        {"kind": "execute_code", "success": False, "session_id": "haiku-b", "caller": "haiku"},
        {"kind": "execute_code", "success": True, "session_id": None},
    ]
    got = summarize_errors(events)["by_caller"]
    assert got["main"] == {"runs": 2, "failed": 1, "fail_rate": 0.5}
    assert got["haiku"] == {"runs": 1, "failed": 1, "fail_rate": 1.0}
    assert got["unknown"]["runs"] == 1


@pytest.mark.parametrize(
    "code",
    [
        "from skills.forgetful_compact import gi_frame",
        "from skills.forgetful_compact import f_globals",
        "from json import gi_frame",
        "import skills.forgetful_compact.gi_frame",
        "import skills.forgetful_compact.query",
    ],
)
def test_noop_import_refuses_names_attr_rule_refuses(code: str) -> None:
    from code_runner.executor import _is_noop_import

    assert not _is_noop_import(ast.parse(code).body[0])
