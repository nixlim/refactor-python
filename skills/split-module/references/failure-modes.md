# Failure modes and the gate that catches them

Published studies of LLM refactoring (Pomian et al. 2025; CoRenameAgent 2026; Horikawa
et al. 2025) report hallucination rates up to ~80% for freehand move-method suggestions
and a majority of agent refactorings tangled into unrelated commits. Every rule in this
skill exists to neutralize one of the rows below.

| Failure | How it shows up | Caught by | Prevented by |
|---|---|---|---|
| Dropped code | A helper vanishes during a "move"; tests that don't cover it stay green | `snapshot_bodies.py compare` → `MISSING` | rope moves whole definitions; agents never cut/paste |
| Silent behavior edit | Agent "cleans up" a body while moving it | `compare` → `CHANGED`; reviewer | "never edit bodies" rule; `--strict-bodies` at finalize |
| Invented code | Agent writes a replacement instead of moving | `compare` → `ADDED` | rope; extractor brief lists exact symbols |
| Broken imports elsewhere | A caller in another package still imports the old path | `compileall`, type ratchet, tests | rope rewrites project-wide imports |
| Undefined name after post-processing | Bare name left by de-qualification, dropped import, self-import | type ratchet (`type_baseline.py check`: new `name-defined`/`attr-defined` vs baseline) | name-scoped de-qualification; import-only edits |
| Circular import | `partially initialized module` at import time | tests (import fails), `lint-imports` | playbook §Circular imports; thin `__init__` |
| Forked module state | Two modules each define `REGISTRY = {}` | reviewer (inventory lists state); tests if covered | single-owner rule; move state first |
| Lazy-import cover-up | Extractor hides a cycle with a function-local import | reviewer (must be flagged in report) | rule: flag every local import |
| Partial read of the file | Agent reasons over the first 2,000 lines as if whole | inventory-based planning; extractor never Reads whole file | playbook §Reading large files |
| Tangled commit | Refactor mixed with a bug fix or feature | reviewer; one-cluster-per-commit rule | commit message convention `refactor(<name>):` only |
| Merge conflict resolved by hand | Manual 3-way merge of a 10K-line file loses a hunk | `compare` after merge | rule: abort and re-extract on updated base |
| Tool side effects counted as edits | rope qualifies references / writes self-imports / reverses order; oracle reports CHANGED | `compare` → `CHANGED` (this is how it was found) | `rope_move.py` post-processing (import-line edits only, ast-validated) |
| Regrowth after split | The new modules balloon again next week | `check_file_length.py` in hook + CI; import-linter | guardrails skill; baseline removal at finalize |
| Design drift | Extractor adds an ABC/registry to make a move compile | reviewer | rule: no new abstractions during a split |
| Wrong base branch | Worktree branched from `main`, commits do not apply to the refactor branch | preflight NOTE; merge fails | `worktree.baseRef: "head"` in settings |

## Orchestration failure modes

Learned on a 19,300-line split (46 modules, 17 waves, 2026-09-14). None of these lose
code, because the oracle and the gate still hold; all of them lose hours.

| Failure | How it shows up | Caught by | Prevented by |
|---|---|---|---|
| Parallel extraction of one module | Every cluster rewrites the root's import block, so after the first merge every other branch conflicts and is re-extracted anyway | merge conflicts on `__init__.py` | extract clusters of one source module **sequentially** from the merged HEAD; parallel only across different source modules |
| Merge by reported branch name | An extractor reports the base branch as its own; `git merge` says "Already up to date", the gate runs on the unchanged tree and returns PASS with nothing merged | HEAD unchanged after "merge" | merge the extractor's **commit SHA**; treat an ancestor as already merged; FAIL when HEAD does not move |
| Prose conflict cause | The gate-runner returns `cause: "git merge of abc conflicted in ..."` instead of the literal `conflict`; the script treats it as a hard failure and stops the wave | wave stops with clusters "failed" | match `/conflict/i` anywhere in the cause; gate-runner returns the literal |
| Resume replays a stale prefix | Workflow resume caching is a prefix of the agent() call sequence, not a key map: after any control-flow change every later call runs live, and cached extract results from an old tip are replayed and merged | re-extraction of symbols already moved; conflicts on already-merged clusters | prefer a **fresh launch** with `doneWaves`/`doneClusters` derived from `git ls-tree` of the package; remove the run's worktrees first |
| Worktree number reuse | A resumed instance restarts its worktree numbering and reuses an existing `worktree-<run>-N` branch, so the "fresh" extraction starts from a stale base | merge conflict on a cluster extracted "from HEAD" | remove every worktree and branch of the run before any relaunch |
| Long job inside a subagent | The full test gate (7 min) or the headless Codex review (30 min) exhausts the hosting subagent's turn budget; the job dies with the agent | agent "completed without calling StructuredOutput"; no output file | run long jobs **detached** (`nohup setsid ... > log &` + a Monitor on the exit line) or as their own single-command agent; hand results to the workflow via args |
| Extractor appends to `__all__` | rope or the extractor adds moved private names to `__all__`; the shim's forwarding contract changes | `__all__` length/AST compare against baseline at wave close and finalize | rule: `__all__` verbatim; the close step diffs it |
| Extractor edits CHANGELOG | A cluster commit carries a one-line changelog entry; the branch ends with N stale entries | review | rule: extractors touch only source, destination and import sites; the orchestrator writes one entry at finalize |
| Layout-pinning test | A test asserts `fn.__globals__ is vars(package)` or patches `package.os`; the move breaks the identity, not the behavior | focused tests | repoint the pin at the owning submodule (or the singleton) **in the same commit as the move**, with the operator's standing permission for layout pins |
| Stale byte pins | Any later edit to a moved file (even restoring `__all__`) invalidates digest pins such as FR-230 subject hashes; only the full suite notices | full-suite gate, reviewer | re-mint pins in the **same commit** as any edit to a pinned file; make that part of the close checklist |
| Target over budget with its header | The plan estimated 461 code lines; the file lands at 510 with imports | `file-length` gate | the plan names a **fallback seam** per target; the orchestrator applies it as two clusters |
| Review before finalize | Reviewers block on the baseline ceilings and the missing spec/contract wave that finalize is about to write; the recorded verdict is stale | REJECT/REVISE on items the next step fixes | **finalize first, then review the finished tree** |
| Self-kill by `pkill -f` | The tool shell is `zsh -c "<full command text>"`; a `pkill -f <pattern>` whose pattern appears in the same command kills the shell (exit 144) before anything runs | no output, exit 144 | list with `pgrep -fa` first and kill by PID; never put the pattern in the same command as the kill |

## Escalation is part of the design

A failed gate is information, not an emergency. The ladder in SKILL.md (resume → stronger
model → re-plan smaller → stop) bounds the cost of any one cluster and keeps the main
context free of debugging noise: the extractor debugs in its own context; the
orchestrator only sees the JSON report.
