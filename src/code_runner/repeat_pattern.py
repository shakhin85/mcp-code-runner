"""Повтор формы блока в сессии → HINT save_skill с черновиком (ресёрч #5, Agent Workflow Memory).

Форма блока — AST с литералами str/int/float/bytes, заменёнными на плейсхолдер:
блоки, которые отличаются только таблицей или id, дают один отпечаток. Литералы,
которые менялись между первым и текущим повтором, становятся параметрами черновика.
Без LLM: только ast.
"""

import ast
import hashlib
from typing import Any

REPEAT_HINT_AT = 3
DRAFT_MAX_CHARS = 1500
_PARAM_TYPES = (str, int, float, bytes)


def _literals(tree: ast.AST) -> list[ast.Constant]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, _PARAM_TYPES)
        and not isinstance(n.value, bool)
    ]


class _Params(ast.NodeTransformer):
    def __init__(self, names: dict[int, str]) -> None:
        self._names = names

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        name = self._names.get(id(node))
        return ast.copy_location(ast.Name(name, ast.Load()), node) if name else node


def pattern_key(code: str) -> tuple[str, list[Any]] | None:
    """(отпечаток формы, значения литералов в порядке ast.walk) или None.

    None — блок не кандидат в скилл: не парсится или уже зовёт skills.*.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    if any(isinstance(n, ast.Name) and n.id == "skills" for n in ast.walk(tree)):
        return None
    lits = _literals(tree)
    values = [n.value for n in lits]
    for n in lits:
        n.value = "…"
    shape = ast.dump(tree, annotate_fields=False)
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16], values


def draft(code: str, first: list[Any]) -> str:
    """`async def run(p0, …)`: литералы, отличные от первого повтора, стали параметрами."""
    tree = ast.parse(code)
    params: dict[int, str] = {}
    for n, old in zip(_literals(tree), first, strict=False):
        if n.value != old:
            params[id(n)] = f"p{len(params)}"
    tree = _Params(params).visit(tree)
    fn = ast.AsyncFunctionDef(
        name="run",
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(p) for p in params.values()], kwonlyargs=[],
            kw_defaults=[], defaults=[],
        ),
        body=tree.body or [ast.Pass()],
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    return ast.unparse(module)[:DRAFT_MAX_CHARS]


def hint(code: str, first: list[Any], count: int) -> str:
    """HINT с ведущим переводом строки."""
    return (
        f"\n\nHINT: паттерн повторился {count}x в этой сессии — вынести в скилл? "
        "save_skill(name=…, code=<черновик>, description=…, "
        "example='print(await skills.<name>.run(…))'); "
        "сервер внутри скилла — через ~/.claude/lib/skill_ns.py. Черновик:\n"
        + draft(code, first)
    )
