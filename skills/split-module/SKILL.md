---
name: split-module
description: >-
  Safely decompose an oversized Python module (anything over ~500 code lines, especially
  multi-thousand-line "god files") into a package of small modules, using deterministic
  tools (rope for moves, an AST body-hash oracle, a test/type/import gate) and parallel
  subagents in git worktrees. Use this whenever a Python file is too large, blocks parallel
  agent work, trips the file-size guard hook, or someone says "split", "break up",
  "decompose", "modularize", "this file is huge", or "extract into modules". Also use it
  proactively before adding code to a file that is already over budget.
argument-hint: "<path/to/module.py> [--max-lines N] [--dry-run]"
allowed-tools: Bash(python3 *), Bash(bash *), Bash(git *), Bash(ruff *), Bash(pytest *), Bash(mypy *), Bash(pyright *), Bash(lint-imports *), Read, Grep, Glob, Agent
---

# Split a Python god-module

You are the **orchestrator**. You plan, delegate, gate, and merge. You do **not**
cut-and-paste code by hand: every relocation goes through `rope_move.py`, and every
step must pass `verify.sh` before it is committed. The reason is empirical: freehand
LLM moves hallucinate at high rates (dropped helpers, silent edits, broken imports);
a deterministic tool plus an AST oracle cannot "forget" code.

All scripts live in `${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts/`. Define once:

```bash
S="${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts"
```

Target: `$ARGUMENTS` (a module path, e.g. `app/core/engine.py`). If no path was given,
find candidates with `python3 $S/check_file_length.py .` and ask which to split.

## Phase 0: Preflight (never skip)

1. `bash $S/preflight.sh` — if it reports missing REQUIRED tools, run
   `bash $S/preflight.sh --install`, then re-run. Do not proceed on PREFLIGHT FAILED.
   Read `references/toolchain.md` if a tool cannot be installed (it lists fallbacks).
2. Repo state must be: on a feature branch (`git switch -c refactor/split-<name>` if
   not), clean working tree, tests passing (`REFACTOR_TEST_CMD` or `pytest -q -x`).
   If tests already fail, stop and report; a red baseline makes the gate meaningless.
3. Confirm `.claude/settings.json` has `"worktree": {"baseRef": "head"}` (preflight
   prints a NOTE if not). Without it, extractor worktrees branch from the default branch
   and their commits will not apply to your refactor branch.

## Phase 1: Freeze behavior (the safety net)

```bash
mkdir -p .refactor
python3 $S/snapshot_bodies.py snapshot <pkg_dir> --out .refactor/before.json
python3 $S/check_file_length.py --write-baseline .refactor-baseline.json .
```

