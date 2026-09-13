# Toolchain for splitting Python modules

`scripts/preflight.sh` checks all of this automatically. This file explains *why* each
tool is required and what to do when one is unavailable.

| Tool | Role in the split | Install | Verify | Fallback |
|---|---|---|---|---|
| **rope** (required) | Deterministic move of a top-level symbol between modules; rewrites every import in the project. Driven by `scripts/rope_move.py`. | `pip install rope` / `uv pip install rope` | `python3 -c "import rope; print(rope.VERSION)"` | Serena MCP (`move symbol` via LSP) if installed and the user prefers it; or LibCST codemod for repetitive, rule-based moves. Never "fallback" to cut-and-paste. |
| **ruff** (required) | Lint gate; `ruff check --fix` for unused/duplicate imports after moves; complexity rules (`C901`, `PLR09xx`) in guardrails. | `pip install ruff` | `ruff --version` | `flake8` + `isort` + `autoflake`, slower and three tools instead of one. |
| **pytest** (required) | Behavior gate. | `pip install pytest` | `pytest --version` | The project's own runner via `REFACTOR_TEST_CMD` (e.g. `python -m unittest`). |
| **mypy** or **pyright** (one required) | Type gate; catches moved symbols that are now unresolved, wrong relative imports, and re-exports that shadow. | `pip install mypy` or `pip install pyright` | `mypy --version` / `pyright --version` | If the project has no type annotations at all, the gate still runs `python -m compileall` and ruff; type gate is skipped with a SKIP line. |
| **import-linter** (optional, recommended) | Enforces layer/independence/no-cycle contracts; stops the new package from re-tangling. | `pip install import-linter` | `lint-imports --version` | `tach` (`pip install tach`; `tach check`). |
| **grimp** (optional) | Import-graph queries when the planner needs "who imports whom" across the repo. | `pip install grimp` | `python3 -c "import grimp"` | `pydeps --show-deps`, or `inventory.py`'s regex-based `external_dependents`. |
| **radon** (optional) | Rank functions by cyclomatic complexity to decide extraction priority. | `pip install radon` | `radon cc --version` | `ruff check --select C901 --statistics`. |
| **vulture** (optional) | Dead code before splitting: do not migrate corpses. | `pip install vulture` | `vulture --version` | `ruff check --select F401,F841` (imports/locals only). |
| **git** (required) | Worktrees for parallel extractors, one commit per cluster, cheap rollback. | system | `git --version` | none. |
| **jq** (optional) | Faster JSON parsing in hooks. | system | `jq --version` | scripts use Python's json when jq is absent. |

## Why rope and not "just edit the file"

Rope resolves the symbol, its decorators, and its references; it rewrites
`from pkg.big import X` in every module of the project to `from pkg.big.models import X`;
it refuses moves it cannot resolve instead of guessing. `rope_move.py` prints a
dry-run description first so the extractor can review the touched-file list before
applying. Rope needs to see the whole project (`--project .`), which is why the
extractor runs it from the repo root inside its worktree.

Known rope behaviors that `rope_move.py` post-processes automatically (found while
dogfooding on a 19K-line module; keep these in mind when reading a dry run):

- **rope qualifies remaining references in the source** (`name(...)` becomes
  `pkg.mod.dest.name(...)` plus `import pkg.mod.dest`). That edits every referencing
  body and trips the `bodies-unchanged` oracle. The script restores bare names for the
  moved symbols only and writes `from pkg.mod.dest import name, ...`; `--keep-qualified-refs`
  disables this.
- **rope writes `from <source> import ...` into the destination** listing every source
  global the moved code might need, including the names just moved there (a load-time
  cycle) and names the source root only re-exports. The script drops self-imports and
  points re-exported names at their owner module.
- **rope inserts each moved definition at the top of the destination**, so a batch lands
  in reverse order (`B = A` above `A`). The script reorders into the requested order.
- **rope must be told what not to parse**: `--ignore glob,glob` or one glob per line in
  `.refactor/rope-ignore.txt` (generated code, vendored trees, importers that the
  package root's re-exports keep valid, and files rope's own parser cannot read: rope
  1.14's patched AST raises `AttributeError` in `_consume_pattern` on some modern string
  syntax). Without it rope may refuse a move outright or rewrite an importer that did not
  need rewriting. `preflight.sh` runs rope's parser over every `.py` file and lists the
  ones it cannot read so they can go straight into the ignore file. The `.txt` extension
  matters in repositories whose conformance checks require every path to match a file
  category.

Known rope limits and what to do:

- **Symbol uses module-level state via a bare name** (e.g. `REGISTRY[...]` inside a moved
  function): rope adds `from pkg.big import REGISTRY` to the destination, which creates a
  cycle if `pkg.big/__init__.py` re-exports from the destination. Move the state first to
  its owner module (see playbook §Module-level state).
- **Decorated symbols with decorators defined in the same module**: move the decorator
  first (it is a leaf) or in the same call, listed first in `--symbols`.
- **Name clash in destination**: rope aborts. Pick another destination or rename first
  (`rope` rename is exposed by Serena; or do it as a separate reviewed commit).
- **`__all__` in the source**: rope does not update it. The orchestrator fixes `__all__`
  in Phase 5 when adding re-exports.

## Serena MCP (optional accelerator)

If Serena is configured (`claude mcp list` shows it), extractors may use its
`find_symbol` / `find_referencing_symbols` to inspect usages without reading files, and
its `move` refactoring instead of `rope_move.py`. Keep the rest of the loop identical:
gate after every move, one cluster per commit. Do not mix rope and Serena moves within
one cluster; pick one per cluster so a failure has one cause.

## LibCST (optional, for repetitive transforms)

When a split reveals a pattern that must be applied dozens of times (e.g. replace
`big.CONFIG` with `config.CONFIG` in 40 files), write a small LibCST codemod instead of
40 edits. `pip install libcst`; template in the LibCST docs (`libcst.codemod.CodemodCommand`).
Codemods are reviewed like any code and gated like any move.
