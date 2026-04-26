from pathlib import Path

import pytest

from code_runner.skills import SkillLoader, SkillsNamespace


TEMPLATES = Path(__file__).resolve().parent.parent / "skills_templates"


@pytest.fixture(scope="module")
def ns():
    return SkillsNamespace(SkillLoader(TEMPLATES).discover())


def test_csv_export_writes_file(ns, tmp_path):
    out = tmp_path / "x.csv"
    n = ns.csv_export.write_rows(
        [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], str(out)
    )
    assert n == 2
    text = out.read_text()
    assert "a,b" in text and "1,x" in text


def test_csv_export_empty_rows(ns, tmp_path):
    out = tmp_path / "empty.csv"
    n = ns.csv_export.write_rows([], str(out))
    assert n == 0
    assert out.read_text() == ""


def test_snapshot_diff_detects_changes(ns):
    before = [{"id": 1, "v": 10}, {"id": 2, "v": 20}]
    after = [{"id": 1, "v": 11}, {"id": 3, "v": 30}]
    d = ns.snapshot_diff.diff(before, after, key="id")
    assert d == {"added": [3], "removed": [2], "changed": [1]}


def test_snapshot_diff_no_changes(ns):
    rows = [{"id": 1, "v": 10}]
    d = ns.snapshot_diff.diff(rows, rows, key="id")
    assert d == {"added": [], "removed": [], "changed": []}


def test_schema_dump_renders_table(ns):
    cols = [
        {"name": "id", "type": "int", "nullable": "no"},
        {"name": "amount", "type": "decimal", "nullable": "yes"},
    ]
    out = ns.schema_dump.render_columns(cols)
    assert "name" in out and "type" in out
    assert "id" in out and "decimal" in out
    # has separator row
    assert "----" in out


def test_schema_dump_empty(ns):
    assert ns.schema_dump.render_columns([]) == "(no columns)"
