# Playbook: decomposing a Python god-module

## Choosing seams

`inventory.py` clusters symbols by intra-module references (weakly connected components).
Good targets share a *responsibility* and have few edges out. Use these signals:

1. **Leaves first.** Symbols with fan-out 0 inside the module (listed under "Leaves")
   can move anywhere without dragging dependencies. Extract them first to shrink the
   problem and to warm up the gate.
2. **Cut by responsibility, not by size.** Typical targets in a god-module:
   `types.py`/`models.py` (dataclasses, enums, exceptions), `io.py` (file/network),
   `service.py` (orchestration), `validation.py`, `render.py`. A target module should
   be describable in one sentence.
3. **One giant component** is the common case: everything references everything through
   a few hub symbols (a config object, a base class, a logger). `inventory.py` handles this:
   it ranks symbols by fan-in, peels hubs until the largest component is under 40% of the
   definitions (`--exclude-hubs N` to force N), reports the components that remain, and
   also runs label-propagation communities for anything still too coarse. The plan's
   Wave 0 moves the peeled hubs and module-level state into `_core.py`; afterwards the
   clusters are extractable independently. If auto-peeling reaches `--max-hubs` (12) and
   still reports one dominant component, the module is coupled through *data flow* rather
   than a few names; use the Communities section and accept larger, fewer targets.
4. **Aim for 150–400 code lines per target module**, hard cap 500. Below 100 lines you
   are creating navigation overhead for agents; above 500 you have not finished.
5. **Order inside a cluster** = topological order of references (dependencies before
   dependents), so each intermediate state still compiles.

## Circular imports

Symptom: `ImportError: cannot import name X from partially initialized module`.
After a move, the new module imports something from `__init__.py`, which now imports
the new module. Resolution, in preference order:

1. **Move the shared symbol to its true home.** If `models.py` needs `DEFAULT_TIMEOUT`
   from `__init__.py`, the constant belongs in `models.py` (or `_constants.py`), not in
   the package root. Move it (rope) so the edge points one way.
2. **Type-only imports go under `TYPE_CHECKING`:**
   ```python
   from __future__ import annotations
   from typing import TYPE_CHECKING
   if TYPE_CHECKING:
       from .service import Service
   ```
   This changes no runtime behavior and is allowed during a split.
3. **Keep `__init__.py` thin.** The package root should contain re-exports and nothing
   that submodules import. Anything submodules need lives in a submodule.
4. **Function-local import** as a last resort, and it must be flagged in the extractor's
   report so the reviewer sees it. Lazy imports hide cycles; they do not remove them.

Never add an abstract base class, a protocol, a registry, or a plugin mechanism to
"solve" a cycle during a split. That is a design change and belongs in a separate PR.

## Module-level state

`inventory.py` lists mutable module-level assignments (`REGISTRY = {}`, `_cache = {}`,
`client = Client()`) and `global` statements. Rules:

- Each piece of state gets **exactly one owning module**. Everyone else imports it from
  there. Duplicating a dict across two modules silently forks the state.
- Move state **before** the functions that use it, into the module that will own it.
- Functions using `global NAME` must stay in the module that defines `NAME`, or the
  `global` must be replaced by explicit parameter passing in a *separate*, tested commit.
- Import-time side effects (`configure_logging()` at module top) stay in `__init__.py`
  unless the plan explicitly moves them and the reviewer confirms ordering is preserved.

## Re-exports (keep the public API stable)

After a wave, `<pkg>/<name>/__init__.py` must re-export every public symbol that moved
so that `from pkg.name import Thing` still works for callers outside the repo:

```python
from .models import Model as Model, Store as Store   # explicit alias form satisfies ruff F401
from .io import save as save
__all__ = ["Model", "Store", "save"]
```

Rope already rewrote in-repo imports to the new locations, so re-exports serve external
users and stability. Do not re-export private names (`_helper`). If the project has no
external consumers and the user says so, re-exports can be skipped and `__init__.py` left
empty; say which choice was made in the final report.

## Lock the shape (so agents cannot re-tangle it)

Add an import-linter contract for the new package. Example `.importlinter`:

```ini
[importlinter]
root_package = app

[importlinter:contract:engine-layers]
name = engine package layering
type = layers
layers =
    app.engine.service
    app.engine.io
    app.engine.models
    app.engine._state

[importlinter:contract:engine-no-cycles]
name = engine submodules are independent of __init__
type = forbidden
source_modules = app.engine.models, app.engine.io, app.engine.service
forbidden_modules = app.engine
```

Run `lint-imports` in the gate (verify.sh does this when the config exists) and in CI.
`tach` is an equivalent alternative (`tach mod` to declare, `tach check` to enforce).

## Reading large files without paying for them

Claude Code's Read tool truncates around 2,000 lines / 25K tokens per call and silently
returns partial content. Never `Read` a god-module whole. Use:

- `.refactor/inventory.md` for the map;
- `Grep -n "^def \|^class " <file>` for the index;
- `Read` with `offset`/`limit` for one symbol at a time (line ranges are in the inventory);
- `git diff --stat` and `git diff <file>` on the *moved* module (small) rather than the source.

The approved models have 1M-token windows, so a 12K-line file (~120–160K tokens) would
technically fit. Do not do it anyway: quality degrades with context length ("context rot"),
the Read tool will not deliver it in one call, and the point of fan-out is to keep each
worker's working set small enough to reason about precisely.

## Before the first move: what rope must not see

Two things decide whether rope can do a clean move in a given repository, and both are
preflight facts, not extractor judgment calls:

1. **Importers the re-exports keep valid.** After Phase 3 the package root re-exports every
   moved name, so files that reach the module through `pkg.mod.name` attribute access
   (sibling modules, shims, tests that patch by attribute) need no rewrite. If rope can see
   them it rewrites them anyway, which edits bodies the oracle has frozen. List them in
   `.refactor/rope-ignore.txt` (one glob per line) before Wave 0.
2. **Files rope's parser cannot read.** `bash preflight.sh` prints the `.py` files rope's
   patched AST rejects; put them in the same ignore file. rope only needs to see the
   package being split plus the modules it imports from.

The tool guards the rest: `rope_move.py` restores bare names in the source, repairs the
destination's imports and lands moved statements in dependency order, and the strict body
oracle refuses anything that still differs.

