import asyncio
import sys
import time

import pytest

from code_runner.executor import (
    MAX_ERROR_DETAIL,
    CodeExecutor,
    MCPToolError,
    _append_cost_footer,
    _limit_error_hint,
    _name_error_hint,
    _signature_hint,
    _ToolNamespace,
    validate_code,
)


class _FakeText:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text, is_error=False):
        self.content = [_FakeText(text)]
        self.isError = is_error


class _FakeSession:
    """Test double: records the last call and returns a canned text payload."""
    def __init__(self, payload_text, is_error=False):
        self._payload = payload_text
        self._is_error = is_error
        self.last_call = None

    async def call_tool(self, name, kwargs):
        self.last_call = (name, kwargs)
        return _FakeResult(self._payload, is_error=self._is_error)


class _FakeTool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _RecordingRecorder:
    """Captures metric events so tests can assert on success/kind."""
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class TestValidateCode:
    def test_allows_normal_code(self):
        validate_code("x = 1 + 2\nprint(x)")

    def test_allows_await(self):
        validate_code("result = await foo.bar(x=1)")

    def test_rejects_import(self):
        with pytest.raises(ValueError, match="import"):
            validate_code("import os")

    def test_rejects_from_import(self):
        with pytest.raises(ValueError, match="import"):
            validate_code("from os import path")

    def test_rejects_dunder_attribute(self):
        with pytest.raises(ValueError, match="__"):
            validate_code("x.__class__")

    def test_rejects_dunder_subclasses(self):
        with pytest.raises(ValueError, match="__"):
            validate_code("x.__subclasses__()")

    def test_allows_normal_attributes(self):
        validate_code("x.name\nx.value")

    def test_syntax_error_raises(self):
        with pytest.raises(ValueError, match="SyntaxError"):
            validate_code("def def def")


class TestSandboxNamespace:
    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_print_works(self, executor):
        result = asyncio.run(executor.execute("print('hello')"))
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_open_blocked(self, executor):
        result = asyncio.run(executor.execute("open('test.txt')"))
        assert result["success"] is False

    def test_import_blocked(self, executor):
        result = asyncio.run(executor.execute("import os"))
        assert result["success"] is False
        assert "import" in result["error"].lower()

    def test_dunder_blocked(self, executor):
        result = asyncio.run(executor.execute("x = ''.__class__"))
        assert result["success"] is False
        assert "__" in result["error"]

    def test_json_available(self, executor):
        result = asyncio.run(executor.execute("print(json.dumps({'a': 1}))"))
        assert result["success"] is True
        assert '{"a": 1}' in result["output"]

    def test_asyncio_sleep_available(self, executor):
        result = asyncio.run(executor.execute("await asyncio.sleep(0)"))
        assert result["success"] is True

    def test_asyncio_subprocess_blocked(self, executor):
        result = asyncio.run(executor.execute(
            "p = await asyncio.create_subprocess_exec('echo', 'hi')"
        ))
        assert result["success"] is False

    def test_re_available(self, executor):
        result = asyncio.run(executor.execute(
            "m = re.match(r'(\\d+)', '42 abc')\nprint(m.group(1))"
        ))
        assert result["success"] is True
        assert "42" in result["output"]

    def test_new_safe_builtins_available(self, executor):
        code = (
            "print(next(iter([7])), divmod(7, 2), hex(255), "
            "list(bytes([65])), callable(len), hash(3) == hash(3))"
        )
        result = asyncio.run(executor.execute(code))
        assert result["success"] is True
        assert "7 (3, 1) 0xff [65] True True" in result["output"]

    def test_new_exceptions_catchable(self, executor):
        # `except NameError` must no longer self-raise "name not defined"
        code = (
            "try:\n    raise StopIteration\n"
            "except StopIteration:\n    print('caught')"
        )
        result = asyncio.run(executor.execute(code))
        assert result["success"] is True
        assert "caught" in result["output"]

    def test_getattr_stays_blocked(self, executor):
        # SECURITY: getattr bypasses the AST dunder guard via runtime strings,
        # so it must remain absent from the sandbox namespace.
        result = asyncio.run(executor.execute("getattr(1, 'real')"))
        assert result["success"] is False

    def test_reflection_builtins_stay_blocked(self, executor):
        for name in ("getattr", "setattr", "hasattr", "vars", "globals", "locals", "dir"):
            result = asyncio.run(executor.execute(f"{name}"))
            assert result["success"] is False, f"{name} must stay blocked"

    def test_datetime_available(self, executor):
        result = asyncio.run(executor.execute(
            "d = datetime.date(2026, 4, 10)\nprint(d.isoformat())"
        ))
        assert result["success"] is True
        assert "2026-04-10" in result["output"]

    def test_decimal_available(self, executor):
        result = asyncio.run(executor.execute(
            "x = decimal.Decimal('1.1') + decimal.Decimal('2.2')\nprint(x)"
        ))
        assert result["success"] is True
        assert "3.3" in result["output"]

    def test_math_available(self, executor):
        result = asyncio.run(executor.execute("print(math.sqrt(16))"))
        assert result["success"] is True
        assert "4.0" in result["output"]

    def test_collections_counter_available(self, executor):
        result = asyncio.run(executor.execute(
            "c = collections.Counter(['a', 'b', 'a'])\nprint(c['a'])"
        ))
        assert result["success"] is True
        assert "2" in result["output"]

    def test_type_builtin_available(self, executor):
        result = asyncio.run(executor.execute("print(type(42).__name__)"))
        assert result["success"] is True
        assert "int" in result["output"]

    def test_exception_name_is_printable(self, executor):
        # Naming the exception is the ordinary way to report a failure; the
        # dunder guard used to reject it and force a rewrite.
        result = asyncio.run(executor.execute(
            "try:\n    1 / 0\nexcept Exception as e:\n    print(type(e).__name__)"
        ))
        assert result["success"] is True
        assert "ZeroDivisionError" in result["output"]

    def test_escape_dunders_still_blocked(self, executor):
        for expr in ("x = ''.__class__", "x = print.__globals__", "x = ().__reduce__"):
            result = asyncio.run(executor.execute(expr))
            assert result["success"] is False, expr
            assert "__" in result["error"]

    def test_type_builtin_simple(self, executor):
        result = asyncio.run(executor.execute("t = type(42)\nprint(t is int)"))
        assert result["success"] is True
        assert "True" in result["output"]


