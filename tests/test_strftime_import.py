"""C-level stdlib imports must work under the restricted __builtins__ (SHA-490).

datetime.strftime imports `time` (and strptime imports `_strptime`) from C
through the caller's __builtins__["__import__"]. The sandbox had none, so a
plain `.strftime()` raised KeyError: '__import__'. User-level `import` must
stay refused.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor


class _Pool:
    sessions: dict = {}
    tools: dict = {}


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


def _run(isolation, code):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    return asyncio.run(ex.execute(code, timeout=5))


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_strftime_and_strptime_work(isolation):
    result = _run(
        isolation,
        "print(datetime.date(2026, 1, 2).strftime('%Y-%m-%d'))\n"
        "print(datetime.datetime.strptime('2026-01-02', '%Y-%m-%d').year)\n",
    )
    assert result["success"] is True, result["error"]
    assert result["output"].split() == ["2026-01-02", "2026"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_user_import_still_refused(isolation):
    result = _run(isolation, "import os\nprint(os.getcwd())\n")
    assert result["success"] is False
    assert "os" in result["error"]
    result = _run(isolation, "print(__import__('time'))\n")
    assert result["success"] is False
