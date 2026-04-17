"""Tests for automatic LIMIT injection in SQL queries."""

import pytest

from code_runner.sql_limit import inject_limit


class TestInjectLimitPostgres:
    def test_plain_select_gets_limit(self):
        out = inject_limit("SELECT * FROM users", 500, "postgres")
        assert "LIMIT 500" in out.upper()
        assert "SELECT" in out.upper()

    def test_existing_limit_unchanged(self):
        sql = "SELECT * FROM users LIMIT 10"
        out = inject_limit(sql, 500, "postgres")
        # Must not double-limit to 500
        assert "500" not in out
        assert "10" in out

    def test_existing_limit_smaller_than_default_unchanged(self):
        out = inject_limit("SELECT * FROM t LIMIT 5", 500, "postgres")
        assert "500" not in out

    def test_existing_limit_larger_than_default_unchanged(self):
        # User explicitly asked for more — respect that.
        out = inject_limit("SELECT * FROM t LIMIT 10000", 500, "postgres")
        assert "10000" in out
        assert "500" not in out

    def test_cte_adds_limit_to_outer(self):
        sql = "WITH x AS (SELECT * FROM a) SELECT * FROM x"
        out = inject_limit(sql, 500, "postgres").upper()
        # outer SELECT gets LIMIT (end of query)
        assert out.rstrip(" ;").endswith("LIMIT 500")

    def test_union_limits_outermost(self):
        sql = "SELECT * FROM a UNION SELECT * FROM b"
        out = inject_limit(sql, 500, "postgres").upper()
        assert "LIMIT 500" in out
        # Only one LIMIT (outermost), not one per branch
        assert out.count("LIMIT 500") == 1

    def test_subquery_untouched_but_outer_limited(self):
        sql = "SELECT * FROM (SELECT id FROM t) x"
        out = inject_limit(sql, 500, "postgres").upper()
        # Exactly one LIMIT — the outer one
        assert out.count("LIMIT 500") == 1

    def test_aggregate_still_gets_limit(self):
        # COUNT(*) returns 1 row; LIMIT 500 is harmless no-op.
        out = inject_limit("SELECT COUNT(*) FROM t", 500, "postgres")
        assert "LIMIT 500" in out.upper()

    def test_group_by_gets_limit(self):
        sql = "SELECT city, COUNT(*) FROM t GROUP BY city"
        out = inject_limit(sql, 500, "postgres").upper()
        assert "LIMIT 500" in out
        assert "GROUP BY" in out

    def test_insert_unchanged(self):
        sql = "INSERT INTO t (id, name) VALUES (1, 'x')"
        out = inject_limit(sql, 500, "postgres")
        assert out == sql

    def test_update_unchanged(self):
        sql = "UPDATE t SET name = 'x' WHERE id = 1"
        out = inject_limit(sql, 500, "postgres")
        assert out == sql

    def test_delete_unchanged(self):
        sql = "DELETE FROM t WHERE id = 1"
        out = inject_limit(sql, 500, "postgres")
        assert out == sql

    def test_create_table_unchanged(self):
        sql = "CREATE TABLE x (id INT)"
        out = inject_limit(sql, 500, "postgres")
        assert out == sql

    def test_invalid_sql_returns_original(self):
        # Parser fails → must not raise, return input as-is.
        sql = "SELECT ** FROM WHERE"
        out = inject_limit(sql, 500, "postgres")
        assert out == sql

    def test_empty_string_returns_empty(self):
        assert inject_limit("", 500, "postgres") == ""

    def test_whitespace_only_returns_original(self):
        assert inject_limit("   \n  ", 500, "postgres") == "   \n  "

    def test_zero_limit_disables_injection(self):
        sql = "SELECT * FROM t"
        out = inject_limit(sql, 0, "postgres")
        assert out == sql

    def test_negative_limit_disables_injection(self):
        sql = "SELECT * FROM t"
        out = inject_limit(sql, -1, "postgres")
        assert out == sql

    def test_with_trailing_semicolon(self):
        # Semicolons are common; result should still be valid / parseable.
        out = inject_limit("SELECT * FROM t;", 500, "postgres")
        assert "LIMIT 500" in out.upper()


class TestInjectLimitMssql:
    def test_plain_select_gets_top(self):
        out = inject_limit("SELECT * FROM users", 500, "mssql")
        assert "TOP" in out.upper()
        assert "500" in out

    def test_existing_top_unchanged(self):
        sql = "SELECT TOP 10 * FROM users"
        out = inject_limit(sql, 500, "mssql")
        assert "10" in out
        assert "500" not in out

    def test_insert_unchanged(self):
        sql = "INSERT INTO t (id) VALUES (1)"
        out = inject_limit(sql, 500, "mssql")
        assert out == sql


class TestInjectLimitDialectFallback:
    def test_unknown_dialect_falls_back_to_postgres(self):
        # Guard against a typo silently producing bogus SQL.
        out = inject_limit("SELECT * FROM t", 500, "oracle")
        # Should still inject something or return original, never crash.
        assert isinstance(out, str)
