# Operation contract

All paths are project-relative. Run from the project root. `--import-root` identifies
the Python import root when it differs from the project root (for example `src`).
Dependencies: Python 3.10+, rope, LibCST, pytest, and the existing lint/type toolchain.

Keep all evidence (snapshots, manifests, inventories, critiques, type deltas) under
the tracked `.refactor/` directory with unique names. Never store it in scratch or
temp directories that the environment may prune mid-run.

## Inventory and quality

```bash
python3 "$D/class_inventory.py" src/pkg/engine.py --class Engine --repo-root .
python3 "$D/function_inventory.py" src/pkg/engine.py --function Engine.run
python3 "$D/quality.py" src/pkg/engine.py src/pkg/_operations.py --debt .refactor/debt.json
```

Inventory data-flow and the repo census are conservative candidates. Dynamic dispatch,
aliases and runtime reflection cannot be exhaustively resolved. The census includes
sites referencing the class or visible aliases, not unrelated patch calls. Method
and shared-state hubs are peeled before clustering, using split-module's graph helpers.
Inspect reported hubs separately. Seeds are a JSON
object from seam name to method names. Do not force disconnected methods into a
cluster just because they share a seed.

## Tier 1 method moves

```bash
python3 "$D/move_methods.py" --project . --source src/pkg/engine.py \
  --class Engine --methods run,observe --dest src/pkg/_operations.py \
  --import-root src --shape function --manifest .refactor/run-move.json
# Inspect the dry-run diff and manifest; repeat with --apply.
```

Function shape moves undecorated functions and applies the original decorators in
the source class's binding, e.g. `run = _operations.run` or
`build = classmethod(_operations.build)`. Sibling imports use `from . import _operations`.
Known decorators are `staticmethod`,
`classmethod`, `property`, and `contextmanager` / `contextlib.contextmanager`.
A project may declare its own plain function-wrapper decorators (no descriptor
semantics) in `.refactor-quality.json` as `plain_decorators`, written as they appear in
the class body; they are re-applied in the class binding like `contextmanager`.
Property setters/deleters, stacked/undeclared decorators, name mangling, `super`,
`__class__`, namespace introspection, global writes, class-dependent signatures,
metaclasses, decorated classes and slots are refused. Nested `nonlocal` inside an
otherwise unchanged method can travel with the method; hoisting that closure is refused.

Imports are copied from their owning module, resolving relative imports. Existing
source imports whose last internal reader moved are removed (literal `__all__` exports
are preserved). For façade roots with an external import contract, opt into
`--retain-module-api` to keep these imports with targeted F401 markers. Any global
still defined in the source must be relocated first, preventing source/destination
cycles. Production class bases are unchanged. Reflected `__module__`, `__qualname__`,
source locations and pickling of method objects may change; inspect census findings.

For test shapes, provide `--test-only --test-snapshot .refactor/tests.json` plus
`--shape mixin --target-class ObservationMixin` (non-discovered support file) or
`--shape class --target-class TestObservation --id-map mapping.json` (new test file).
Applying class shape requires that mapping file. Sibling classes must
have no local class state and no calls into remaining methods: put shared helpers
in an existing support base first. Mixins must not match test class patterns.
Custom discovery patterns are recorded by the pytest collector and honored by the
method mover, alongside the configured unittest pattern.

Real test files often define shared constants and helpers in their preamble. Inventory
reports these as `prerequisites`; a refusal names the global, method and line. First
relocate those top-level symbols with split-module's `rope_move.py` to a non-discovered
support module, preserving their source imports where callers need them. This is a
separate preparation commit, not a body edit. Check initialization order and globals
such as `__file__` before relocating constants. Recollect IDs and re-freeze before the
method move. Do not import the source test module from its extracted support module.
For namespace tests, run the preparatory rope move from the tests directory with
that directory as `--project`; its bare support imports then match unittest's top level.

## Tier 2 extraction

```bash
python3 "$D/extract_ranges.py" --source src/pkg/engine.py --function Engine.run \
  --start 120 --end 145 --name calculate_totals --manifest .refactor/totals.json
python3 "$D/extract_ranges.py" --source src/pkg/engine.py --function Engine.run \
  --nested calculate --name calculate_explicit --manifest .refactor/closure.json
```

Add `--dest src/pkg/_calculations.py --import-root src` for a separate destination.
Ranges use the baseline's inclusive line numbers and must cover whole consecutive
statements of one block of the selected function; the block may be the body of a nested
`if`/`for`/`with`/`try` statement. A `break`/`continue` whose loop is outside the range is refused. Rope infers parameters and outputs; the oracle independently
checks the extracted statements, optional return, and exact source call/assignment.
No similar-occurrence rewriting. Partial loops, return/yield/await/global/nonlocal and
nested definitions in a range are refused. Complete loops are subject to rope's checks.
Hoisting supports plain synchronous closures with positional parameters and direct calls;
no defaults, annotations, decorators, nonlocal/global writes, recursion, escaping functions,
star calls, calls from another lexical scope or further nested functions. Captured objects become explicit parameters,
retaining object identity; tests must check effects. Excess parameters mean re-plan.

## Manifest and oracle

