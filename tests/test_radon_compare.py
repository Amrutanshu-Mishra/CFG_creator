"""Tests for --compare-radon gap classification."""

import ast
import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

from cfgtool.__main__ import main
from cfgtool.complexity import analyze, radon_known_differences
from cfgtool.parser import build_cfgs

try:
    import radon  # noqa: F401
    HAVE_RADON = True
except ImportError:
    HAVE_RADON = False

TRY_ELSE = textwrap.dedent("""\
    def load(path):
        try:
            f = open(path)
        except OSError:
            return None
        else:
            data = f.read()
        return data
""")

LAMBDA_OR = textwrap.dedent("""\
    def pick(items):
        return sorted(items, key=lambda x: x or 0)
""")


def _function_cfg(source: str):
    cfgs = build_cfgs(ast.parse(source), source)
    return cfgs[1]  # cfgs[0] is <module>


def _run_cli(source: str, *flags: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "input.py"
        path.write_text(source)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([str(path), "--out", str(Path(tmp) / "out"), "--compare-radon", *flags])
    return rc, out.getvalue(), err.getvalue()


class TryExceptElseTest(unittest.TestCase):
    def test_known_differences_count_try_else(self):
        cfg = _function_cfg(TRY_ELSE)
        self.assertEqual(radon_known_differences(cfg.ast_body),
                         {"and/or": 0, "try-else": 1, "loop-else": 0})

    def test_cfg_complexity(self):
        # One except handler is the only decision; the else clause adds no branch.
        result = analyze(_function_cfg(TRY_ELSE))
        self.assertEqual((result.m_graph, result.m_ast), (2, 2))

    @unittest.skipUnless(HAVE_RADON, "radon not installed")
    def test_gap_is_fully_explained_and_not_warned(self):
        rc, out, err = _run_cli(TRY_ELSE)
        self.assertEqual(rc, 0)
        row = next(line.split() for line in out.splitlines() if line.startswith("load"))
        self.assertEqual(row[4:7], ["2", "2", "3"])  # M, M(ast), radon
        self.assertNotIn("warning: load", err)
        self.assertIn("1 function(s) differ from radon only for known reasons: try-else clauses +1 in 1", err)

    @unittest.skipUnless(HAVE_RADON, "radon not installed")
    def test_json_reports_breakdown(self):
        import json
        _, out, _ = _run_cli(TRY_ELSE, "--json")
        load = next(f for f in json.loads(out)["functions"] if f["name"] == "load")
        self.assertEqual(load["radon_explained"], {"try-else": 1})
        self.assertEqual(load["radon_unexplained"], 0)


class UnexplainedGapTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_RADON, "radon not installed")
    def test_lambda_gap_is_warned_with_amount(self):
        # radon counts the `or` inside the lambda; cfgtool does not graph lambda bodies.
        _, _, err = _run_cli(LAMBDA_OR, "--strict-boolean")
        self.assertIn("warning: pick: M=1 but radon M=2; unexplained +1", err)
        self.assertNotIn("note:", err)


if __name__ == "__main__":
    unittest.main()
