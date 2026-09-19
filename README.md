# refactor-python

A Claude Code plugin for safely decomposing oversized Python modules in agent-written
codebases, and for keeping them small afterwards.

It also decomposes classes, giant functions, and test classes through
`/refactor-python:decompose`. Class methods move with LibCST and retain bindings on
the original class; statement ranges extract through rope. A manifest oracle checks
the complete declared transformation, including class headers, decorators, imports,
and unchanged surrounding code. Test moves additionally compare exact unittest/pytest
IDs and shard memberships. Unsafe constructs are refused before writing.

| Operation | Guarantee | Command |
|---|---|---|
| Whole top-level relocation | Existing AST body oracle | `split-module` |
| Method relocation with class bindings; test mixins/sibling classes | Tier 1: manifest-checked structure, with behavior and ID gates | `decompose` / `move_methods.py` |
| Statement extraction; explicit-parameter closure hoist | Tier 2: exact declared rewrite plus tests | `decompose` / `extract_ranges.py` |
| State redesign or broader recomposition | Tier 3: method allow-list, tests, two reviews and operator approval | Separate reviewed commit |

Structural equality does not prove general Python behavior equivalence. Reflection,
global lookup and descriptors need census review and behavior tests even for tier 1.

**What it enforces:** whole-module relocations use rope; method moves use LibCST with
manifest verification. Every step must pass a gate (compile, ruff, types, import contracts,
AST body-hash oracle, tests) before it is committed; extraction runs one cluster
at a time per module in git worktrees (every cluster rewrites the package root, so parallel
extraction of one module only conflicts) and merges each by commit SHA; finalize lands the
baseline, docs and import contract first, then a headless Codex (GPT-5.6 Sol, ultra) review
followed by a Fable 5.1 review adjudicate the finished tree, with a consensus round on
disagreements; a hook and CI stop files from regrowing.

## Install

```bash
# from a local checkout (development)
claude --plugin-dir ./refactor-python

# or as a project-scoped skills-directory plugin (auto-loads on next session)
mkdir -p .claude/skills && cp -r refactor-python .claude/skills/refactor-python
```

Then, in the repo you want to refactor:

```bash
bash "$CLAUDE_PLUGIN_ROOT/skills/split-module/scripts/preflight.sh" --install   # or let the skill do it
# Add --decompose for class/function moves (also requires LibCST).
```

Add to `.claude/settings.json` (preflight reminds you):

```json
{ "worktree": { "baseRef": "head" } }
```

Without it, extractor worktrees branch from your default branch instead of the refactor branch.

## Use

```
/refactor-python:split-module app/core/engine.py
/refactor-python:guardrails --max-lines 500
/refactor-python:decompose app/core/engine.py --class Engine
/refactor-python:decompose app/core/engine.py --function Engine.run
```

For several modules or a plan with many clusters:

```
/refactor-python:split-module-workflow   with args {"modules": ["app/core/engine.py", "app/api/handlers.py"]}
```
or just say "use a workflow to split app/core/engine.py".

For several class/function clusters, use `/refactor-python:decompose-workflow` with
`targets: [{source: "app/core/engine.py", className: "Engine"}]` and `pkgDir: "app"`.
The separate workflow keeps its resume sequence independent of split-module.

Quality is configured in `.refactor-quality.json`:

```json
{"module_target": 500, "module_ceiling": 500, "function_target": 150,
 "class_target": 30, "max_parameters": 6}
```

The planner aims at the target; pass the ceiling to the standalone guard with
`--max` or `REFACTOR_MAX_LINES` (also used by hooks and `verify.sh`). Projects may raise
these independently (for example target 1,000 / ceiling 3,000). Files between them
remain tracked debt with a reason and follow-up. Large functions and classes still
need decomposition even when their module fits the ceiling.

Project hooks remain project-owned: `REFACTOR_TEST_CMD` selects tests;
`REFACTOR_MINT_CMD` regenerates byte/digest pins when `verify.sh --mint` runs at wave
close or finalize. No project-specific minting paths are embedded in the new scripts.

