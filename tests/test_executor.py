import asyncio
import pytest

from code_runner.executor import validate_code, CodeExecutor, _ToolNamespace


class _FakeText:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeText(text)]


class _FakeSession:
    """Test double: records the last call and returns a canned text payload."""
    def __init__(self, payload_text):
        self._payload = payload_text
        self.last_call = None

    async def call_tool(self, name, kwargs):
        self.last_call = (name, kwargs)
        return _FakeResult(self._payload)


class _FakeTool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


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
        # __name__ dunder blocked — use without dunder
        assert result["success"] is False

    def test_type_builtin_simple(self, executor):
        result = asyncio.run(executor.execute("t = type(42)\nprint(t is int)"))
        assert result["success"] is True
        assert "True" in result["output"]


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