class TestModuleReexportEscape:
    """The AST guard blocks only dunder attributes, but the pure-compute modules
    in SAFE_MODULES re-export *other* modules as ordinary attributes
    (`uuid.os`, `dataclasses.sys`, `random._os`, …). Each is a one-liner to
    `sys.modules` and a full escape. These are the vectors confirmed live during
    the 2026-07-26 audit — they must all be closed by _RestrictedModule.
    """

    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    # (label, code) — each attempts to reach os/sys/socket/builtins via a re-export.
    ESCAPES = [
        ("uuid.os", "print(uuid.os.getpid())"),
        ("uuid.sys", "print(uuid.sys.modules['socket'])"),
        (
            "dataclasses.sys",
            "print(dataclasses.sys.modules['builtins'].open('/etc/hostname').read())",
        ),
        ("statistics.sys", "print(statistics.sys.modules['os'])"),
        ("random._os", "print(random._os.getuid())"),
        ("collections._sys", "print(collections._sys.modules['os'])"),
        ("json.codecs", "print(json.codecs.open)"),
        ("base64.binascii", "print(base64.binascii)"),
    ]

    @pytest.mark.parametrize("label,code", ESCAPES, ids=[e[0] for e in ESCAPES])
    def test_reexport_escape_blocked(self, executor, label, code):
        result = asyncio.run(executor.execute(code))
        assert result["success"] is False, f"{label} escaped the sandbox"
        assert "blocked" in (result["error"] or "").lower(), result["error"]

    def test_no_file_read_via_reexport(self, executor):
        # The most damaging concrete payload from the audit: reading a file far
        # outside the workspace / read-roots. Must produce no file contents.
        result = asyncio.run(executor.execute(
            "data = dataclasses.sys.modules['builtins'].open('/etc/hostname').read()\n"
            "print(data)"
        ))
        assert result["success"] is False

    # Legitimate, non-module attributes must keep working — the wrapper blocks by
    # TYPE (module), so normal library use is untouched.
    LEGIT = [
        ("uuid.uuid4", "print(type(uuid.uuid4()).__name__)", "UUID"),
        ("dataclasses.dataclass", "print(callable(dataclasses.dataclass))", "True"),
        ("collections.OrderedDict", "print(len(collections.OrderedDict([('a', 1)])))", "1"),
        ("json.dumps", "print(json.dumps({'a': 1}))", '{"a": 1}'),
        ("base64.b64encode", "print(base64.b64encode(bytes([65])).decode())", "QQ=="),
        ("random.Random", "print(random.Random(1).randint(5, 5))", "5"),
        ("re.compile", "print(re.compile(r'\\d+').match('42').group())", "42"),
        ("uuid.__name__", "print(uuid.__name__)", "uuid"),
    ]

    @pytest.mark.parametrize("label,code,expected", LEGIT, ids=[e[0] for e in LEGIT])
    def test_legit_module_use_unbroken(self, executor, label, code, expected):
        result = asyncio.run(executor.execute(code))
        assert result["success"] is True, f"{label}: {result['error']}"
        assert expected in result["output"]


class TestErrorTruncation:
    """A raised exception's message is attacker/accident-reachable and was NOT
    capped: `raise ValueError('q' * 300000)` put 300 KB straight into context.
    """

    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_huge_error_is_capped(self, executor):
        from code_runner.executor import MAX_ERROR_BYTES
        result = asyncio.run(executor.execute("raise ValueError('q' * 300000)"))
        assert result["success"] is False
        # Cap + a bounded marker/hint suffix — nowhere near the raw 300 KB.
        assert len(result["error"].encode("utf-8")) < MAX_ERROR_BYTES + 512
        assert "truncated" in result["error"].lower()

    def test_short_error_untouched(self, executor):
        result = asyncio.run(executor.execute("raise ValueError('boom')"))
        assert result["success"] is False
        assert "boom" in result["error"]
        assert "truncated" not in result["error"].lower()


