"""Render a CFG to Graphviz DOT (and PNG/SVG when the ``dot`` binary is available)."""

from __future__ import annotations

import re
from pathlib import Path

import graphviz

from .complexity import Complexity
from .graph import ENTRY, EXIT, CFG, Block

MAX_LINE = 60
MAX_ITEMS = 14

EDGE_STYLES = {
    "T": {"color": "#2e7d32", "fontcolor": "#2e7d32"},
    "F": {"color": "#c62828", "fontcolor": "#c62828"},
    "break": {"color": "#1565c0", "fontcolor": "#1565c0", "style": "dashed"},
    "continue": {"color": "#1565c0", "fontcolor": "#1565c0", "style": "dashed"},
    "raise": {"color": "#6a1b9a", "fontcolor": "#6a1b9a", "style": "dashed"},
    "return": {"color": "#37474f", "fontcolor": "#37474f"},
}
EXCEPT_STYLE = {"color": "#ef6c00", "fontcolor": "#ef6c00", "style": "dashed"}


def _truncate(text: str, limit: int = MAX_LINE) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(text: str) -> str:
    # Source text may contain backslashes, which DOT would read as escapes.
    return text.replace("\\", "\\\\")


def _left(lines: list[str]) -> str:
    return "".join(_escape(line) + "\\l" for line in lines)


def _block_label(block: Block) -> str:
    rng = block.line_range
    head = f"B{block.id}" + (f"  L{rng[0]}" + (f"-{rng[1]}" if rng[1] != rng[0] else "") if rng else "")
    lines = [head]
    for lineno, text in block.items[:MAX_ITEMS]:
        lines.append(_truncate(f"{lineno:>4}: {text}"))
    if len(block.items) > MAX_ITEMS:
        lines.append(f"      … (+{len(block.items) - MAX_ITEMS} more)")
    return _left(lines)


def safe_filename(name: str) -> str:
    if name == "<module>":
        return "__module__"
    return re.sub(r"[^\w.@-]", "_", name)


def build_digraph(cfg: CFG, result: Complexity) -> graphviz.Digraph:
    title = f"{cfg.name}   N={result.nodes} E={result.edges} M={result.m_graph} ({result.risk})"
    g = graphviz.Digraph(safe_filename(cfg.name))
    g.attr(label=_escape(title), labelloc="t", fontname="Helvetica", fontsize="14")
    g.attr("node", fontname="Courier", fontsize="10", shape="box", style="rounded,filled", fillcolor="#fafafa")
    g.attr("edge", fontname="Helvetica", fontsize="9")

    for block in cfg.blocks.values():
        node_id = str(block.id)
        if block.kind == ENTRY:
            label = "ENTRY" + (f"\\n{_escape(_truncate(cfg.signature))}" if cfg.signature else "")
            g.node(node_id, label, shape="ellipse", style="filled,bold",
                   fillcolor="#c8e6c9", fontname="Helvetica")
        elif block.kind == EXIT:
            g.node(node_id, "EXIT", shape="doublecircle", style="filled,bold",
                   fillcolor="#ffcdd2", fontname="Helvetica")
        elif not block.reachable:
            g.node(node_id, _block_label(block) + "(unreachable)\\l", style="rounded,dashed",
                   color="#9e9e9e", fontcolor="#757575")
        else:
            g.node(node_id, _block_label(block))

    for edge in cfg.edges:
        attrs: dict[str, str] = {}
        if edge.label:
            attrs["label"] = _escape(_truncate(edge.label, 32))
            # Routed jumps look like "return→EXIT" or "F raise→EXIT"; style by the first word.
            key = edge.label.split()[0].split("→")[0]
            attrs.update(EXCEPT_STYLE if key.startswith("except") else EDGE_STYLES.get(key, {}))
            if "→" in edge.label:
                attrs["style"] = "bold"
        g.edge(str(edge.src), str(edge.dst), **attrs)
    return g


def render(cfg: CFG, result: Complexity, outdir: Path, fmt: str = "png") -> tuple[Path, Path | None]:
    """Write ``<name>.dot`` and, if possible, ``<name>.<fmt>``.

    Returns (dot_path, image_path); image_path is None when the Graphviz
    ``dot`` executable is not installed.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    base = safe_filename(cfg.name)
    g = build_digraph(cfg, result)
    dot_path = outdir / f"{base}.dot"
    dot_path.write_text(g.source, encoding="utf-8")
    try:
        data = g.pipe(format=fmt)
    except graphviz.ExecutableNotFound:
        return dot_path, None
    image_path = outdir / f"{base}.{fmt}"
    image_path.write_bytes(data)
    return dot_path, image_path
