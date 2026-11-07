"""SQL servers: execute_sql is classified by statement text, not by tool name.
Only plain SELECT passes without allow_side_effects; everything else (and
anything sqlglot can't parse) is refused. Harness mirrors test_side_effect_gate.py.
"""

import asyncio

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor


class _Content:
    text = "[]"


class _Result:
    isError = False
    content = [_Content()]


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return _Result()


def _run(server, sql, arg="sql", **kw):
    session = _Session()

    class Pool:
        sessions = {server: session}
        tools = {server: [Tool(name="execute_sql", inputSchema={"type": "object"})]}

    ex = CodeExecutor(Pool(), isolation="inprocess")
    code = f"await {server}.execute_sql({arg}={sql!r})"
    return asyncio.run(ex.execute(code, timeout=5, **kw)), session


SERVERS = ["mssql", "postgres_main"]


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select a from t where b = 1",
        "WITH c AS (SELECT 1 AS x) SELECT x FROM c",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT 1; SELECT 2",
    ],
)
def test_read_only_sql_passes_without_permission(server, sql):
    result, session = _run(server, sql)
    assert result["success"] is True, result["error"]
    assert len(session.calls) == 1


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM t WHERE id = 1", "DELETE"),
        ("UPDATE t SET a = 1", "UPDATE"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("DROP TABLE x", "DROP"),
        ("TRUNCATE TABLE x", "TRUNCATE"),
        ("SELECT * INTO copy FROM t", "INTO"),
        ("SELECT 1; DROP TABLE x", "DROP"),
        ("EXEC sp_who", "EXEC"),
        ("SET search_path = x", "SET"),
        ("SELECT FROM WHERE ((", "не разобран"),
    ],
)
def test_non_read_sql_refused_with_reason(server, sql, reason):
    result, session = _run(server, sql)
    assert result["success"] is False
    assert "PermissionError" in result["error"], result["error"]
    assert reason in result["error"], result["error"]
    assert (f'allow_side_effects=["{server}.execute_sql"] to execute_code') in result["error"], (
        result["error"]
    )
    assert session.calls == []


@pytest.mark.parametrize("server", SERVERS)
def test_non_read_sql_allowed_explicitly(server):
    result, session = _run(server, "DELETE FROM t", allow_side_effects=[f"{server}.execute_sql"])
    assert result["success"] is True, result["error"]
    assert len(session.calls) == 1


def test_use_refused_on_mssql():
    result, session = _run("mssql", "USE other_db")
    assert result["success"] is False
    assert "side-effecting" in result["error"]
    assert session.calls == []


def test_postgres_writable_cte_refused():
    result, session = _run("postgres_main", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d")
    assert result["success"] is False
    assert "DELETE" in result["error"]
    assert session.calls == []


def test_query_arg_is_classified_too():
    result, _ = _run("mssql", "DELETE FROM t", arg="query")
    assert result["success"] is False
    assert "DELETE" in result["error"]


def test_empty_sql_keeps_old_behaviour():
    result, session = _run("mssql", "")
    assert result["success"] is True, result["error"]
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("server", "sql", "reason"),
    [
        ("mssql", "SELECT 1 DELETE FROM t", "DELETE"),
        ("mssql", "SELECT 1 delete FROM t", "DELETE"),
        ("mssql", "SELECT 1 AS [drop] FROM t", "DROP"),
        ("postgres_main", 'SELECT 1 AS "truncate" FROM t', "TRUNCATE"),
        ("mssql", "SELECT * FROM OPENQUERY(s, 'DELETE FROM t')", "openquery"),
        ("mssql", "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", "openrowset"),
        ("postgres_main", "SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
        ("postgres_main", "SELECT PG_Cancel_Backend(1)", "pg_cancel_backend"),
        ("postgres_main", "SELECT nextval('s')", "nextval"),
        ("postgres_main", "SELECT setval('s', 1)", "setval"),
        ("postgres_main", "SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
        ("postgres_main", "SELECT pg_advisory_xact_lock(1)", "pg_advisory_xact_lock"),
        ("postgres_main", "SELECT * FROM dblink('x', 'DELETE FROM t') AS d(a int)", "dblink"),
        ("postgres_main", "SELECT lo_import('/etc/passwd')", "lo_import"),
        ("postgres_main", "SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("postgres_main", "SELECT set_config('a', 'b', false)", "set_config"),
        ("postgres_main", "SELECT * FROM t FOR UPDATE", "LOCK"),
        ("postgres_main", "SELECT * FROM t FOR SHARE", "LOCK"),
    ],
)
def test_bypass_forms_refused(server, sql, reason):
    result, session = _run(server, sql)
    assert result["success"] is False
    assert reason.lower() in result["error"].lower(), result["error"]
    assert session.calls == []


@pytest.mark.parametrize(
    ("server", "sql"),
    [
        ("mssql", "SELECT region, COUNT(*) AS n, SUM(amount) AS s FROM t GROUP BY region"),
        ("mssql", "SELECT a.id, b.name FROM a JOIN b ON b.id = a.id WHERE a.x > 1"),
        ("mssql", "SELECT id, ROW_NUMBER() OVER (PARTITION BY g ORDER BY d) AS rn FROM t"),
        ("mssql", "SELECT DB_NAME(), DATEADD(day, -7, GETDATE())"),
        ("postgres_main", "SELECT date_trunc('day', ts), count(*) FROM t GROUP BY 1"),
        ("postgres_main", "SELECT id, SUM(x) OVER (PARTITION BY g) FROM t ORDER BY id LIMIT 5"),
    ],
)
def test_analytics_select_still_passes(server, sql):
    result, session = _run(server, sql)
    assert result["success"] is True, result["error"]
    assert len(session.calls) == 1


def _run_code(code):
    session = _Session()

    class Pool:
        sessions = {"mssql": session}
        tools = {"mssql": [Tool(name="execute_sql", inputSchema={"type": "object"})]}

    ex = CodeExecutor(Pool(), isolation="inprocess")
    return asyncio.run(ex.execute(code, timeout=5)), session


def test_every_sql_argument_is_classified():
    result, session = _run_code("await mssql.execute_sql(sql='SELECT 1', query='DELETE FROM t')")
    assert result["success"] is False
    assert "DELETE" in result["error"]
    assert session.calls == []


@pytest.mark.parametrize("value", ["['DELETE FROM t']", "None", "b'DELETE FROM t'", "5"])
def test_non_str_sql_argument_refused(value):
    result, session = _run_code(f"await mssql.execute_sql(sql={value})")
    assert result["success"] is False
    assert "SQL не разобран" in result["error"], result["error"]
    assert session.calls == []
