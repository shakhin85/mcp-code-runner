"""SHA-524: allow_side_effects as a per-run call budget, and a gate refusal
repeated by fingerprint within a session."""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import REPEAT_PREFIX, CodeExecutor, SideEffectGate


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    isError = False

    def __init__(self):
        self.content = [_Content("ok")]


class _CountingSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return _Result()


def _executor(isolation):
    linear = _CountingSession()

    class FakePool:
        sessions = {"linear": linear}
        tools = {
            "linear": [
                Tool(name="linear_createIssue", inputSchema={"type": "object"}),
                Tool(name="linear_deleteIssue", inputSchema={"type": "object"}),
                Tool(name="linear_getIssues", inputSchema={"type": "object"}),
            ],
        }

    return CodeExecutor(FakePool(), isolation=isolation, mem_limit_mb=1024), linear


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]

CREATE = "await linear.linear_createIssue(title='x')\n"


def _run(ex, code, **kw):
    return asyncio.run(ex.execute(code, timeout=10, **kw))


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_budget_refuses_call_over_n(isolation):
    ex, linear = _executor(isolation)
    result = _run(ex, CREATE * 2, allow_side_effects=["linear.linear_createIssue:1"])
    assert result["success"] is False
    assert "бюджет 1 вызовов за execute_code исчерпан" in result["error"], result["error"]
    assert len(linear.calls) == 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_budget_resets_between_runs_of_one_session(isolation):
    ex, linear = _executor(isolation)
    for _ in range(2):
        result = _run(
            ex, CREATE, allow_side_effects=["linear.linear_createIssue:1"],
            session_id="sha524-reset",
        )
        assert result["success"] is True, result["error"]
    assert len(linear.calls) == 2


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_server_wildcard_budget_is_shared(isolation):
    ex, linear = _executor(isolation)
    code = (
        "await linear.linear_getIssues()\n"
        "await linear.linear_createIssue(title='x')\n"
        "await linear.linear_deleteIssue(id='1')\n"
        "await linear.linear_createIssue(title='y')\n"
    )
    result = _run(ex, code, allow_side_effects=["linear.*:2"])
    assert result["success"] is False
    assert "linear.*: бюджет 2" in result["error"], result["error"]
    # Reads don't spend the budget: getIssues + two writes went through.
    assert [name for name, _ in linear.calls] == [
        "linear_getIssues", "linear_createIssue", "linear_deleteIssue",
    ]


def test_bad_budget_entry_is_rejected():
    for entry in ["linear.linear_createIssue:0", "linear.*:many", "linear.x:²"]:
        with pytest.raises(ValueError, match="целое > 0"):
            SideEffectGate([entry])


def test_colon_in_server_name_is_not_a_budget():
    allowed, budget = SideEffectGate.parse(["plugin:x:y.create", "plugin:x:y.*:3"])
    assert allowed == {"plugin:x:y.create", "plugin:x:y.*"}
    assert budget == {"plugin:x:y.*": 3}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_bad_budget_entry_fails_the_run_cleanly(isolation):
    ex, linear = _executor(isolation)
    result = _run(ex, CREATE, allow_side_effects=["linear.linear_createIssue:0"])
    assert result["success"] is False
    assert "целое > 0" in result["error"]
    assert "isolation error" not in result["error"]
    assert linear.calls == []


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_server_budget_caps_tool_with_own_entry(isolation):
    ex, linear = _executor(isolation)
    result = _run(
        ex, CREATE * 2,
        allow_side_effects=["linear.linear_createIssue", "linear.*:1"],
    )
    assert result["success"] is False
    assert "linear.*: бюджет 1" in result["error"], result["error"]
    assert len(linear.calls) == 1


def test_uncanonical_args_are_not_remembered():
    assert SideEffectGate.fingerprint("s", "t", {1: "a", "b": 2}, (False, None)) is None
    refused = {}
    gate = SideEffectGate(None, refused)
    gate.remember(None, "x")
    gate.check_repeat(None)
    assert refused == {}


def test_refusal_store_is_capped(monkeypatch):
    monkeypatch.setattr("code_runner.executor.MAX_REFUSALS", 3)
    refused = {}
    gate = SideEffectGate(None, refused)
    for i in range(5):
        gate.remember(f"fp{i}", "m" * 5000)
    assert list(refused) == ["fp2", "fp3", "fp4"]
    assert all(len(msg) == 2000 for _, msg in refused.values())


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_refusal_repeated_in_session_is_marked(isolation):
    ex, linear = _executor(isolation)
    first = _run(ex, CREATE, session_id="sha524-repeat")
    assert first["success"] is False
    assert REPEAT_PREFIX not in first["error"]
    second = _run(ex, CREATE, session_id="sha524-repeat")
    assert second["success"] is False
    assert REPEAT_PREFIX in second["error"], second["error"]
    assert 'allow_side_effects=["linear.linear_createIssue"]' in second["error"]
    # Different arguments are a different call: evaluated afresh.
    other = _run(ex, "await linear.linear_createIssue(title='z')", session_id="sha524-repeat")
    assert REPEAT_PREFIX not in other["error"]
    # Changed permissions: evaluated afresh and let through.
    allowed = _run(
        ex, CREATE, session_id="sha524-repeat",
        allow_side_effects=["linear.linear_createIssue"],
    )
    assert allowed["success"] is True, allowed["error"]
    assert len(linear.calls) == 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_no_session_no_repeat_marker(isolation):
    ex, _ = _executor(isolation)
    for _ in range(2):
        result = _run(ex, CREATE)
        assert result["success"] is False
        assert REPEAT_PREFIX not in result["error"]


def test_fingerprint_ignores_kwarg_order_and_tells_calls_apart():
    fp = SideEffectGate.fingerprint
    base = fp("linear", "linear_createIssue", {"a": 1, "b": 2}, (False, None))
    assert base == fp("linear", "linear_createIssue", {"b": 2, "a": 1}, (False, None))
    assert base != fp("linear", "linear_createIssue", {"a": 1, "b": 3}, (False, None))
    assert base != fp("linear", "linear_deleteIssue", {"a": 1, "b": 2}, (False, None))
    # A refusal to user code never blocks the skill-side owner's identical call.
    assert base != fp("linear", "linear_createIssue", {"a": 1, "b": 2}, (True, "x"))
