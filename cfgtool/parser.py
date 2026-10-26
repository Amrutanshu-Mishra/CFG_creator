"""AST -> CFG builder.

One CFG is built for module-level code and one for every function or method
(including nested ones). Nested ``def``/``class`` statements appear as a single
statement in their enclosing CFG; their bodies get their own graphs. Lambda
bodies are not expanded (they run later, not where they are written).

The builder and the AST decision counter in ``complexity.py`` follow the same
rules, so E - N + 2P equals decisions + 1:

* if / elif / while / for       -> one 2-way branch (T/F) each
* assert                        -> T continues, F raises AssertionError
* x if c else y                 -> one 2-way branch
* comprehension                 -> each ``for`` clause is a loop header and each
                                   ``if`` clause a 2-way branch
* try                           -> one extra edge per ``except`` handler,
                                   all leaving the block that starts the try
* try ... finally               -> return/raise/break/continue inside the try
                                   route through a separate copy of the finally
                                   body per exit kind, so decisions inside a
                                   finally body count once per copy
* match                         -> a chain of case tests; each case is a T/F
                                   branch unless it is an irrefutable pattern
                                   with no guard (``case _:``)
* and / or (--strict-boolean)   -> each extra operand is a short-circuit branch
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

from .graph import CFG, Block

MODULE_NAME = "<module>"

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_SCOPE_NODES = _DEF_NODES + (ast.Lambda,)
_TRY_NODES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
_LOOP_NODES = (ast.While, ast.For, ast.AsyncFor)
_COMP_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def is_irrefutable(pattern: ast.pattern) -> bool:
    """True for patterns that always match (``_``, a bare capture, or an or-pattern containing one)."""
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(is_irrefutable(p) for p in pattern.patterns)
    return False


def _is_decision_expr(node: ast.AST, strict: bool) -> bool:
    return isinstance(node, (ast.IfExp,) + _COMP_NODES) or (strict and isinstance(node, ast.BoolOp))


def decision_exprs(node: ast.AST, strict: bool) -> Iterator[ast.expr]:
    """Yield outermost branching expressions under ``node``, not descending into nested scopes."""
    for child in ast.iter_child_nodes(node):
        if _is_decision_expr(child, strict):
            yield child
        elif not isinstance(child, _SCOPE_NODES):
            yield from decision_exprs(child, strict)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({ast.unparse(node.args)})"


@dataclass
class _Loop:
    header: Block
    exit: Block
    lineno: int


@dataclass
class _Finally:
    """A try statement with a ``finally`` clause whose body or handlers are being built."""

    node: ast.Try
    depth: int       # index of this context in the finally stack
    loop_depth: int  # number of enclosing loops at the try statement
    copies: dict[str, Block] = field(default_factory=dict)  # exit kind -> finally copy


class _Builder:
    def __init__(self, cfg: CFG, source: str, strict: bool):
        self.cfg = cfg
        self.source = source
        self.lines = source.splitlines()
        self.strict = strict
        self.loops: list[_Loop] = []
        self.finallies: list[_Finally] = []

    def build(self) -> CFG:
        first = self.cfg.new_block()
        self.cfg.add_edge(self.cfg.entry, first)
        end = self._stmts(self.cfg.ast_body, first)
        if end is not None:
            self.cfg.add_edge(end, self.cfg.exit)
        self.cfg.finalize()
        return self.cfg

    # -- helpers ----------------------------------------------------------

    def _new(self) -> Block:
        return self.cfg.new_block()

    def _ensure(self, cur: Block | None) -> Block:
        # cur is None after return/break/continue/raise: code that follows is
        # unreachable, so it starts a fresh block with no incoming edge.
        return cur if cur is not None else self._new()

    def _join(self, block: Block) -> Block | None:
        if not self.cfg.preds(block):
            self.cfg.remove_block(block)
            return None
        return block

    def _text(self, node: ast.AST) -> str:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _signature(node) + ":"
        if isinstance(node, ast.ClassDef):
            return f"class {node.name}:"
        seg = ast.get_source_segment(self.source, node) or ast.unparse(node)
        return " ".join(seg.split())

    def _stmts(self, stmts: list[ast.stmt], cur: Block | None) -> Block | None:
        for stmt in stmts:
            cur = self._stmt(stmt, cur)
        return cur

    # -- jumps and finally routing -----------------------------------------

    def _target(self, kind: str) -> tuple[Block, str]:
        if kind in ("return", "raise"):
            return self.cfg.exit, "EXIT"
        loop = self.loops[-1]
        if kind == "break":
            return loop.exit, f"after loop L{loop.lineno}"
        return loop.header, f"loop L{loop.lineno}"

    def _enclosing_finally(self, kind: str) -> _Finally | None:
        if not self.finallies:
            return None
        ctx = self.finallies[-1]
        if kind in ("break", "continue") and ctx.loop_depth < len(self.loops):
            return None  # the target loop is inside the try, so finally is not crossed
        return ctx

    def _jump(self, cur: Block, kind: str, label: str | None = None) -> None:
        """Leave ``cur`` via return/raise/break/continue, passing through any finally on the way."""
        target, desc = self._target(kind)
        ctx = self._enclosing_finally(kind)
        if ctx is None:
            default = None if kind == "return" else kind
            self.cfg.add_edge(cur, target, label or default)
            return
        routed = f"{kind}→{desc}"
        self.cfg.add_edge(cur, self._finally_copy(ctx, kind, routed), f"{label} {routed}" if label else routed)

    def _finally_copy(self, ctx: _Finally, kind: str, routed: str) -> Block:
        if kind in ctx.copies:
            return ctx.copies[kind]
        block = self._new()
        block.add(self._finally_line(ctx.node), f"finally: ({routed})")
        ctx.copies[kind] = block
        # The finally body runs in the context of the try statement itself.
        saved = self.loops, self.finallies
        self.loops, self.finallies = self.loops[: ctx.loop_depth], self.finallies[: ctx.depth]
        end = self._stmts(ctx.node.finalbody, block)
        if end is not None:
            self._jump(end, kind)
        self.loops, self.finallies = saved
        return block

    # -- branching expressions --------------------------------------------

    def _values(self, cur: Block, node: ast.AST | None) -> Block:
        """Expand branching expressions inside ``node`` into blocks that run before ``node``."""
        if node is None or isinstance(node, _SCOPE_NODES):
            return cur
        if _is_decision_expr(node, self.strict):
            return self._expand(cur, node)
        for sub in decision_exprs(node, self.strict):
            cur = self._expand(cur, sub)
        return cur

    def _expand(self, cur: Block, node: ast.expr) -> Block:
        if isinstance(node, ast.IfExp):
            return self._ifexp(cur, node)
        if isinstance(node, ast.BoolOp):
            return self._boolop_value(cur, node)
        return self._comprehension(cur, node)

    def _ifexp(self, cur: Block, node: ast.IfExp) -> Block:
        then_b, else_b, join = self._new(), self._new(), self._new()
        self._cond(cur, node.test, then_b, else_b, f"ifexp: {ast.unparse(node.test)}", node.lineno)
        for block, value in ((then_b, node.body), (else_b, node.orelse)):
            block = self._values(block, value)
            block.add(value.lineno, f"ifexp → {ast.unparse(value)}")
            self.cfg.add_edge(block, join)
        return join

    def _comprehension(self, cur: Block, node: ast.expr) -> Block:
        # Each ``for`` clause is a loop nested in the previous one: when it is
        # exhausted control returns to the enclosing clause's header.
        join = self._new()
        exhausted = join
        for gen in node.generators:
            cur = self._values(cur, gen.iter)
            header = self._new()
            self.cfg.add_edge(cur, header)
            keyword = "async for" if gen.is_async else "for"
            header.add(gen.iter.lineno, f"comp: {keyword} {ast.unparse(gen.target)} in {ast.unparse(gen.iter)}")
            body = self._new()
            self.cfg.add_edge(header, body, "T")
            self.cfg.add_edge(header, exhausted, "F")
            body = self._values(body, gen.target)
            for test in gen.ifs:
                nxt = self._new()
                self._cond(body, test, nxt, header, f"comp: if {ast.unparse(test)}", test.lineno)
                body = nxt
            cur, exhausted = body, header
        if isinstance(node, ast.DictComp):
            cur = self._values(self._values(cur, node.key), node.value)
            text = f"{ast.unparse(node.key)}: {ast.unparse(node.value)}"
        else:
            cur = self._values(cur, node.elt)
            text = ast.unparse(node.elt)
        cur.add(node.lineno, f"comp → {text}")
        self.cfg.add_edge(cur, exhausted)
        return join

    def _boolop_value(self, cur: Block, node: ast.BoolOp) -> Block:
        is_and = isinstance(node.op, ast.And)
        opname = "and" if is_and else "or"
        join = self._new()
        last = len(node.values) - 1
        for i, value in enumerate(node.values):
            cur = self._values(cur, value)
            cur.add(value.lineno, ast.unparse(value) if i == 0 else f"{opname} {ast.unparse(value)}")
            if i == last:
                self.cfg.add_edge(cur, join)
                break
            nxt = self._new()
            if is_and:
                self.cfg.add_edge(cur, nxt, "T")
                self.cfg.add_edge(cur, join, "F")
            else:
                self.cfg.add_edge(cur, join, "T")
                self.cfg.add_edge(cur, nxt, "F")
            cur = nxt
        return join

    def _cond(self, cur: Block, test: ast.expr, t: Block, f: Block, text: str, lineno: int) -> None:
        """Emit a T/F branch on ``test`` at the end of ``cur``.

        In strict mode an and/or test becomes a chain of single-operand tests.
        """
        if self.strict and isinstance(test, ast.BoolOp):
            is_and = isinstance(test.op, ast.And)
            opname = "and" if is_and else "or"
            last = len(test.values) - 1
            for i, value in enumerate(test.values):
                vtext = text if i == 0 else f"{opname} {ast.unparse(value)}"
                vline = lineno if i == 0 else value.lineno
                if i == last:
                    self._cond(cur, value, t, f, vtext, vline)
                    break
                nxt = self._new()
                if is_and:
                    self._cond(cur, value, nxt, f, vtext, vline)
                else:
                    self._cond(cur, value, t, nxt, vtext, vline)
                cur = nxt
            return
        cur = self._values(cur, test)
        cur.add(lineno, text)
        self.cfg.add_edge(cur, t, "T")
        self.cfg.add_edge(cur, f, "F")

    # -- statements -------------------------------------------------------

    def _stmt(self, stmt: ast.stmt, cur: Block | None) -> Block | None:
        if isinstance(stmt, ast.If):
            return self._if(stmt, cur, "if")
        if isinstance(stmt, ast.While):
            return self._while(stmt, cur)
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            return self._for(stmt, cur)
        if isinstance(stmt, _TRY_NODES):
            return self._try(stmt, cur)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self._with(stmt, cur)
        if isinstance(stmt, ast.Match):
            return self._match(stmt, cur)
        if isinstance(stmt, ast.Assert):
            return self._assert(stmt, cur)

        cur = self._ensure(cur)
        if not isinstance(stmt, _DEF_NODES):
            cur = self._values(cur, stmt)
        cur.add(stmt.lineno, self._text(stmt))
        jump = {ast.Return: "return", ast.Raise: "raise", ast.Break: "break", ast.Continue: "continue"}
        kind = jump.get(type(stmt))
        if kind is not None:
            self._jump(cur, kind)
            return None
        return cur

    def _assert(self, stmt: ast.Assert, cur: Block | None) -> Block:
        cur = self._ensure(cur)
        ok_b, fail_b = self._new(), self._new()
        self._cond(cur, stmt.test, ok_b, fail_b, f"assert {ast.unparse(stmt.test)}", stmt.lineno)
        fail_b = self._values(fail_b, stmt.msg)
        fail_b.add(stmt.lineno, "raise AssertionError" + (f"({ast.unparse(stmt.msg)})" if stmt.msg else ""))
        self._jump(fail_b, "raise")
        return ok_b

    def _is_elif(self, node: ast.stmt) -> bool:
        line = self.lines[node.lineno - 1] if node.lineno <= len(self.lines) else ""
        return line[node.col_offset:].startswith("elif")

    def _if(self, stmt: ast.If, cur: Block | None, keyword: str) -> Block | None:
        cur = self._ensure(cur)
        then_b = self._new()
        after = self._new()
        else_b = self._new() if stmt.orelse else after
        self._cond(cur, stmt.test, then_b, else_b, f"{keyword} {ast.unparse(stmt.test)}:", stmt.lineno)

        end = self._stmts(stmt.body, then_b)
        if end is not None:
            self.cfg.add_edge(end, after)

        if stmt.orelse:
            orelse = stmt.orelse
            if len(orelse) == 1 and isinstance(orelse[0], ast.If) and self._is_elif(orelse[0]):
                end = self._if(orelse[0], else_b, "elif")
            else:
                end = self._stmts(orelse, else_b)
            if end is not None:
                self.cfg.add_edge(end, after)
        return self._join(after)

    def _loop_tail(self, stmt: ast.While | ast.For | ast.AsyncFor, header: Block,
                   body_b: Block, else_b: Block, after: Block) -> Block | None:
        self.loops.append(_Loop(header, after, stmt.lineno))
        end = self._stmts(stmt.body, body_b)
        if end is not None:
            self.cfg.add_edge(end, header)
        self.loops.pop()
        if stmt.orelse:
            # The else clause runs only when the loop ends without break.
            end = self._stmts(stmt.orelse, else_b)
            if end is not None:
                self.cfg.add_edge(end, after)
        return self._join(after)

    def _while(self, stmt: ast.While, cur: Block | None) -> Block | None:
        header = self._new()
        if cur is not None:
            self.cfg.add_edge(cur, header)
        body_b, after = self._new(), self._new()
        else_b = self._new() if stmt.orelse else after
        self._cond(header, stmt.test, body_b, else_b, f"while {ast.unparse(stmt.test)}:", stmt.lineno)
        return self._loop_tail(stmt, header, body_b, else_b, after)

    def _for(self, stmt: ast.For | ast.AsyncFor, cur: Block | None) -> Block | None:
        cur = self._values(self._ensure(cur), stmt.iter)
        header = self._new()
        self.cfg.add_edge(cur, header)
        keyword = "async for" if isinstance(stmt, ast.AsyncFor) else "for"
        header.add(stmt.lineno, f"{keyword} {ast.unparse(stmt.target)} in {ast.unparse(stmt.iter)}:")
        body_b, after = self._new(), self._new()
        else_b = self._new() if stmt.orelse else after
        self.cfg.add_edge(header, body_b, "T")
        self.cfg.add_edge(header, else_b, "F")
        body_b = self._values(body_b, stmt.target)
        return self._loop_tail(stmt, header, body_b, else_b, after)

    def _finally_line(self, stmt: ast.Try) -> int:
        first = stmt.finalbody[0].lineno
        for ln in range(first, stmt.lineno, -1):
            if self.lines[ln - 1].lstrip().startswith("finally"):
                return ln
        return first

    def _try(self, stmt: ast.Try, cur: Block | None) -> Block | None:
        # Approximation: every handler is reachable from the start of the try
        # body, and a raise always leaves the function (via any finally).
        try_b = self._new()
        if cur is not None:
            self.cfg.add_edge(cur, try_b)
        try_b.add(stmt.lineno, "try:")
        after = self._new()
        final_b = self._new() if stmt.finalbody else None
        cont = final_b or after

        if final_b is not None:
            self.finallies.append(_Finally(stmt, len(self.finallies), len(self.loops)))

        end = self._stmts(stmt.body, try_b)
        if stmt.orelse:
            end = self._stmts(stmt.orelse, end)
        if end is not None:
            self.cfg.add_edge(end, cont)

        star = "*" if type(stmt).__name__ == "TryStar" else ""
        for handler in stmt.handlers:
            exc = ast.unparse(handler.type) if handler.type is not None else ""
            label = f"except{star} {exc}".rstrip()
            header = label + (f" as {handler.name}" if handler.name else "") + ":"
            hb = self._new()
            self.cfg.add_edge(try_b, hb, label)
            hb = self._values(hb, handler.type)
            hb.add(handler.lineno, header)
            end = self._stmts(handler.body, hb)
            if end is not None:
                self.cfg.add_edge(end, cont)

        if final_b is not None:
            self.finallies.pop()
            # The fall-through copy of finally exists only if something falls into it;
            # exits via return/raise/break/continue already have their own copies.
            if self.cfg.preds(final_b):
                final_b.add(self._finally_line(stmt), "finally:")
                end = self._stmts(stmt.finalbody, final_b)
                if end is not None:
                    self.cfg.add_edge(end, after)
            else:
                self.cfg.remove_block(final_b)
        return self._join(after)

    def _with(self, stmt: ast.With | ast.AsyncWith, cur: Block | None) -> Block | None:
        cur = self._ensure(cur)
        for item in stmt.items:
            cur = self._values(cur, item)
        keyword = "async with" if isinstance(stmt, ast.AsyncWith) else "with"
        cur.add(stmt.lineno, f"{keyword} {', '.join(ast.unparse(i) for i in stmt.items)}:")
        return self._stmts(stmt.body, cur)

    def _match(self, stmt: ast.Match, cur: Block | None) -> Block | None:
        cur = self._values(self._ensure(cur), stmt.subject)
        cur.add(stmt.lineno, f"match {ast.unparse(stmt.subject)}:")
        after = self._new()
        case_b = self._new()
        self.cfg.add_edge(cur, case_b)
        last = len(stmt.cases) - 1
        for i, case in enumerate(stmt.cases):
            header = f"case {ast.unparse(case.pattern)}"
            if case.guard is not None:
                header += f" if {ast.unparse(case.guard)}"
            header += ":"
            lineno = case.pattern.lineno
            body_b = self._new()
            if is_irrefutable(case.pattern) and case.guard is None:
                case_b.add(lineno, header)
                self.cfg.add_edge(case_b, body_b)
                next_b = None
            else:
                next_b = after if i == last else self._new()
                if case.guard is not None:
                    self._cond(case_b, case.guard, body_b, next_b, header, lineno)
                else:
                    case_b.add(lineno, header)
                    self.cfg.add_edge(case_b, body_b, "T")
                    self.cfg.add_edge(case_b, next_b, "F")
            end = self._stmts(case.body, body_b)
            if end is not None:
                self.cfg.add_edge(end, after)
            if next_b is None:
                break  # an irrefutable case must be last
            case_b = next_b
        return self._join(after)


def build_cfgs(tree: ast.Module, source: str, strict: bool = False) -> list[CFG]:
    """Build the module-level CFG followed by one CFG per function, in source order."""
    module = CFG(MODULE_NAME, 1, tree.body)
    cfgs = [_Builder(module, source, strict).build()]

    funcs: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = prefix + child.name
                funcs.append((qualname, child))
                visit(child, qualname + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    funcs.sort(key=lambda item: (item[1].lineno, item[1].col_offset))

    seen: dict[str, int] = {}
    for qualname, node in funcs:
        # Redefinitions (e.g. property setters) share a name; disambiguate by line.
        name = qualname if qualname not in seen else f"{qualname}@L{node.lineno}"
        seen[qualname] = seen.get(qualname, 0) + 1
        cfg = CFG(name, node.lineno, node.body, _signature(node))
        cfgs.append(_Builder(cfg, source, strict).build())
    return cfgs
