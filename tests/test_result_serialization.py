"""Last-expression result with non-JSON types (Decimal/date from SQL rows) must render, not fail."""

import asyncio

import pytest

from code_runner.executor import CodeExecutor
from code_runner.workspace import WorkspaceManager


class _EmptyPool:
    sessions: dict = {}
    tools: dict = {}


CODE = (
    "import decimal\n"
    "import datetime\n"
    "[{'amount': decimal.Decimal('1.50'), 'day': datetime.date(2026, 1, 2)}]"
)


@pytest.mark.parametrize("isolation", ["inprocess", "subprocess"])
def test_result_with_decimal_and_date_renders(tmp_path, isolation):
    ex = CodeExecutor(_EmptyPool(), workspace=WorkspaceManager(tmp_path), isolation=isolation)
    result = asyncio.run(ex.execute(CODE))
    assert result["success"] is True, result["error"]
    assert '"amount": "1.50"' in result["output"]
    assert '"day": "2026-01-02"' in result["output"]
