---
name: decompose
description: Decompose oversized Python classes, functions, and test classes using manifest-verified method moves, statement extraction, and test-ID checks. Use split-module for whole top-level relocations.
---

# Decompose Python code

Use `$D = ${CLAUDE_PLUGIN_ROOT}/skills/decompose/scripts` and
`$S = ${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts` in the commands below.
Read [the operation contract](references/operations.md) before planning a move;
it defines the CLI, manifest format, supported subset, and refusal handling.

## Guarantees and scope

Report every operation's tier:

1. **Relocation:** LibCST moves methods without body changes. Function shape keeps
   bindings on the original class; mixin shape preserves inherited test identities.
2. **Declared rewrite:** rope extracts complete statement ranges; LibCST hoists
   supported nested functions with explicit closure parameters. The oracle reconstructs
   exactly the declared source replacement and extracted definition.
3. **Design change:** a separate, reviewed commit under operator approval. Use a
   method-only allow-list, tests, census and test IDs, and both independent reviews.

The oracle proves structural conformance, not general Python behavior equivalence.
Identical ASTs can resolve different globals or have different reflection metadata.
Tests and the census remain required for class relocation, including tier 1.
Never hand-edit bodies in tiers 1/2. A refusal is a planning result, not permission
to bypass the mover or widen a waiver.

## Quality configuration

One project file, `.refactor-quality.json`, owns these settings:

```json
{"module_target": 500, "module_ceiling": 500, "function_target": 150,
 "class_target": 30, "max_parameters": 6}
```

Plans aim at `module_target`. Pass `module_ceiling` to the standalone size guard with
`--max`, or export `REFACTOR_MAX_LINES` for hooks and `verify.sh`. The per-edit hook
does not import this skill or read project JSON; its default remains 500.
Run `$D/quality.py` over resulting files, with `--debt <json>` containing
`{"path.py": {"reason": "...", "follow_up": "issue or plan reference"}}`.
Modules between target and ceiling require this tracking. Any function over its
target or class over its method target remains an extraction candidate, never an
accepted final plan. Methods with neither shared state nor calls belong in separate
clusters. Test modules cover one scenario family; common helpers live in support files.

## Procedure

1. **Preflight and freeze.** Run `$S/preflight.sh --decompose`. Work on a clean feature branch;
   preserve unrelated user work. Establish passing behavior tests and type baseline.
   Snapshot the entire affected parent scope using `$S/snapshot_bodies.py snapshot`.
   For tests, use `$D/collect_tests.py snapshot`, including the project's shard settings.
   Record the baseline commit, source snapshot, test IDs, configuration, and census.
2. **Inventory.** Run `$D/class_inventory.py` and/or `$D/function_inventory.py`.
   Review method verdicts and the census; unresolved dynamic sites require investigation.
   Class inventory can take `--seeds` with proposed seam memberships. Resolve reported
   preamble prerequisites in a separate top-level relocation commit before moving
   test methods. Namespace test directories work with the collector's default auto preset.
3. **Plan and critique.** Delegate to `split-planner` then `plan-critic`, specifying
   decompose mode. Require CLI arguments, tier, exact methods/ranges, import owners,
   destination shape, expected test mappings, quality/debt report, and expected refusals.
   Import formatting defaults to off. If the project's gate enforces isort/import
   ordering (including Ruff's `I` rules), include `--format-imports` in every cluster's
   `move_methods.py` or `extract_ranges.py` invocation, for both dry run and apply.
   Otherwise unsorted generated imports fail the per-cluster gate; do not defer
   sorting to finalize or run an unmanifested auto-fix after extraction.
   Use dry runs to produce concrete manifests and diffs. Obtain plan approval when
   not already authorized. Tier 3 always requires operator approval of the concrete diff.
4. **Census preparation.** Inspect patches, unbound calls and reflection sites. Preserve
   class patch paths with bindings. Any necessary layout-test update is a separate,
   explicitly justified preparation commit before re-freezing; never weaken behavior tests.
5. **Extract sequentially per source.** Use `extractor` in a worktree. Freeze a fresh
   snapshot before each cluster, then execute the approved mover with `--apply`.
   A manifest is tied to this baseline, not the original entire-run snapshot. Store
   snapshot, manifest and commit SHA together; never overwrite earlier evidence.
   Run `verify.sh --manifest ... --snapshot ... --strict-bodies`, plus `--test-snapshot`
   and `--test-mode identity|mapping` for test code. One tier per commit. Merge by SHA,
   rerun the gate, and stop on failure. Re-extract on conflicts instead of resolving a move by hand.
6. **Wave close.** Verify every committed cluster against its own before/after trees.
   Check class headers against manifests, import contracts, source bindings, shard
   membership and quality debt. Invoke the project-owned `REFACTOR_MINT_CMD` via
   `verify.sh --mint` before full digest tests. Keep digest tests out of the focused
   per-cluster test command if they require wave-end regeneration.
7. **Finalize.** Measure targets/ceilings and function/class sizes; report remaining
   debt with follow-ups. Update import contracts, measured baselines and documentation.
   Carry reviewed source-path lint exceptions to destination paths where appropriate;
   pre-existing findings are not permission to edit relocated bodies.
   Re-mint after any subject edit. Run the full behavior, type, import and test-ID gates.
8. **Independent reviews.** Run the existing Codex review, then `refactor-reviewer`
   adjudication, on the finalized tree. Require both verdicts and resolve disagreements;
   after fixes rerun gates and review the new tip. Do not report completion after a
   missing verdict or failed check. Hand back commits, tiers, manifests and debt.

Use `workflows/decompose.js` for orchestration. It is a separate workflow so its
agent-call sequence cannot invalidate split-module resume caches. Resume only with
verified cluster SHAs; retain per-cluster digest snapshots for review at those commits.

Production classes use **function shape**. Class-body bindings and method names stay
on the public class; no new production base classes. **Mixin shape** is only for
collected test classes outside the type-gated production package, preserving pinned
IDs and shard memberships. **Class shape** moves unpinned scenarios to real sibling
test classes and requires an explicit ID mapping. Tier 3 regrouping beyond these
mechanical rules is separate from the relocation commit.
