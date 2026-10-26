"""Command-line entry point: ``python -m cfgtool input.py --out outdir``."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from .complexity import RADON_REASONS, Complexity, analyze, radon_complexities, radon_known_differences
from .parser import build_cfgs


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cfgtool",
        description="Build control flow graphs for a Python file and compute McCabe complexity.",
    )
    p.add_argument("input", type=Path, help="Python source file to analyse")
    p.add_argument("--out", type=Path, default=Path("cfg_out"), help="output directory (default: cfg_out)")
    p.add_argument("--format", choices=("png", "svg"), default="png", help="image format (default: png)")
    p.add_argument("--strict-boolean", action="store_true",
                   help="count each extra and/or operand as a decision point")
    p.add_argument("--json", action="store_true", help="print results as JSON instead of a table")
    p.add_argument("--compare-radon", action="store_true",
                   help="add radon's complexity as a column and warn where it differs")
    return p.parse_args(argv)


def _table(results: list[Complexity], with_radon: bool) -> str:
    headers = ["Function", "Line", "N", "E", "M", "M(ast)"] + (["radon"] if with_radon else []) + ["Risk"]
    rows = []
    for r in results:
        radon = ["-" if r.m_radon is None else r.m_radon] if with_radon else []
        rows.append([r.name, r.lineno, r.nodes, r.edges, r.m_graph, r.m_ast, *radon, r.risk])
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    left = {0, len(headers) - 1}

    def fmt(row: list) -> str:
        cells = [str(c).ljust(w) if i in left else str(c).rjust(w)
                 for i, (c, w) in enumerate(zip(row, widths))]
        return "  ".join(cells).rstrip()

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def _compare_radon(source: str, cfgs: list, results: list[Complexity], strict: bool) -> bool:
    """Fill in radon's numbers, explain each gap, and warn only on unexplained ones.

    Returns False if radon is unavailable.
    """
    try:
        radon = radon_complexities(source)
    except ImportError:
        print("cfgtool: error: --compare-radon needs radon (pip install -r requirements.txt)", file=sys.stderr)
        return False
    explained_funcs = 0
    totals = dict.fromkeys(RADON_REASONS, 0)    # summed counts over fully explained functions
    affected = dict.fromkeys(RADON_REASONS, 0)  # how many of those functions each reason touched
    for cfg, r in zip(cfgs, results):
        r.m_radon = radon.get(r.name if r.name == "<module>" else r.lineno)
        if r.m_radon is None:
            continue
        known = radon_known_differences(cfg.ast_body, strict)
        r.radon_explained = {k: v for k, v in known.items() if v}
        r.radon_unexplained = r.m_radon - r.m_graph - sum(known.values())
        if r.m_radon == r.m_graph:
            continue
        if r.radon_unexplained == 0:
            explained_funcs += 1
            for reason, n in r.radon_explained.items():
                totals[reason] += n
                affected[reason] += 1
            continue
        detail = ", ".join(f"{RADON_REASONS[k]} +{v}" for k, v in r.radon_explained.items())
        print(f"cfgtool: warning: {r.name}: M={r.m_graph} but radon M={r.m_radon}; "
              f"unexplained {r.radon_unexplained:+d}" + (f" (explained: {detail})" if detail else "")
              + " (see README, 'Differences from radon')", file=sys.stderr)
    if explained_funcs:
        parts = [f"{RADON_REASONS[k]} +{totals[k]} in {affected[k]}" for k in RADON_REASONS if totals[k]]
        print(f"cfgtool: note: {explained_funcs} function(s) differ from radon only for known reasons: "
              + "; ".join(parts) + (" (use --strict-boolean to count and/or)" if totals["and/or"] else ""),
              file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        source = args.input.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(args.input))
    except OSError as exc:
        print(f"cfgtool: cannot read {args.input}: {exc}", file=sys.stderr)
        return 1
    except SyntaxError as exc:
        print(f"cfgtool: syntax error in {args.input}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return 1

    cfgs = build_cfgs(tree, source, strict=args.strict_boolean)
    results = [analyze(cfg, args.strict_boolean) for cfg in cfgs]

    for r in results:
        if r.mismatch:
            print(f"cfgtool: warning: {r.name}: graph M={r.m_graph} (E-N+2P) differs from "
                  f"AST M={r.m_ast} (decisions+1)", file=sys.stderr)

    if args.compare_radon and not _compare_radon(source, cfgs, results, args.strict_boolean):
        return 1

    outputs: dict[str, dict[str, str | None]] = {}
    try:
        from .render import render
    except ImportError:
        print("cfgtool: warning: the 'graphviz' Python package is not installed; skipping graph output "
              "(pip install -r requirements.txt)", file=sys.stderr)
    else:
        missing_dot = False
        for cfg, r in zip(cfgs, results):
            dot_path, image_path = render(cfg, r, args.out, args.format)
            missing_dot = missing_dot or image_path is None
            outputs[r.name] = {"dot": str(dot_path), "image": str(image_path) if image_path else None}
        if missing_dot:
            print(f"cfgtool: warning: Graphviz 'dot' executable not found; wrote .dot files only "
                  f"to {args.out}", file=sys.stderr)

    if args.json:
        payload = {
            "file": str(args.input),
            "strict_boolean": args.strict_boolean,
            "functions": [{**r.to_dict(), **outputs.get(r.name, {})} for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_table(results, args.compare_radon))
    return 0


if __name__ == "__main__":
    sys.exit(main())