@pytest.mark.skipif(sys.platform == "win32", reason="subprocess isolation is POSIX-only")
class TestSubprocessIsolation:
    """P0.1: each call runs in a fresh, rlimited child process; MCP calls are
    proxied back to the parent. Verifies the broker round-trips, resource limits
    bite, timeouts kill the child, persistence survives, and the module-reexport
    escape stays closed inside the child too."""

    def _executor(self, pool=None):
        class FakePool:
            sessions = {}
            tools = {}
        # Generous mem cap so the child's own imports fit; individual tests that
        # probe the limit allocate well past it.
        return CodeExecutor(
            pool or FakePool(),
            isolation="subprocess",
            mem_limit_mb=1024,
        )

    def test_basic_output_in_child(self):
        ex = self._executor()
        result = asyncio.run(ex.execute("print('from child', 1 + 2)"))
        assert result["success"] is True, result["error"]
        assert "from child 3" in result["output"]

    def test_last_expr_autodisplay(self):
        ex = self._executor()
        result = asyncio.run(ex.execute("sum(range(10))"))
        assert result["success"] is True, result["error"]
        assert "45" in result["output"]

    def test_memory_limit_kills_allocation(self):
        # ~8 GB list under a 1 GB address-space cap → MemoryError in the child,
        # not an OOM that takes the whole server down.
        ex = self._executor()
        result = asyncio.run(ex.execute("x = [0] * (10 ** 9)\nprint(len(x))"))
        assert result["success"] is False
        assert "Error" in (result["error"] or "")

    def test_cpu_loop_is_killed(self):
        ex = self._executor()
        start = time.monotonic()
        result = asyncio.run(ex.execute("while True:\n    pass", timeout=0.5))
        elapsed = time.monotonic() - start
        assert result["success"] is False
        assert "timed out" in (result["error"] or "").lower()
        assert elapsed < 3.0, f"child not killed promptly, took {elapsed:.2f}s"

    def test_reexport_escape_blocked_in_child(self):
        ex = self._executor()
        result = asyncio.run(ex.execute("print(uuid.os.getpid())"))
        assert result["success"] is False
        assert "blocked" in (result["error"] or "").lower()

    def test_proxy_call_round_trips(self):
        # A fake server whose tool returns JSON; the child calls it via the
        # broker and gets back the parsed object.
        class FakePool:
            sessions = {"db": _FakeSession('[{"n": 7}]')}
            tools = {"db": [_FakeTool("echo")]}
        ex = self._executor(FakePool())
        result = asyncio.run(ex.execute("rows = await db.echo(x=1)\nprint(rows[0]['n'])"))
        assert result["success"] is True, result["error"]
        assert "7" in result["output"]
        # Cost footer proves the proxy call was accounted in the parent's stats.
        assert "[cr]" in result["output"]

    def test_proxy_error_surfaces_in_child(self):
        class FakePool:
            sessions = {"db": _FakeSession("boom", is_error=True)}
            tools = {"db": [_FakeTool("echo")]}
        ex = self._executor(FakePool())
        code = (
            "try:\n"
            "    await db.echo()\n"
            "    print('no error')\n"
            "except Exception as e:\n"
            "    print('caught', type(e).__name__)"
        )
        result = asyncio.run(ex.execute(code))
        assert result["success"] is True, result["error"]
        assert "caught MCPToolError" in result["output"]

    def test_gather_over_proxies_serializes_correctly(self):
        # Concurrent proxy calls (asyncio.gather) share one pipe; the child's lock
        # keeps them single-flight. They must not deadlock or cross responses.
        class FakePool:
            sessions = {"db": _FakeSession('{"ok": 1}')}
            tools = {"db": [_FakeTool("echo")]}
        ex = self._executor(FakePool())
        code = (
            "res = await asyncio.gather(db.echo(x=1), db.echo(x=2), db.echo(x=3))\n"
            "print(len(res), res[0]['ok'], res[2]['ok'])"
        )
        result = asyncio.run(ex.execute(code))
        assert result["success"] is True, result["error"]
        assert "3 1 1" in result["output"]

    def test_session_vars_persist_across_calls(self):
        ex = self._executor()
        r1 = asyncio.run(ex.execute("x = 42", session_id="s1"))
        assert r1["success"] is True, r1["error"]
        r2 = asyncio.run(ex.execute("print(x + 1)", session_id="s1"))
        assert r2["success"] is True, r2["error"]
        assert "43" in r2["output"]

    def test_workspace_write_confined(self):
        ex = self._executor()
        code = (
            "with open('note.txt', 'w') as f:\n"
            "    f.write('hi')\n"
            "with open('note.txt') as f:\n"
            "    print(f.read())"
        )
        result = asyncio.run(ex.execute(code, session_id="ws1"))
        assert result["success"] is True, result["error"]
        assert "hi" in result["output"]

    def test_inprocess_still_default(self):
        # The behaviour change is opt-in: a default executor stays in-process.
        class FakePool:
            sessions = {}
            tools = {}
        ex = CodeExecutor(FakePool())
        assert ex._isolation == "inprocess"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM is POSIX-only")
