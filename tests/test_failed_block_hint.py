"""B.3: NameError на переменную, которую не присвоил упавший блок той же сессии.

Упавший блок не сохраняет ни одной своей переменной (сессия откатывается к
последнему успешному состоянию), поэтому следующий блок падает на NameError, а
базовый HINT про похожие имена ведёт по ложному следу. Сессия помнит номер
последнего упавшего блока (failed_block) и строку падения — HINT называет их.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


def _run_blocks(isolation: str, sid: str, *blocks: str) -> list[dict]:
    class EmptyPool:
        sessions: dict = {}
        tools: dict = {}

    executor = CodeExecutor(pool=EmptyPool(), isolation=isolation)

    async def scenario() -> list[dict]:
        return [await executor.execute(code, session_id=sid) for code in blocks]

    return asyncio.run(scenario())


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_failed_block_hint_names_block_and_line(isolation):
    ok, failed, res = _run_blocks(
        isolation, "failed_block-hint",
        "a = 1",
        "rows = [1, 2]\nboom = 1 / 0\n",
        "print(len(rows))",
    )
    assert ok["success"], ok["error"]
    assert not failed["success"]
    assert not res["success"]
    assert "NameError" in res["error"]
    assert "блок 2 упал на строке 2, переменная rows не присвоена" in res["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_failed_block_hint_silent_for_unrelated_name(isolation):
    _failed, res = _run_blocks(
        isolation, "failed_block-unrelated",
        "x = 1\nboom = 1 / 0\n",
        "print(never_assigned)",
    )
    assert "NameError" in res["error"]
    assert "упал на строке" not in res["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_failed_block_hint_ignores_function_locals(isolation):
    _failed, local, func = _run_blocks(
        isolation, "failed_block-locals",
        "def f():\n    inner = 1\n    return inner\nboom = 1 / 0\n",
        "print(inner)",
        "print(f())",
    )
    assert "NameError" in local["error"]
    assert "упал на строке" not in local["error"]
    assert "блок 1 упал на строке 4, переменная f не присвоена" in func["error"]
