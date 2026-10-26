# cfgtool

Builds a control flow graph (CFG) for every function, method and the module-level
code in a Python file, draws each one with Graphviz, and computes McCabe's
cyclomatic complexity.

## Install

```bash
pip install -r requirements.txt     # the `graphviz` Python package
```

Rendering PNG/SVG also needs the Graphviz `dot` executable
(`sudo dnf install graphviz`, `sudo apt install graphviz`, `brew install graphviz`).
Without it, cfgtool still writes the `.dot` files and prints a warning.

Requires Python 3.10+ (for `match`).

## Usage

```bash
python -m cfgtool input.py --out outdir [--format png|svg] [--strict-boolean] [--json] [--compare-radon]
```

| Option             | Meaning                                                        |
|--------------------|----------------------------------------------------------------|
| `--out DIR`        | Output directory (default `cfg_out`)                           |
| `--format`         | `png` (default) or `svg`                                       |
| `--strict-boolean` | Treat each extra `and`/`or` operand as a decision (short-circuit branch) |
| `--json`           | Print JSON instead of the table                                |
| `--compare-radon`  | Add radon's complexity as a column and warn where it differs (see [Differences from radon](#differences-from-radon)) |

Example (`python -m cfgtool examples/sample.py --out examples/cfg_out --compare-radon`):

```
Function        Line   N   E  M  M(ast)  radon  Risk
--------------  ----  --  --  -  ------  -----  ----
<module>           1   5   5  2       2      2  low
branches           7  11  13  4       4      4  low
loops             17  14  17  5       5      9  low
exc               39   9  10  3       3      4  low
fin_loop          90  18  25  9       9      6  low
exprs            113  20  27  9       9      9  low
```

`M` is the graph value and `M(ast)` is the AST value. The risk bands are 1–10 low,
11–20 moderate, 21–50 high, and >50 very high.

For each function, `outdir/<qualname>.dot` and `outdir/<qualname>.png` are written.
Module-level code is written to `__module__.*`. `examples/cfg_out/` has the rendered
graphs for `examples/sample.py`.

## How it works

* `cfgtool/parser.py`: walks the `ast` and builds basic blocks. Each CFG has
  one ENTRY and one EXIT. Nested functions and methods get their own CFG
  (`Class.method`, `outer.inner`). A `def`/`class` statement is one statement in its
  enclosing graph.
* `cfgtool/graph.py`: blocks, labelled edges (a multigraph), empty-block cleanup,
  reachability, and connected components.
* `cfgtool/complexity.py`: computes complexity two ways and warns if they differ:
  * graph: `M = E - N + 2P`
  * AST: `M = decision_points + 1`

  It also computes radon's number for `--compare-radon`.
* `cfgtool/render.py`: Graphviz output. Each node shows its line range and the
  statements it contains. Edges are labelled `T`/`F`, `except …`, `break`,
  `continue` or `raise`. Bold edges labelled `kind→target` lead into a copy of a
  `finally` block. Unreachable blocks are drawn dashed and grey.

### Decision points

| Construct                    | Counts as                                                   |
|------------------------------|-------------------------------------------------------------|
| `if` / `elif`                | 1 each                                                      |
| `while`, `for` (incl. async) | 1 each (`else` clauses add none; `break` skips them)        |
| `x if c else y`              | 1                                                           |
| comprehension / generator    | 1 per `for` clause + 1 per `if` clause                      |
| `assert`                     | 1 (the failing branch raises `AssertionError`)              |
| `try`                        | 1 per `except` handler                                      |
| `try … finally`              | decisions inside `finally` count once per copy (see below)  |
| `match`                      | 1 per `case`, except a final irrefutable `case _:` with no guard |
| `and` / `or`                 | `len(operands) - 1` per expression, only with `--strict-boolean` |

In the graph, an if-expression is a T/F diamond. A comprehension is drawn as nested
loops: each `for` clause is a header, and each `if` clause branches back to it.
Comprehensions are drawn inline, where they appear.

### try / finally