class TestCpuHangProtection:
    """Pure-CPU loops in user code must be interruptible.

    asyncio.wait_for cannot cancel a coroutine that never yields — the event
    loop itself is blocked. A SIGALRM-based hard timeout interrupts user
    bytecode directly and frees the loop.
    """

    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_while_true_loop_times_out(self, executor):
        start = time.monotonic()
        result = asyncio.run(executor.execute(
            "x = 0\nwhile True:\n    x = x + 1",
            timeout=0.5,
        ))
        elapsed = time.monotonic() - start
        assert result["success"] is False
        assert "timed out" in result["error"].lower()
        assert elapsed < 2.5, f"hang not interrupted, took {elapsed:.2f}s"

    def test_except_exception_cannot_swallow_timeout(self, executor):
        """User's `except Exception` must not catch the hard timeout."""
        code = (
            "x = 0\n"
            "while True:\n"
            "    try:\n"
            "        x = x + 1\n"
            "    except Exception:\n"
            "        pass\n"
        )
        start = time.monotonic()
        result = asyncio.run(executor.execute(code, timeout=0.5))
        elapsed = time.monotonic() - start
        assert result["success"] is False
        assert elapsed < 2.5, f"timeout swallowed by except clause, took {elapsed:.2f}s"

    def test_normal_code_unaffected(self, executor):
        """Fast-completing code must not hit the alarm."""
        result = asyncio.run(executor.execute("x = sum(range(1000))\nprint(x)", timeout=5.0))
        assert result["success"] is True
        assert "499500" in result["output"]

    def test_async_sleep_still_uses_wait_for(self, executor):
        """asyncio.sleep should be cancelled by wait_for (not signal)."""
        start = time.monotonic()
        result = asyncio.run(executor.execute("await asyncio.sleep(10)", timeout=0.3))
        elapsed = time.monotonic() - start
        assert result["success"] is False
        assert elapsed < 1.5, f"wait_for path broken, took {elapsed:.2f}s"


class TestResultParsing:
    """Wrapper must parse both JSON and Python-repr responses from MCP servers.

    Postgres MCP servers return str(list_of_dicts) which includes Decimal(...)
    and single quotes — not valid JSON. The wrapper needs a safe fallback that
    understands Decimal and datetime literals.
    """

    def _make_wrapper(self, payload):
        session = _FakeSession(payload)
        tool = _FakeTool("execute_sql")
        ns = _ToolNamespace("postgres_test", session, [tool])
        return ns.execute_sql

    def test_json_list_parsed(self):
        wrapper = self._make_wrapper('[{"a": 1}, {"a": 2}]')
        result = asyncio.run(wrapper())
        assert isinstance(result, list)
        assert result[0]["a"] == 1

    def test_json_dict_parsed(self):
        wrapper = self._make_wrapper('{"count": 42}')
        result = asyncio.run(wrapper())
        assert isinstance(result, dict)
        assert result["count"] == 42

    def test_python_repr_with_decimal_parsed(self):
        import decimal
        payload = "[{'amount': Decimal('123.45'), 'name': 'foo'}]"
        wrapper = self._make_wrapper(payload)
        result = asyncio.run(wrapper())
        assert isinstance(result, list), f"got {type(result).__name__}: {result!r}"
        assert result[0]["amount"] == decimal.Decimal("123.45")
        assert result[0]["name"] == "foo"

    def test_python_repr_with_datetime_parsed(self):
        import datetime
        payload = "[{'created_at': datetime.datetime(2026, 4, 10, 12, 0, 0), 'id': 1}]"
        wrapper = self._make_wrapper(payload)
        result = asyncio.run(wrapper())
        assert isinstance(result, list)
        assert result[0]["created_at"] == datetime.datetime(2026, 4, 10, 12, 0, 0)
        assert result[0]["id"] == 1

    def test_python_repr_with_date_parsed(self):
        import datetime
        payload = "[{'day': datetime.date(2026, 4, 10)}]"
        wrapper = self._make_wrapper(payload)
        result = asyncio.run(wrapper())
        assert result[0]["day"] == datetime.date(2026, 4, 10)

    def test_python_repr_nested(self):
        import decimal
        payload = "[{'id': 1, 'items': [{'price': Decimal('9.99')}, {'price': Decimal('1.50')}]}]"
        wrapper = self._make_wrapper(payload)
        result = asyncio.run(wrapper())
        assert result[0]["items"][0]["price"] == decimal.Decimal("9.99")
        assert result[0]["items"][1]["price"] == decimal.Decimal("1.50")

    def test_plain_string_passthrough(self):
        wrapper = self._make_wrapper("just a plain error message")
        result = asyncio.run(wrapper())
        assert result == "just a plain error message"

    def test_unparseable_returns_raw_string(self):
        # Malformed but starts with [ — should not crash, should return raw
        wrapper = self._make_wrapper("[this is not valid python or json")
        result = asyncio.run(wrapper())
        assert isinstance(result, str)

    def test_iteration_yields_dicts_not_chars(self):
        """Regression test for the lime_api bug: result[0] used to return first char."""
        import decimal
        payload = "[{'a': Decimal('1.00')}, {'a': Decimal('2.00')}]"
        wrapper = self._make_wrapper(payload)
        result = asyncio.run(wrapper())
        first = result[0]
        assert isinstance(first, dict), f"got {type(first).__name__}: {first!r}"
        assert first["a"] == decimal.Decimal("1.00")


