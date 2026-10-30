"""Таблица предложений в docs/research/*.md несёт колонку «Статус» с непустой ячейкой.

Таблица предложений — та, где первая колонка «#» или есть колонка «Идея».
Без статуса вопрос «что из ресёрча покрыто» решается угадыванием.
"""

from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parent.parent / "docs" / "research"


def _proposal_tables(text: str):
    lines = text.split("\n")
    for i, line in enumerate(lines[:-1]):
        if not (line.startswith("|") and lines[i + 1].startswith("|---")):
            continue
        header = [c.strip() for c in line.strip().strip("|").split("|")]
        if header[0] != "#" and "Идея" not in header:
            continue
        rows = []
        for row in lines[i + 2:]:
            if not row.startswith("|"):
                break
            rows.append([c.strip() for c in row.strip().strip("|").split("|")])
        yield i + 1, header, rows


@pytest.mark.parametrize("doc", sorted(RESEARCH.glob("*.md")), ids=lambda p: p.name)
def test_proposal_rows_have_status(doc: Path) -> None:
    tables = list(_proposal_tables(doc.read_text(encoding="utf-8")))
    assert tables, f"{doc.name}: нет таблицы предложений (# или «Идея»)"
    for lineno, header, rows in tables:
        assert "Статус" in header, f"{doc.name}:{lineno}: нет колонки «Статус»"
        col = header.index("Статус")
        for n, row in enumerate(rows, start=lineno + 2):
            assert len(row) > col and row[col], f"{doc.name}:{n}: пустой «Статус»"