`<pkg_dir>` is the directory that will contain the resulting package (usually the
target module's parent directory). Commit both files: `git add .refactor .refactor-baseline.json && git commit -m "refactor: freeze baseline for split of <module>"`.

Characterization tests: run `pytest --co -q | grep -c <module_stem>` (or coverage if
configured). If the module has little or no test coverage of its public functions, first
delegate to a `general-purpose` subagent: "write characterization tests that pin the
current observable behavior of the public functions/classes in <module>; do not change
production code" and commit those tests. This is not optional for modules that touch I/O,
state, or money.

## Phase 2: Plan (delegate, then critique)

1. Run `python3 $S/inventory.py <module> --repo-root . --module-name <dotted.name> --json .refactor/inventory.json > .refactor/inventory.md`.
2. Delegate to the **`refactor-python:split-planner`** subagent (Fable 5.1). Give it the
   paths to `.refactor/inventory.md` and `.refactor/inventory.json` and the module path.
   It returns `.refactor/plan-<name>.md` with, for each target module: name, symbols
   (in dependency order), the single owner of each piece of module-level state, expected
   circular-import risks, and an extraction order grouped into **waves** (a wave =
   clusters that touch disjoint symbols and can run in parallel).
3. Delegate to **`refactor-python:plan-critic`** (Opus 5, a different model on purpose)
   to attack the plan. If it returns blocking findings, send them back to the planner
   once; if still blocked, stop and show the user both documents.
4. Print the final plan summary (target modules, waves, state owners) to the user. In an
   interactive session, wait for approval before Phase 3.

If `inventory.md` reports one dominant component even after hub peeling, re-run
`inventory.py --exclude-hubs 20` (or more) and give the planner both outputs; the planner
must not hand-cut a component, it must plan Wave 0 = hubs → `_core.py` and take the
post-peel clusters/communities from there. See `references/playbook.md` §"Choosing seams".

## Phase 3: Convert to a package (one mechanical step)

```bash
git mv <dir>/<name>.py <dir>/<name>/__init__.py
bash $S/verify.sh --pkg <pkg_dir> --snapshot .refactor/before.json
git commit -am "refactor(<name>): convert module to package (no code moved)"
```

Every existing `import <pkg>.<name>` and `from <pkg>.<name> import X` still resolves.
Nothing has moved yet. If the gate fails here, the repo had a latent problem; fix that
first (typical: a relative import inside the module, or a test importing by file path).

## Phase 4: Fan-out (parallel extraction in worktrees)

For each **wave** in the plan, spawn one **`refactor-python:extractor`** subagent
(Opus 4.8) per cluster, all in the same turn so they run concurrently. Each extractor
runs in its own git worktree (`isolation: worktree` is set on the agent). Give each one
exactly this brief; nothing else:

```
Cluster: <cluster name>
Source module: <dir>/<name>/__init__.py
Destination module: <dir>/<name>/<target>.py
Symbols to move, in this order: <A, B, C>
Snapshot: .refactor/before.json
Package dir for the gate: <pkg_dir>
Scripts: ${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts
Rules: move only with rope_move.py; never edit bodies; if you hit a circular import
follow references/playbook.md §Circular imports; commit on your worktree branch only
when verify.sh passes; report the JSON block described in your instructions.
```

Cap concurrency at the number of clusters in the wave or 8, whichever is smaller. More
than that just contends for CPU during test runs. Model context is not the constraint
here (all approved models have 1M windows); wall-clock and merge simplicity are.

Each extractor returns a JSON block: `{"cluster":..., "branch":..., "commit":..., "gate":"pass|fail", "moved":[...], "notes":...}`.

**Escalation ladder** (apply per cluster, do not skip rungs):
1. `gate: fail` once → resume that same extractor with the gate output and ask it to fix.
2. Fails again → spawn a fresh extractor for the cluster with `model: claude-opus-5`.
3. Fails again → send the cluster back to `split-planner` with the failure log; it must
   split the cluster smaller or reorder it. Re-run from step 1 with the new sub-clusters.
4. Still failing → stop the wave, keep what merged, report to the user with the log.

## Phase 5: Fan-in (merge leaves-first, re-gate, never hand-resolve)

Merge the wave's branches into your refactor branch **one at a time, in the plan's
order** (leaves first). After each merge:

```bash
git merge --no-ff <branch>            # if this reports conflicts: git merge --abort
bash $S/verify.sh --pkg <pkg_dir> --snapshot .refactor/before.json
```

On a merge conflict: **abort, do not resolve by hand.** The extraction is a deterministic
tool operation, so re-running it on the updated base is cheaper and safer than a manual
three-way merge of a 10K-line file. Spawn a new extractor for that cluster (same brief,
it now starts from the merged HEAD) and merge its result.

When the whole wave is merged, add the backwards-compatibility re-exports to
`<name>/__init__.py` for every public symbol that moved
(`from .<target> import A, B  # re-export` plus keep `__all__` accurate; use
`from .<target> import A as A` if ruff F401 complains), run the gate, commit:
`git commit -am "refactor(<name>): extract <targets> (wave N)"`.

Then delegate the next wave. Waves are sequential; clusters inside a wave are parallel.

## Phase 6: Two-model review (Codex first, then Fable adjudicates)

Self-review by the model family that did the work reproduces its blind spots, so the
review is heterogeneous and sequential:

**6a. Headless Codex review (GPT-5.6 Sol, reasoning effort `ultra`).**

```bash
bash $S/codex_review.sh --base <baseline-commit> --plan .refactor/plan-<name>.md --out .refactor/codex-review.md
```

The script encodes the operational details from the `codex-orchestrator` plugin (which
should be installed; `/codex-orchestrator:codex-orchestrator` is the reference if the
script needs adapting): locates the IDE-bundled binary when `codex` is not on PATH, runs
`codex exec` in `-s read-only -c approval_policy=never`, captures the final message with
`-o`, keeps the session so it can be resumed, and falls back to `xhigh` if the backend
rejects `ultra` (API-key providers do; ChatGPT-signed-in Codex accepts it). Do not read
the JSONL log; read only `.refactor/codex-review.md`. If Codex is unavailable, record
`CODEX: unavailable (<reason>)` in the final report and continue; do not skip 6b.

**6b. Fable adjudicates both the changes and Codex's verdict.** Delegate to
**`refactor-python:refactor-reviewer`** (Fable 5.1; deliberately not the extractor's
model) with: baseline commit, HEAD, the plan, `.refactor/before.json`, `$S`, and
`.refactor/codex-review.md`. It (1) checks the diff itself with the oracle and targeted
reads, (2) verifies every Codex BLOCKING finding against the code and marks it
CONFIRMED / REFUTED (with evidence) / UNVERIFIABLE, (3) adds findings Codex missed, and
(4) returns one consolidated verdict plus `ALLOWED_CHANGED`.

**6c. Consensus round (only on disagreement).** For each Codex finding the reviewer
REFUTED, run one round: `bash $S/codex_review.sh --followup <codex_thread> "<finding + reviewer's evidence>"`
(the thread id is at the bottom of `codex-review.md`). Codex answers AGREE / DISAGREE /
RETRACT with evidence. Hand that answer back to the same reviewer (resume it). A finding
both models still disagree on after one round is escalated to the user with both sides'
evidence; it is never silently dropped. Record every disagreement and its resolution in
`.refactor/consensus-<name>.md` (finding, Codex evidence, Fable evidence, outcome).

Fix blocking findings through the extractor → gate → merge loop, then re-run 6a and 6b on
the new HEAD (Codex resumes its thread, so the second pass is cheap).

## Phase 7: Finalize

1. Remove the split module's entries from `.refactor-baseline.json`
   (`python3 $S/check_file_length.py --write-baseline .refactor-baseline.json .`) and
   confirm `python3 $S/check_file_length.py .` passes for the new package.
2. If import-linter is configured, add a contract for the new package (see
   `references/playbook.md` §"Lock the shape") and run `lint-imports`.
3. Final `bash $S/verify.sh --pkg <pkg_dir> --snapshot .refactor/before.json --strict-bodies`.
   `--strict-bodies` means zero CHANGED bodies; if the reviewer approved specific
   changes (e.g. a `global` converted to a parameter), pass `--allow-changed a,b` instead.
4. Report: table of new modules with code-line counts, number of symbols moved, gate
   status, Codex verdict, Fable verdict, consensus record summary, and the exact commands a
   human can run to reproduce the gate.

## Rules that override everything above

- Never edit a function or class body during a split. If a body must change to break a
  cycle, that is a separate, later commit with its own tests.
- Never add an interface, base class, or new abstraction "to make the move work".
  Report the cycle instead (see playbook).
- Never merge a branch whose gate did not pass, and never skip the gate "because the
  change is tiny".
- Never read the whole god-module into your own context. Use inventory.md, Grep, and
  targeted line-range reads. Extractors do the same.
- One cluster per commit. A failed gate reverts exactly one commit.

## When to use a dynamic workflow instead

If the plan has more than ~6 clusters, or you are splitting several modules at once,
run the bundled workflow so orchestration lives in a script and not in your context:
`/refactor-python:split-module-workflow` (or say "use a workflow to split <module>").
It encodes exactly the phases above; see `workflows/split-module.js`.

## References (read when needed, not up front)

- `references/toolchain.md`: what each tool is for, install commands, fallbacks (Serena MCP, LibCST).
- `references/playbook.md`: seams, circular imports, module-level state, re-exports, locking the shape.
- `references/failure-modes.md`: the ways agent refactors go wrong and which gate catches each.
- `references/model-roles.md`: why each subagent runs on the model it does, the Codex review tier, and the escalation ladder.