class TestIsErrorHandling:
    """A tool result with isError=True must raise MCPToolError, not return the
    error text as if it were a normal result."""

    def _make_wrapper(self, payload, is_error=True):
        session = _FakeSession(payload, is_error=is_error)
        tool = _FakeTool("list_projects")
        ns = _ToolNamespace("some_server", session, [tool])
        return ns.list_projects

    def test_error_result_raises(self):
        wrapper = self._make_wrapper("backend blew up: enum reference")
        with pytest.raises(MCPToolError, match="enum reference"):
            asyncio.run(wrapper())

    def test_error_message_names_server_and_tool(self):
        wrapper = self._make_wrapper("boom")
        with pytest.raises(MCPToolError, match=r"some_server\.list_projects"):
            asyncio.run(wrapper())

    def test_mcptoolerror_is_runtimeerror(self):
        # Sandboxed code can only catch by builtin name; RuntimeError must work.
        wrapper = self._make_wrapper("boom")
        with pytest.raises(RuntimeError):
            asyncio.run(wrapper())

    def test_empty_error_content_still_raises(self):
        wrapper = self._make_wrapper("")
        with pytest.raises(MCPToolError, match="no error detail"):
            asyncio.run(wrapper())

    def test_long_error_detail_truncated(self):
        wrapper = self._make_wrapper("x" * (MAX_ERROR_DETAIL + 500))
        with pytest.raises(MCPToolError, match="truncated"):
            asyncio.run(wrapper())

    def test_ok_result_not_affected(self):
        wrapper = self._make_wrapper('{"ok": true}', is_error=False)
        result = asyncio.run(wrapper())
        assert result == {"ok": True}

    def test_error_call_recorded_as_failure(self):
        # Metrics must see the failed call, mirroring the except path.
        recorder = _RecordingRecorder()
        session = _FakeSession("boom", is_error=True)
        tool = _FakeTool("list_projects")
        ns = _ToolNamespace("some_server", session, [tool], recorder=recorder)
        with pytest.raises(MCPToolError):
            asyncio.run(ns.list_projects())
        assert recorder.events
        assert recorder.events[-1]["success"] is False
        assert recorder.events[-1]["kind"] == "tool_call"


class TestAutoLimitInjection:
    """Proxy must inject a default LIMIT into SQL queries for postgres/mssql
    servers so the model doesn't accidentally burn context on a huge result."""

    def _make_ns(self, server_name, auto_limit=500):
        session = _FakeSession("[]")
        tool = _FakeTool("execute_sql")
        ns = _ToolNamespace(server_name, session, [tool], auto_limit=auto_limit)
        return ns, session

    def test_postgres_sql_kwarg_rewritten(self):
        ns, session = self._make_ns("postgres_test")
        asyncio.run(ns.execute_sql(sql="SELECT * FROM t"))
        assert "LIMIT 500" in session.last_call[1]["sql"].upper()

    def test_mssql_query_kwarg_rewritten_with_top(self):
        ns, session = self._make_ns("mssql")
        asyncio.run(ns.execute_sql(query="SELECT * FROM t"))
        assert "TOP" in session.last_call[1]["query"].upper()
        assert "500" in session.last_call[1]["query"]

    def test_existing_limit_preserved(self):
        ns, session = self._make_ns("postgres_test")
        asyncio.run(ns.execute_sql(sql="SELECT * FROM t LIMIT 10"))
        called_sql = session.last_call[1]["sql"]
        assert "500" not in called_sql
        assert "10" in called_sql

    def test_non_sql_server_untouched(self):
        # forgetful.execute_forgetful_tool(tool_name=..., arguments=...) —
        # pretend SELECT-like string; we must NOT rewrite because server isn't SQL.
        ns, session = self._make_ns("forgetful")
        asyncio.run(ns.execute_sql(sql="SELECT * FROM t"))
        assert session.last_call[1]["sql"] == "SELECT * FROM t"

    def test_insert_untouched(self):
        ns, session = self._make_ns("postgres_test")
        asyncio.run(ns.execute_sql(sql="INSERT INTO t VALUES (1)"))
        assert session.last_call[1]["sql"] == "INSERT INTO t VALUES (1)"

    def test_auto_limit_zero_disables_injection(self):
        ns, session = self._make_ns("postgres_test", auto_limit=0)
        asyncio.run(ns.execute_sql(sql="SELECT * FROM t"))
        assert session.last_call[1]["sql"] == "SELECT * FROM t"

    def test_no_sql_kwarg_no_error(self):
        # Some SQL tools may have other methods; if sql/query missing, do nothing.
        ns, session = self._make_ns("postgres_test")
        asyncio.run(ns.execute_sql())
        assert session.last_call[1] == {}

    def test_non_execute_sql_tool_untouched(self):
        session = _FakeSession("[]")
        tool = _FakeTool("list_schemas")
        ns = _ToolNamespace("postgres_test", session, [tool], auto_limit=500)
        asyncio.run(ns.list_schemas(sql="SELECT * FROM t"))
        assert session.last_call[1]["sql"] == "SELECT * FROM t"

    def test_executor_propagates_auto_limit(self):
        # End-to-end: CodeExecutor.execute passes auto_limit through to the
        # namespace so user code gets rewritten SQL on its way to the session.
        from code_runner.executor import CodeExecutor

        class FakePool:
            def __init__(self):
                self.session = _FakeSession("[{'n': 1}]")
                self.sessions = {"postgres_test": self.session}
                self.tools = {"postgres_test": [_FakeTool("execute_sql")]}

        pool = FakePool()
        executor = CodeExecutor(pool)
        code = 'result = await postgres_test.execute_sql(sql="SELECT * FROM t")'
        asyncio.run(executor.execute(code, auto_limit=500))
        assert "LIMIT 500" in pool.session.last_call[1]["sql"].upper()


class TestAutoDisplay:
    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_bare_expression_returned(self, executor):
        result = asyncio.run(executor.execute("x = 42\nx"))
        assert result["success"] is True
        assert "42" in result["output"]

    def test_bare_string_expression(self, executor):
        result = asyncio.run(executor.execute("'hello world'"))
        assert result["success"] is True
        assert "hello world" in result["output"]

    def test_assignment_no_auto_display(self, executor):
        result = asyncio.run(executor.execute("x = 42"))
        assert result["success"] is True
        assert result["output"] == ""

    def test_print_still_works(self, executor):
        result = asyncio.run(executor.execute("print('explicit')"))
        assert result["success"] is True
        assert "explicit" in result["output"]

    def test_dict_auto_display(self, executor):
        result = asyncio.run(executor.execute("{'a': 1, 'b': 2}"))
        assert result["success"] is True
        assert "a" in result["output"]