A `return`, `raise`, `break` or `continue` inside a `try` body, `except` handler or
`else` of a try with a `finally` runs the `finally` body before reaching its real
target. cfgtool draws one copy of the `finally` body for each kind of exit that occurs,
plus one for falling through normally. Each copy has a header like
`finally: (break→after loop L19)`, and the edge into it has the same label.
- Nested `try/finally` blocks chain: an inner copy jumps into the outer try's copy.
- `break`/`continue` aimed at a loop inside the `try` don't cross the `finally`.
- If every path through the `try` jumps away, no fall-through copy is drawn, so a
  `finally` never appears unreachable.

Because each copy is a real path, a decision inside `finally` counts once per copy.
The AST side computes the same number on its own, from which exit kinds occur and
whether the `try` can complete normally.

### Approximations

* Each `except` handler is reachable from the start of the try body, not from each
  statement in it.
* `raise` (and a failing `assert`) goes to EXIT through any enclosing `finally`. It does
  not go to a matching `except` handler of the same `try`.
* Lambda bodies are not graphed or counted. They run when called, not where they are
  written.
* Generator expressions are drawn as if evaluated immediately.
* Class-body statements other than method definitions are not graphed.

## Differences from radon

`--compare-radon` puts radon's cyclomatic complexity next to ours and explains each gap.
For every function, cfgtool counts the reasons radon is known to come out higher:

| Reason | Counted per function | Why cfgtool doesn't count it |
|--------|----------------------|------------------------------|
| and/or operands | `len(operands) - 1` per `and`/`or` expression (outside `assert` and lambdas), only without `--strict-boolean` | cfgtool counts `and`/`or` only with `--strict-boolean`; radon always does. |
| try-else clauses | +1 per `try` with an `else` | The `else` runs after the try body completes normally. That is straight-line flow, not a branch. |
| for/while-else clauses | +1 per loop with an `else` | The `else` is just where the loop condition's F edge goes, so it adds no branch. Whether `break` is taken is already counted by the `if` around it. |

If these reasons account for the whole gap (`radon = M + reasons`), no per-function
warning is printed. They are summed in one note instead, for example:

```
cfgtool: note: 4 function(s) differ from radon only for known reasons: and/or operands +6 in 3; try-else clauses +1 in 1; for/while-else clauses +2 in 1 (use --strict-boolean to count and/or)
```

If a gap remains, cfgtool warns with the unexplained amount and the reasons it did
account for:

```
cfgtool: warning: _signature_get_user_defined_method: M=6 but radon M=8; unexplained +1 (explained: and/or operands +1) (see README, 'Differences from radon')
```

With `--json`, each function also gets `radon_explained` (reason → count) and
`radon_unexplained` (`radon - M - sum(radon_explained)`).

### Known causes of unexplained gaps

These differences are also intentional but are not classified, so they show up as
"unexplained":

| Cause | Sign | Why |
|-------|------|-----|
| decisions inside a `lambda` | + (radon higher) | radon adds them to the enclosing function. A lambda body runs only when called, so its branches don't belong in this function's graph. |
| decisions inside `finally` | − (cfgtool higher) | Each exit kind gets its own copy of `finally` (see above), and each copy has its own paths. radon counts them once. |
| decisions inside `assert` | − (cfgtool higher) | radon counts the `assert` itself and nothing inside it. cfgtool also counts if-expressions, comprehensions and (strict) `and`/`or` inside the test and message. |
| `except*` handlers | − (cfgtool higher) | radon ignores `TryStar`. cfgtool counts each handler like `except`. |
| guarded wildcard cases | − (cfgtool higher) | radon treats `case _ if x:` (or any bare-name case) as free. cfgtool counts the guard as a branch. |

Opposite-signed causes can cancel. For example, an `or` inside an `assert` (cfgtool +1)
can offset an `or` inside a lambda (radon +1), so the gap looks fully explained.

Test runs: `examples/sample.py` and the stdlib modules `argparse`, `ast`, `typing`,
`inspect` and `tarfile`, with and without `--strict-boolean`. Every warning came
from the lambda or finally causes above, and none from the builder.

radon also has no number for methods of classes defined inside a function. Those rows
show `-`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The radon tests are skipped if radon is not installed.
