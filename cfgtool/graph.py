"""CFG data structures: basic blocks, labelled edges and graph-level utilities."""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field

ENTRY = "entry"
EXIT = "exit"
NORMAL = "normal"


@dataclass
class Block:
    """A basic block: a maximal run of straight-line statements.

    ``items`` holds ``(lineno, source_text)`` pairs. Compound statements
    contribute their header (``if x:``, ``for i in y:``) as the item that
    ends the block they branch from.
    """

    id: int
    kind: str = NORMAL
    items: list[tuple[int, str]] = field(default_factory=list)
    reachable: bool = True

    def add(self, lineno: int, text: str) -> None:
        self.items.append((lineno, text))

    @property
    def line_range(self) -> tuple[int, int] | None:
        if not self.items:
            return None
        lines = [ln for ln, _ in self.items]
        return min(lines), max(lines)


@dataclass(frozen=True)
class Edge:
    src: int
    dst: int
    label: str | None = None


class CFG:
    """A control flow graph with a single ENTRY and a single EXIT block.

    Edges form a multigraph: two edges between the same pair of blocks
    (e.g. both branches of ``if x: pass`` ending at the same join) are kept,
    since collapsing them would distort E - N + 2P.
    """

    def __init__(self, name: str, lineno: int, ast_body: list[ast.stmt], signature: str = ""):
        self.name = name
        self.lineno = lineno
        self.ast_body = ast_body
        self.signature = signature
        self.blocks: dict[int, Block] = {}
        self.edges: list[Edge] = []
        self._next_id = 0
        self.entry = self.new_block(ENTRY)
        self.exit = self.new_block(EXIT)

    # -- construction -----------------------------------------------------

    def new_block(self, kind: str = NORMAL) -> Block:
        block = Block(self._next_id, kind)
        self.blocks[block.id] = block
        self._next_id += 1
        return block

    def add_edge(self, src: Block, dst: Block, label: str | None = None) -> None:
        self.edges.append(Edge(src.id, dst.id, label))

    def remove_block(self, block: Block) -> None:
        del self.blocks[block.id]
        self.edges = [e for e in self.edges if e.src != block.id and e.dst != block.id]

    def preds(self, block: Block) -> list[Edge]:
        return [e for e in self.edges if e.dst == block.id]

    def succs(self, block: Block) -> list[Edge]:
        return [e for e in self.edges if e.src == block.id]

    # -- post-processing --------------------------------------------------

    def finalize(self) -> None:
        self._drop_empty_blocks()
        self._mark_reachable()
        self._renumber()

    def _drop_empty_blocks(self) -> None:
        """Splice out empty pass-through blocks left over from construction.

        An empty block with one successor contributes one node and one edge,
        so removing it leaves E - N unchanged. Cycles of empty blocks cannot
        occur (every loop has a header item), so one pass suffices.
        """
        for block in list(self.blocks.values()):
            if block.kind != NORMAL or block.items:
                continue
            outs = self.succs(block)
            if len(outs) > 1 or any(e.dst == block.id for e in outs):
                continue
            if not outs and self.preds(block):
                continue
            target = outs[0].dst if outs else None
            out_label = outs[0].label if outs else None
            new_edges = []
            for e in self.edges:
                if e.src == block.id:
                    continue
                if e.dst == block.id:
                    new_edges.append(Edge(e.src, target, e.label or out_label))
                else:
                    new_edges.append(e)
            self.edges = new_edges
            del self.blocks[block.id]

    def _mark_reachable(self) -> None:
        adj: dict[int, list[int]] = {bid: [] for bid in self.blocks}
        for e in self.edges:
            adj[e.src].append(e.dst)
        seen = {self.entry.id}
        queue = deque([self.entry.id])
        while queue:
            for nxt in adj[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for bid, block in self.blocks.items():
            # EXIT is always drawn as live, even for functions that never return.
            block.reachable = bid in seen or block.kind == EXIT

    def _renumber(self) -> None:
        mapping = {old: new for new, old in enumerate(sorted(self.blocks))}
        self.blocks = {mapping[old]: b for old, b in self.blocks.items()}
        for new, block in self.blocks.items():
            block.id = new
        self.edges = [Edge(mapping[e.src], mapping[e.dst], e.label) for e in self.edges]

    # -- metrics ----------------------------------------------------------

    @property
    def num_nodes(self) -> int:
        return len(self.blocks)

    @property
    def num_edges(self) -> int:
        return len(self.edges)

    def connected_components(self) -> int:
        """Number of weakly connected components (P in E - N + 2P)."""
        parent = {bid: bid for bid in self.blocks}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for e in self.edges:
            a, b = find(e.src), find(e.dst)
            if a != b:
                parent[a] = b
        return len({find(bid) for bid in self.blocks})
