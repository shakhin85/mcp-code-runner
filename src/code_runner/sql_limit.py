"""Automatic LIMIT/TOP injection for user-issued SQL queries.

Protects the model's context from runaway SELECT results by defaulting to a
small row cap when the user hasn't set one. Only applies to top-level SELECT
(including CTEs and UNIONs); INSERT/UPDATE/DELETE/DDL are left untouched.
Parse errors are swallowed — we return the original SQL so the DB can report
its own error rather than letting this helper mask one.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp

_DIALECT_MAP = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mssql": "tsql",
    "tsql": "tsql",
}

_MUTATION_TYPES: tuple[type, ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
)


def inject_limit(sql: str, default_limit: int, dialect: str) -> str:
    if default_limit <= 0 or not sql or not sql.strip():
        return sql

    sg_dialect = _DIALECT_MAP.get(dialect.lower(), "postgres")

    try:
        tree = sqlglot.parse_one(sql, read=sg_dialect)
    except Exception:
        return sql

    if tree is None:
        return sql

    if isinstance(tree, _MUTATION_TYPES):
        return sql

    if not isinstance(tree, exp.Query):
        return sql

    # LIMIT present — respect user's choice (smaller OR larger).
    if tree.args.get("limit") is not None:
        return sql

    # MSSQL TOP is stored as a Limit attached to the wrapped Select.
    inner_select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if inner_select is not None and inner_select.args.get("limit") is not None:
        return sql

    try:
        new_tree = tree.limit(default_limit)
        return new_tree.sql(dialect=sg_dialect)
    except Exception:
        return sql


_WRITE_NODES: tuple[type, ...] = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Into, exp.Lock)

# Parser differential: T-SQL needs no ';' between statements, so `SELECT 1 DELETE FROM t`
# parses as one SELECT with alias DELETE while the server runs both. Any identifier or
# alias spelled like a statement keyword is refused (quoted too: fail closed).
_STATEMENT_KEYWORDS: frozenset[str] = frozenset(
    [
        "delete",
        "update",
        "insert",
        "merge",
        "exec",
        "execute",
        "drop",
        "alter",
        "create",
        "truncate",
        "grant",
        "revoke",
        "deny",
        "use",
        "set",
        "declare",
        "backup",
        "restore",
        "kill",
        "shutdown",
        "dbcc",
        "bulk",
        "call",
        "do",
        "copy",
        "vacuum",
        "reindex",
        "cluster",
        "lock",
        "listen",
        "notify",
    ]
)
# Functions that write, lock, reach other servers or read server files from inside a SELECT.
_DENIED_FUNCTIONS: frozenset[str] = frozenset(
    [
        "openquery",
        "openrowset",
        "opendatasource",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "nextval",
        "setval",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "set_config",
        "query_to_xml",
    ]
)
_DENIED_FUNCTION_PREFIXES: tuple[str, ...] = ("pg_advisory_lock", "pg_advisory_xact_lock", "dblink")


def _node_reason(stmt: exp.Expression) -> str | None:
    for node in stmt.walk():
        if isinstance(node, _WRITE_NODES):
            return node.key.upper()
        if isinstance(node, exp.Identifier) and node.name.lower() in _STATEMENT_KEYWORDS:
            return f"идентификатор {node.name.upper()}"
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _DENIED_FUNCTIONS or name.startswith(_DENIED_FUNCTION_PREFIXES):
                return f"функция {name}"
    return None


def non_read_reason(sql: str, dialect: str) -> str | None:
    """Why `sql` is not read-only, or None when every statement is a plain SELECT.

    Fail closed: a parse error, a Command/unknown statement, SELECT … INTO, FOR UPDATE,
    a data-modifying node inside a query (postgres `WITH x AS (DELETE …)`), a statement
    keyword used as identifier/alias (T-SQL parser differential) and a denylisted
    function (_DENIED_FUNCTIONS) all count as writes. EXPLAIN/SHOW/SET/USE are not
    whitelisted — they parse as Command or non-Query and are refused.

    This is a filter, not a boundary: the real one is the read-only DB principal
    (svc_datahub has only CONNECT+SELECT in Jade.Production).
    """
    sg_dialect = _DIALECT_MAP.get(dialect.lower(), "postgres")
    try:
        statements = [s for s in sqlglot.parse(sql, read=sg_dialect) if s is not None]
    except Exception:
        return "SQL не разобран"
    if not statements:
        return "SQL не разобран"
    for stmt in statements:
        if isinstance(stmt, exp.Command):
            return f"SQL не только чтение: {str(stmt.this).upper()}"
        if not isinstance(stmt, exp.Query):
            return f"SQL не только чтение: {stmt.key.upper()}"
        reason = _node_reason(stmt)
        if reason is not None:
            return f"SQL не только чтение: {reason}"
    return None