class TestOutputTruncation:
    """Output over max_output_bytes must be truncated with a footer.

    Protects against runaway outputs (e.g. SELECT * without LIMIT) from
    burning the model's context window.
    """

    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_small_output_not_truncated(self, executor):
        result = asyncio.run(executor.execute("print('hello')", max_output_bytes=1000))
        assert result["success"] is True
        assert "hello" in result["output"]
        assert "TRUNCATED" not in result["output"]

    def test_large_print_truncated(self, executor):
        result = asyncio.run(executor.execute(
            "print('x' * 50000)", max_output_bytes=1000
        ))
        assert result["success"] is True
        assert "TRUNCATED" in result["output"]
        assert len(result["output"].encode("utf-8")) < 1500

    def test_truncation_footer_mentions_totals(self, executor):
        result = asyncio.run(executor.execute(
            "print('x' * 50000)", max_output_bytes=1000
        ))
        assert "50001" in result["output"] or "50000" in result["output"]
        assert "1000" in result["output"]

    def test_default_limit_applied(self, executor):
        result = asyncio.run(executor.execute("print('x' * 30000)"))
        assert result["success"] is True
        assert "TRUNCATED" in result["output"]

    def test_under_default_limit_not_truncated(self, executor):
        result = asyncio.run(executor.execute("print('x' * 5000)"))
        assert result["success"] is True
        assert "TRUNCATED" not in result["output"]

    def test_zero_disables_truncation(self, executor):
        result = asyncio.run(executor.execute(
            "print('x' * 50000)", max_output_bytes=0
        ))
        assert result["success"] is True
        assert "TRUNCATED" not in result["output"]
        assert len(result["output"]) >= 50000

    def test_utf8_boundary_safe(self, executor):
        result = asyncio.run(executor.execute(
            "print('я' * 2000)", max_output_bytes=1001
        ))
        assert result["success"] is True
        result["output"].encode("utf-8")
        assert "TRUNCATED" in result["output"]

    def test_auto_displayed_value_truncated(self, executor):
        result = asyncio.run(executor.execute(
            "'x' * 50000", max_output_bytes=1000
        ))
        assert "TRUNCATED" in result["output"]

    def test_dict_return_truncated(self, executor):
        result = asyncio.run(executor.execute(
            "{'data': 'x' * 50000}", max_output_bytes=1000
        ))
        assert "TRUNCATED" in result["output"]

    def test_error_path_output_also_truncated(self, executor):
        code = "print('x' * 50000)\nraise ValueError('boom')"
        result = asyncio.run(executor.execute(code, max_output_bytes=1000))
        assert result["success"] is False
        assert "TRUNCATED" in result["output"]
        assert len(result["output"].encode("utf-8")) < 1500


class TestPersistentNamespace:
    """Variables set under a session_id must persist across execute() calls.

    Enables cheap follow-up operations on previously fetched data without
    re-running expensive MCP queries.
    """

    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_no_session_id_is_ephemeral(self, executor):
        asyncio.run(executor.execute("x = 42"))
        result = asyncio.run(executor.execute("print(x)"))
        assert result["success"] is False
        assert "NameError" in result["error"] or "x" in result["error"]

    def test_same_session_persists_variable(self, executor):
        r1 = asyncio.run(executor.execute("x = 42", session_id="s1"))
        assert r1["success"] is True
        r2 = asyncio.run(executor.execute("print(x)", session_id="s1"))
        assert r2["success"] is True
        assert "42" in r2["output"]

    def test_different_sessions_isolated(self, executor):
        asyncio.run(executor.execute("x = 'alpha'", session_id="a"))
        asyncio.run(executor.execute("x = 'beta'", session_id="b"))
        r_a = asyncio.run(executor.execute("print(x)", session_id="a"))
        r_b = asyncio.run(executor.execute("print(x)", session_id="b"))
        assert "alpha" in r_a["output"]
        assert "beta" in r_b["output"]

    def test_session_persists_complex_data(self, executor):
        asyncio.run(executor.execute(
            "data = [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}]",
            session_id="s",
        ))
        result = asyncio.run(executor.execute(
            "print(len(data))\nprint(data[0]['name'])",
            session_id="s",
        ))
        assert result["success"] is True
        assert "2" in result["output"]
        assert "a" in result["output"]

    def test_failed_exec_preserves_previous_vars(self, executor):
        asyncio.run(executor.execute("x = 100", session_id="s"))
        bad = asyncio.run(executor.execute("raise ValueError('oops')", session_id="s"))
        assert bad["success"] is False
        r = asyncio.run(executor.execute("print(x)", session_id="s"))
        assert r["success"] is True
        assert "100" in r["output"]

    def test_framework_names_not_persisted(self, executor):
        """json/re/datetime/asyncio and MCP server objects must not leak into user_vars."""
        asyncio.run(executor.execute("x = 1", session_id="s"))
        state = executor._sessions["s"]
        assert "x" in state.user_vars
        assert "json" not in state.user_vars
        assert "re" not in state.user_vars
        assert "datetime" not in state.user_vars
        assert "asyncio" not in state.user_vars
        assert "print" not in state.user_vars

    def test_lru_eviction_over_max_sessions(self, executor):
        from code_runner.executor import MAX_SESSIONS
        for i in range(MAX_SESSIONS + 5):
            asyncio.run(executor.execute(f"x = {i}", session_id=f"s{i}"))
        assert len(executor._sessions) <= MAX_SESSIONS
        # earliest sessions should be gone
        assert "s0" not in executor._sessions
        # most recent should be present
        assert f"s{MAX_SESSIONS + 4}" in executor._sessions

    def test_ttl_expiry_on_next_access(self, executor):
        import time

        from code_runner.executor import SESSION_TTL
        asyncio.run(executor.execute("x = 1", session_id="old"))
        # force expiry by rewinding last_access
        executor._sessions["old"].last_access = time.monotonic() - SESSION_TTL - 1
        asyncio.run(executor.execute("y = 2", session_id="new"))
        assert "old" not in executor._sessions

    def test_auto_display_works_in_session(self, executor):
        asyncio.run(executor.execute("x = 7", session_id="s"))
        r = asyncio.run(executor.execute("x * 2", session_id="s"))
        assert r["success"] is True
        assert "14" in r["output"]


