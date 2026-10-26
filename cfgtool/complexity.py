"""McCabe cyclomatic complexity, computed two independent ways (plus radon, optionally)."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass

from .graph import CFG
from .parser import _LOOP_NODES, _SCOPE_NODES, _TRY_NODES, MODULE_NAME, is_irrefutable

RISK_BANDS = [(10, "low"), (20, "moderate"), (50, "high")]


def risk_band(m: int) -> str:
    for upper, name in RISK_BANDS:
        if m <= upper:
            return name
    return "very high"


def graph_complexity(cfg: CFG) -> int:
    """M = E - N + 2P."""
    return cfg.num_edges - cfg.num_nodes + 2 * cfg.connected_components()


# -- AST-side analysis -------------------------------------------------------

def _falls_through(stmts: list[ast.stmt]) -> bool:
    """Whether control can run off the end of ``stmts``.

    Only the last statement matters: code after a return is unreachable but
    still drawn, and it falls through like any other code.
    """
    if not stmts:
        return True
    stmt = stmts[-1]
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return False
    if isinstance(stmt, ast.If):
        return _falls_through(stmt.body) or _falls_through(stmt.orelse)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _falls_through(stmt.body)
    if isinstance(stmt, _TRY_NODES):
        normal = _try_completes_normally(stmt)
        return normal and _falls_through(stmt.finalbody)
    if isinstance(stmt, ast.Match):
        last = stmt.cases[-1]
        return (any(_falls_through(c.body) for c in stmt.cases)
                or not (is_irrefutable(last.pattern) and last.guard is None))
    return True  # loops always have a loop-condition-false exit


def _try_completes_normally(stmt: ast.Try) -> bool:
    return (_falls_through(stmt.orelse or stmt.body)
            or any(_falls_through(h.body) for h in stmt.handlers))


def _escape_kinds(stmts: list[ast.AST]) -> set[str]:
    """Kinds of jump (return/raise/break/continue) that leave ``stmts``."""
    kinds: set[str] = set()

    def walk(node: ast.AST, in_loop: bool) -> None:
        if isinstance(node, _SCOPE_NODES):
            return
        if isinstance(node, ast.Return):
            kinds.add("return")
        elif isinstance(node, (ast.Raise, ast.Assert)):
            kinds.add("raise")
        elif isinstance(node, (ast.Break, ast.Continue)) and not in_loop:
            kinds.add("break" if isinstance(node, ast.Break) else "continue")
        if isinstance(node, _LOOP_NODES):
            for s in node.body:
                walk(s, True)
            for s in node.orelse:  # break in a loop's else targets the outer loop
                walk(s, in_loop)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, in_loop)

    for s in stmts:
        walk(s, False)
    return kinds


def finally_copies(stmt: ast.Try) -> int:
    """How many copies of ``stmt.finalbody`` the CFG contains: one per exit kind, plus fall-through."""
    region: list[ast.AST] = [*stmt.body, *stmt.handlers, *stmt.orelse]
    return int(_try_completes_normally(stmt)) + len(_escape_kinds(region))


def count_decisions(body: list[ast.stmt], strict: bool = False) -> int:
    """Count decision points in ``body``, skipping nested functions, classes and lambdas."""

    def count(node: ast.AST) -> int:
        if isinstance(node, _SCOPE_NODES):
            return 0
        n = 0
        if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.IfExp, ast.Assert)):
            n += 1
        elif isinstance(node, ast.comprehension):
            n += 1 + len(node.ifs)
        elif isinstance(node, _TRY_NODES):
            n += len(node.handlers)
            if node.finalbody:
                # Decisions inside finally are duplicated in every copy.
                n += sum(count(c) for c in (*node.body, *node.handlers, *node.orelse))
                n += finally_copies(node) * sum(count(s) for s in node.finalbody)
                return n
        elif isinstance(node, ast.match_case):
            if not (is_irrefutable(node.pattern) and node.guard is None):
                n += 1
        elif strict and isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        return n + sum(count(c) for c in ast.iter_child_nodes(node))

    return sum(count(s) for s in body)


# -- radon --------------------------------------------------------------------

def radon_complexities(source: str) -> dict[object, int]:
    """Map function line numbers (and MODULE_NAME) to radon's cyclomatic complexity.

    Raises ImportError if radon is not installed.
    """
    from radon.visitors import ComplexityVisitor

    visitor = ComplexityVisitor.from_code(source)
    found: dict[object, int] = {MODULE_NAME: visitor.complexity}

    def add_function(func) -> None:
        found[func.lineno] = func.complexity
        for closure in func.closures:
            add_function(closure)

    def add_class(cls) -> None:
        for method in cls.methods:
            add_function(method)
        for inner in cls.inner_classes:
            add_class(inner)

    for func in visitor.functions:
        add_function(func)
    for cls in visitor.classes:
        add_class(cls)
    return found


RADON_REASONS = {
    "and/or": "and/or operands",
    "try-else": "try-else clauses",
    "loop-else": "for/while-else clauses",
}


def radon_known_differences(body: list[ast.stmt], strict: bool = False) -> dict[str, int]:
    """Decision points radon counts in ``body`` that cfgtool deliberately does not.

    * and/or: radon counts ``len(values) - 1`` per BoolOp; cfgtool only with --strict-boolean.
      radon does not look inside ``assert``, so neither does this count.
    * try-else / loop-else: radon adds 1 per ``else`` clause; in the CFG an else
      clause is straight-line code and adds no branch.

    Nested functions, classes and lambdas are skipped, matching cfgtool's scope.
    """
    found = dict.fromkeys(RADON_REASONS, 0)
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPE_NODES + (ast.Assert,)):
            continue
        if isinstance(node, ast.BoolOp) and not strict:
            found["and/or"] += len(node.values) - 1
        elif isinstance(node, ast.Try) and node.orelse:  # radon does not handle TryStar at all
            found["try-else"] += 1
        elif isinstance(node, _LOOP_NODES) and node.orelse:
            found["loop-else"] += 1
        stack.extend(ast.iter_child_nodes(node))
    return found


# -- results ------------------------------------------------------------------

@dataclass
class Complexity:
    name: str
    lineno: int
    nodes: int
    edges: int
    components: int
    decision_points: int
    m_graph: int
    m_ast: int
    m_radon: int | None = None
    radon_explained: dict[str, int] | None = None  # known reasons radon is higher, by reason
    radon_unexplained: int | None = None           # radon - m_graph - sum(radon_explained)

    @property
    def risk(self) -> str:
        return risk_band(self.m_graph)

    @property
    def mismatch(self) -> bool:
        return self.m_graph != self.m_ast

    def to_dict(self) -> dict:
        return {**asdict(self), "risk": self.risk, "mismatch": self.mismatch}


def analyze(cfg: CFG, strict: bool = False) -> Complexity:
    decisions = count_decisions(cfg.ast_body, strict)
    return Complexity(
        name=cfg.name,
        lineno=cfg.lineno,
        nodes=cfg.num_nodes,
        edges=cfg.num_edges,
        components=cfg.connected_components(),
        decision_points=decisions,
        m_graph=graph_complexity(cfg),
        m_ast=decisions + 1,
    )
