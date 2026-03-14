import asyncio
import pytest

from code_runner.executor import validate_code, CodeExecutor


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