class TestWorkspaceIntegration:
    """Task 3: open() wiring + eviction cleanup."""

    @pytest.fixture
    def executor(self, tmp_path):
        from code_runner.workspace import WorkspaceManager
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool(), workspace=WorkspaceManager(tmp_path))

    def test_open_writes_into_session_workspace(self, executor, tmp_path):
        code = (
            "with open('greeting.txt', 'w') as f:\n"
            "    f.write('hi')\n"
            "print('done')"
        )
        result = asyncio.run(executor.execute(code, session_id="s1"))
        assert result["success"] is True, result["error"]
        assert (tmp_path / "s1" / "greeting.txt").read_text() == "hi"

    def test_open_persists_across_calls_in_same_session(self, executor):
        r1 = asyncio.run(executor.execute(
            "with open('x.txt','w') as f: f.write('abc')",
            session_id="s2",
        ))
        assert r1["success"] is True, r1["error"]
        r2 = asyncio.run(executor.execute(
            "print(open('x.txt','r').read())",
            session_id="s2",
        ))
        assert r2["success"] is True, r2["error"]
        assert "abc" in r2["output"]

    def test_open_unavailable_without_session(self, executor):
        result = asyncio.run(executor.execute(
            "open('x.txt','w')",
            session_id=None,
        ))
        assert result["success"] is False

    def test_eviction_cleans_workspace(self, executor, tmp_path):
        r = asyncio.run(executor.execute(
            "open('a.txt','w').write('1')",
            session_id="evictme",
        ))
        assert r["success"] is True, r["error"]
        sess_dir = tmp_path / "evictme"
        assert sess_dir.exists()
        # Force expiration
        executor._sessions["evictme"].last_access = 0.0
        executor._evict_expired_sessions()
        assert not sess_dir.exists()


class TestSkillsIntegration:
    """Task 6: skills namespace injected into sandbox."""

    @pytest.fixture
    def fixture_dir(self):
        from pathlib import Path
        return Path(__file__).parent / "skills_fixtures"

    def _make_executor(self, tmp_path, fixture_dir):
        from code_runner.skills import SkillLoader, SkillsNamespace
        from code_runner.workspace import WorkspaceManager
        ns = SkillsNamespace(SkillLoader(fixture_dir).discover())
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(
            FakePool(),
            workspace=WorkspaceManager(tmp_path),
            skills=ns,
        )

    def test_skill_callable_from_sandbox(self, tmp_path, fixture_dir):
        ex = self._make_executor(tmp_path, fixture_dir)
        code = (
            "rows = [{'a': 1, 'b': 'x'}, {'a': 2, 'b': 'y'}]\n"
            "n = skills.sample_csv.write_csv(rows, 'out.csv')\n"
            "print(n)\n"
            "print(open('out.csv','r').read())"
        )
        result = asyncio.run(ex.execute(code, session_id="s1"))
        assert result["success"] is True, result["error"]
        assert "2" in result["output"]
        assert "a,b" in result["output"]

    def test_skills_unknown_name_raises_inside_sandbox(self, tmp_path, fixture_dir):
        ex = self._make_executor(tmp_path, fixture_dir)
        result = asyncio.run(ex.execute("skills.nope", session_id="s2"))
        assert result["success"] is False
        err = (result["error"] or "").lower()
        assert "unknown" in err or "skills" in err

    def test_skills_absent_when_not_provided(self, tmp_path):
        from code_runner.workspace import WorkspaceManager
        class FakePool:
            sessions = {}
            tools = {}
        ex = CodeExecutor(FakePool(), workspace=WorkspaceManager(tmp_path))
        result = asyncio.run(ex.execute("print(skills)", session_id="s3"))
        assert result["success"] is False
        assert "skills" in (result["error"] or "").lower()

    def test_skills_not_persisted_to_user_vars(self, tmp_path, fixture_dir):
        ex = self._make_executor(tmp_path, fixture_dir)
        asyncio.run(ex.execute("x = 1", session_id="s4"))
        # skills must not leak into the persisted user_vars
        state = ex._sessions["s4"]
        assert "skills" not in state.user_vars

    def test_skill_open_writes_into_workspace_via_bind(self, tmp_path, fixture_dir):
        # Re-verify the sample_csv path now relies on bind() rather than
        # the previous reach-in patch.
        ex = self._make_executor(tmp_path, fixture_dir)
        code = (
            "n = skills.sample_csv.write_csv("
            "    [{'k': 1}], 'rebound.csv')\n"
            "print(open('rebound.csv','r').read())"
        )
        result = asyncio.run(ex.execute(code, session_id="sb"))
        assert result["success"] is True, result["error"]
        assert "k" in result["output"] and "1" in result["output"]
        assert (tmp_path / "sb" / "rebound.csv").exists()