```json
{
  "version": 1,
  "tier": 1,
  "base_commit": "immutable git commit recorded on apply",
  "baseline": {"pkg/engine.py": "sha256 of source", "pkg/_ops.py": "sha256 of empty text"},
  "operations": [{
    "kind": "move", "source": "pkg/engine.py", "dest": "pkg/_ops.py",
    "class": "Engine", "methods": ["run"], "shape": "function",
    "alias": "_ops", "target_class": null, "test_only": false,
    "imports": {"pkg/engine.py": ["from . import _ops"], "pkg/_ops.py": []}
  }]
}
```

One operation per manifest; a move may contain several methods. Extract operations
declare `function`, `start_line`, `end_line`, `name`, `parameters`, `outputs`, and
`imports`. Hoists declare `function`, `nested`, `name`, `free`, and `imports`.
Newly unused source imports are declared in `remove_imports`. Import formatting is off
by default. If the project gate enforces isort/import ordering (including Ruff's `I`
rules), pass `--format-imports` to both dry-run and apply invocations of either mover
for every cluster; otherwise unsorted generated imports fail that cluster's gate.
The flag runs ordinary ruff import sorting and records `format_imports: true`. In that mode the
oracle ignores top-level import grouping/order only; import bindings must still match
and any nested body edit is rejected. No source headers are embedded in manifests.
Manifests are reviewable declarations, not untrusted certificates: scrutinize the
parameters, imports, and ID maps. The oracle rebuilds the expected complete AST from
the frozen sources, so there is no class-wide waiver and no output-hash escape hatch.
Comments/formatting are preserved by LibCST but not proven by the AST oracle; docstrings
are checked in manifest mode. Legacy snapshot comparisons keep their existing semantics.

```bash
python3 "$S/snapshot_bodies.py" snapshot src/pkg --out .refactor/cluster-before.json
python3 "$S/snapshot_bodies.py" compare .refactor/cluster-before.json src/pkg \
  --manifest .refactor/run-move.json --strict
```

Snapshots retain the legacy body hashes and add compact per-file digests, never source
text. Manifest comparison reconstructs touched baseline files from `base_commit` in
git and checks the digests. Legacy snapshots still support ordinary split-module
comparison, including `--allow-changed a,b` for top-level functions.
The per-cluster snapshot scope must equal the gate's `--pkg` path (`src/pkg` in this
example). A snapshot that also covers tests makes the manifest oracle fail on the
first test edit. Supply this same scope to compare, including new destinations.
Sequential manifests use sequential snapshots. Preserve them with their commit SHAs.
At review, run each comparison at its recorded output commit in a worktree. Compare
the final sources against those reviewed outputs; any later change needs declared proof.
There is no separate replay tool.
For tier 3, `--tier3 --base <baseline-sha> --allow-changed Engine.run` permits only that method body to
change; its signature, decorators, class header and every other statement stay checked.
Class names are never valid allow-list entries. Broader recomposition requires a
separate reviewed structural plan, not expansion of this body waiver.

## Test identity and hooks

```bash
python3 "$D/collect_tests.py" snapshot --start tests --out .refactor/tests.json --shards shards.json --pinned pinned.json
python3 "$D/collect_tests.py" compare .refactor/tests.json --mode identity
python3 "$D/collect_tests.py" compare .refactor/tests.json --mode mapping --manifest .refactor/move.json
```

The default `--preset auto` uses the project as unittest's top level for packaged
tests and the start directory for a namespace `tests/` without `__init__.py`.
`--preset namespace` explicitly selects the latter; `--top tests` is its direct
equivalent. Use `--top` for other layouts. The resolved top level and discovery
patterns are recorded: do not change them between snapshots. Namespace unittest IDs
therefore start with `test_engine`, not `tests.test_engine`; use that spelling in
shard module lists. Both import roots are available during collection.

`shards.json` maps shard names to collector overrides:
`{"0": {"modules": ["tests.test_engine"], "env": {"TEST_SHARD": "0"}}}`.
Each runs unittest's loader (including `load_tests`) in an isolated process. Pytest
uses collected node IDs via its plugin API. Collection errors and duplicate IDs fail.
Mapping manifests add `test_id_map: {"unittest": {"old.id": "new.id"},
"pytest": {"old.py::Class::test": "new.py::Class::test"}}`.
Mappings must be injective, all other IDs identical, counts and shards exact.
`pinned.json` maps framework names to pinned ID lists. Pinned IDs must never appear
as mapping keys; comparison refuses them. Collection imports project code; only run
it in the authorized project.

`verify.sh` accepts `--manifest`, `--test-snapshot`, `--test-mode`, and `--mint`.
Set `REFACTOR_TEST_CMD` for focused tests and `REFACTOR_MINT_CMD` for project-specific
digest regeneration. The mint hook runs only with `--mint`, before full tests, and a
nonzero hook status fails the gate. No project paths or digest formats live in scripts.

Dry runs write no files. Apply requires committed source/destination inputs, validates
the proposed tree in memory, checks source digests and refuses existing manifest
outputs before writing. Writes are ordinary worktree edits, not an atomic transaction.
On I/O failure stop and inspect the git diff; recover only the affected paths, preserving
unrelated work. Test-ID and full behavior gates must pass before committing.

Finalize owns lint configuration for moved code: existing source-path per-file ignores
may need corresponding destination entries. Review each inherited finding; do not
blanket-ignore new defects or hand-edit tier-1 bodies to satisfy lint. Header sorting
must be opted into at planning time so the manifest gate covers it.
Do not add a Ruff `I001` ignore for destination modules, whether per file or by glob:
it silences the mover's `--format-imports` result. A temporary glob for relocated
complexity codes is fine; finalize replaces it with measured per-module entries.
