"""Frame/code introspection attributes must be refused (SHA-491).

They carry no underscore, so neither the dunder nor the private rule saw them:
`gen.gi_frame.f_back...f_globals` walked from user code to module globals
holding `os` and `builtins` in both isolation modes.
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

_GEN = "def g():\n    yield 1\ngen = g()\n"

ESCAPES = {
    "generator_frame_walk": _GEN + "print(gen.gi_frame.f_back)\n",
    "frame_globals": _GEN + "fr = gen.gi_frame\nprint(fr.f_globals)\n",
    "code_object": _GEN + "print(gen.gi_code)\n",
    "coroutine_frame": "async def c():\n    pass\nco = c()\nprint(co.cr_frame)\n",
    "class_pattern": _GEN + "match gen:\n    case object(gi_frame=f):\n        print(f)\n",
    "format_field": _GEN + "print('{0.gi_frame.f_globals}'.format(gen))\n",
    "formatter_field": _GEN + "print(string.Formatter().format('{0.gi_frame}', gen))\n",
}


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_frame_attr_refused(isolation, name):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(ESCAPES[name], timeout=5))
    assert result["success"] is False, result["output"]
    assert "frame" in result["error"] or "code" in result["error"], result["error"]