# --- cost receipt: the saving (or its absence) must be visible ---


def test_cost_footer_absent_without_tool_calls():
    stats = {"tool_calls": 0, "raw_tool_bytes": 0}
    assert _append_cost_footer("hello", stats, elapsed_ms=5) == "hello"


def test_cost_footer_reports_saving():
    stats = {"tool_calls": 3, "raw_tool_bytes": 100_000}
    out = _append_cost_footer("summary", stats, elapsed_ms=120)
    assert "3 tool calls" in out
    assert "97.7KB raw" in out
    assert "% saved)" in out


def test_cost_footer_flags_pointless_passthrough():
    raw = "x" * 1000
    stats = {"tool_calls": 1, "raw_tool_bytes": 1000}
    out = _append_cost_footer(raw, stats, elapsed_ms=10)
    assert "no aggregation happened here" in out


def test_cost_footer_silent_when_aggregation_happened():
    stats = {"tool_calls": 1, "raw_tool_bytes": 50_000}
    out = _append_cost_footer("42 rows", stats, elapsed_ms=10)
    assert "no aggregation happened here" not in out


# --- signature hint on argument-shape TypeErrors ---------------------------

def _skills_with_query(tmp_path):
    from code_runner.skills import SkillLoader, SkillsNamespace
    d = tmp_path / "fake_skill"
    d.mkdir()
    (d / "script.py").write_text(
        "async def query(query_text, client, project_ids=None, top_k=5):\n"
        "    return query_text\n"
    )
    (d / "SKILL.md").write_text("---\ndescription: fake\n---")
    return SkillsNamespace(SkillLoader(tmp_path).discover())


def test_signature_hint_lists_accepted_params(tmp_path):
    skills = _skills_with_query(tmp_path)
    with pytest.raises(TypeError) as exc_info:
        skills.fake_skill.query("q", None, limit=6)
    hint = _signature_hint(exc_info.value, skills)
    assert "skills.fake_skill.query(query_text, client, project_ids=None, top_k=5)" in hint


def test_signature_hint_covers_missing_positional(tmp_path):
    skills = _skills_with_query(tmp_path)
    with pytest.raises(TypeError) as exc_info:
        skills.fake_skill.query("q")
    assert "skills.fake_skill.query(" in _signature_hint(exc_info.value, skills)


def test_signature_hint_silent_for_unrelated_errors(tmp_path):
    skills = _skills_with_query(tmp_path)
    assert _signature_hint(TypeError("unsupported operand type(s)"), skills) == ""
    msg = "query() got an unexpected keyword argument 'x'"
    assert _signature_hint(ValueError(msg), skills) == ""
    assert _signature_hint(TypeError("foo() got an unexpected keyword argument 'x'"), skills) == ""
    assert _signature_hint(TypeError("query() got an unexpected keyword argument 'x'"), None) == ""


# --- name-error hint: sandbox roster on unknown names -----------------------

def _namespace_with_proxies():
    return {
        "__builtins__": {},
        "json": None,
        "postgres_lime_prod": _ToolNamespace("postgres_lime_prod", None, []),
        "postgres_ofd": _ToolNamespace("postgres_ofd", None, []),
        "_internal": object(),
    }


def test_name_error_hint_suggests_close_match_and_proxies():
    ns = _namespace_with_proxies()
    hint = _name_error_hint(NameError("name 'postgres_lime' is not defined"), ns)
    assert "postgres_lime_prod" in hint
    assert "доступные MCP-прокси: postgres_lime_prod, postgres_ofd" in hint
    assert "_internal" not in hint


def test_name_error_hint_lists_proxies_without_close_match():
    ns = _namespace_with_proxies()
    hint = _name_error_hint(NameError("name 'zzz_unrelated' is not defined"), ns)
    assert "доступные MCP-прокси" in hint


def test_name_error_hint_silent_for_other_errors():
    ns = _namespace_with_proxies()
    assert _name_error_hint(ValueError("name 'x' is not defined"), ns) == ""
    assert _name_error_hint(NameError("weird message shape"), ns) == ""
    assert _name_error_hint(NameError("name 'x' is not defined"), {"__builtins__": {}}) == ""


# --- limit hint: skills hard-validator ValueError ("<=500 chars") ------------

def test_limit_error_hint_names_the_limit():
    hint = _limit_error_hint(ValueError("context: <=500 chars, got 608"))
    assert "500" in hint
    assert "value[:500]" in hint


def test_limit_error_hint_silent_for_other_value_errors():
    assert _limit_error_hint(ValueError("invalid literal for int()")) == ""
    assert _limit_error_hint(TypeError("context: <=500 chars, got 608")) == ""


# --- import hint: roster for non-preloaded modules ---------------------------

def test_import_os_error_lists_preloaded_roster():
    with pytest.raises(ValueError) as exc_info:
        validate_code("import os")
    msg = str(exc_info.value)
    assert "preloaded:" in msg
    assert "json" in msg
    assert "open()" in msg
