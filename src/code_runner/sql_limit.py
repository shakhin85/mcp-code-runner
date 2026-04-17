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