### Worked example

The fixture in `skills/decompose/tests/fixture` contains a production class with
static/class methods, a property, a context manager, nested closures and an intentionally
unsupported private method, plus behavior tests and a sharded `load_tests` suite.
Copy it to a scratch git project with the fixture committed, set `D` to this plugin's
`skills/decompose/scripts`, then:

```bash
python3 "$D/move_methods.py" --source sample/engine.py --class Engine \
  --methods calculate,add,build,doubled,temporary,closure,nonlocal_closure \
  --dest sample/operations.py --manifest move.json
# The default is a verified dry run: neither destination nor manifest is written.
# Repeat with --apply, then:
python3 -m unittest discover -s tests
```

Measured code lines (excluding blanks/comments):

| File | Before | After |
|---|---:|---:|
| `sample/engine.py` | 49 | 26 |
| `sample/operations.py` | 0 | 27 |

Seven methods move; all five behavior tests still pass, including descriptor and
`patch.object(Engine, ...)` checks. The private method stays on the class. This small
fixture demonstrates mechanics, not a recommended production seam.
See [the operation contract](skills/decompose/references/operations.md) for test shapes,
refusals, manifest schemas, pinned IDs, namespace discovery, shard configuration and compact evidence.

## Layout

```
refactor-python/
├── .claude-plugin/plugin.json
├── skills/
│   ├── split-module/            # the orchestrator procedure
│   │   ├── SKILL.md
│   │   ├── references/          # toolchain, playbook, failure modes, model roles
│   │   └── scripts/             # preflight.sh, inventory.py, rope_move.py,
│   │                            # snapshot_bodies.py, check_file_length.py, verify.sh
│   └── guardrails/              # install ruff/pre-commit/import-linter/CI/CLAUDE.md guards
│       ├── SKILL.md
│       └── templates/
├── agents/                      # split-planner (Fable 5.1), plan-critic (Opus 5),
│                                # extractor (Opus 4.8, worktree), gate-runner (Opus 4.8),
│                                # refactor-reviewer (Fable 5.1)
├── workflows/split-module.js    # dynamic workflow for big/multi-module jobs
└── hooks/hooks.json             # PostToolUse file-size guard
```

`skills/decompose/` adds `SKILL.md`, `references/operations.md`, inventories,
`move_methods.py`, `extract_ranges.py`, `manifest_oracle.py`, `collect_tests.py`,
quality configuration, and the executable fixture test suite.
`workflows/decompose.js` carries manifest and test-ID evidence through orchestration.

## Models

Planner and reviewer: `claude-fable-5-1`. Critic: `claude-opus-5` (deliberately not the
planner's model). Extractors and gate runner: `claude-opus-4-8`, escalating to Opus 5 on
repeated gate failure. Rationale and overrides: `skills/split-module/references/model-roles.md`.

## Second-opinion review (Codex)

Phase 7a runs `scripts/codex_review.sh` (detached from the orchestrator's shell on large splits; it outlives a subagent's turn budget), which follows the operational playbook of the
[codex-orchestrator](https://github.com/alexzh3/codex-orchestrator) plugin (install it too;
its skill is the reference for locating the binary, run modes, and resume/consensus).
Requires Codex CLI or the IDE extension, signed in. `ultra` needs the ChatGPT backend; on
API-key providers the script falls back to `xhigh`.

## Requirements

Python ≥ 3.10, git, and: rope, LibCST, ruff, pytest, mypy or pyright (required); import-linter,
grimp, radon, vulture (recommended). `preflight.sh --install` installs the Python ones
with uv or pip.

Run the plugin's own regression suite with `python -m pip install -r requirements-dev.txt`
and `python -m pytest -q`. CI runs it on Python 3.10 and 3.13.

## Validate the plugin

```bash
claude plugin validate ./refactor-python --strict
```
