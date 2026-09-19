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
| Module-level statement left behind | A seam binding or marker loop (`runtime.X = f`, `for s in (...): setattr`) stays in the source root after its function moved; rope moves definitions only | reviewer (`rg -n '<name>\s*=' <pkg>/`); import-order probe | cluster note names the statement; wave close greps for every module-level statement of the moved names (engine split, 2026-09-18) |
| Re-export block copied into destinations | rope inserts the root's `from pkg._x import A as A, ...` re-export lines into every destination; the redundant-alias form hides F401, and the lines create intra-wave edges that break a layers contract | `lint-imports` on the package contract | finalize prunes every unused `from pkg._x import` name in submodule headers (headers only; oracle unaffected) |
| `__all__` grows during waves | extractors or rope append private names to `__all__`, changing the exported set the shim forwards | wave-close diff of the `__all__` literal vs the baseline | rule: `__all__` verbatim; the close step restores it from the baseline commit |
| Plain-assignment layout pin | a hermetic fixture sets a control by `module.engine.X = v` (not `patch.object`), so the E0 patch census misses it and the moved reader keeps the real value | extractor gate tests (e.g. "detached review did not complete") | census both `patch.object(ROOT, ...)` and `ROOT.X = ` assignments; repoint the fixture to sweep every binder |
| Critic turn cap | plan-critic exhausts `maxTurns` on a 60 KB plan and returns no verdict; the workflow fails on a missing StructuredOutput | workflow failure | `maxTurns: 60`; orchestrator can run the critique outside the run and pass `critiqueDone` |
| Review script default outputs | `codex_review.sh --out X` also rewrites the tracked default `.refactor/codex-review.{md,jsonl}` from an earlier split | `git status` after the review | restore the default files from HEAD before committing evidence |

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
| Digest tests in the per-cluster gate | `REFACTOR_TEST_CMD` includes a suite that hashes production-subject bytes (forge: `tests.test_fr230_phase3_manifest`, `tests.test_fr223_v2_byte_pins`); every extractor gate fails on the first cluster because only the wave-end re-mint can update the digests, and the Opus retry fails identically | extractor report: `cause` names the digest suite; the first workflow launch stops at cluster 1 | keep digest suites out of `focusedTests`; run them in the wave-end prep after `REMINT` (the app split, wf_f0c3217d, cost one launch) |
| Subject edit after finalize without re-mint | A post-review fix (header prune, alias-form cleanup) changes the bytes of a production subject; finalize already minted, so the digests are stale and the full gate fails on the digest suite only | full test discovery; the binding reviewer's blocking finding | every commit that touches a subject re-mints in the same commit or the next one; run the digest suite before launching the gate-1 pair (app split cc2cf98) |
| Consensus transcript trips a repo-wide scan | `codex_review.sh --followup` rewrites the tracked default `.refactor/codex-review.{md,jsonl}`; the transcript can quote a forbidden literal (forge: the legacy runtime name), so a scan test over `git ls-files --others` fails while the file sits in the tree | `tests.test_migration`-style scan tests in the full gate | `git checkout -- .refactor/codex-review.md .refactor/codex-review.jsonl` after every review script run, before launching a full gate; keep the split's own `-<name>.jsonl` clean before committing it |

## Escalation is part of the design

## Decomposition refusals and checks

| Failure | Check and response |
|---|---|
| `super()` / `__class__` / private mangling changes owner semantics | Method inventory refuses; keep method on the class or plan a separately tested design change. |
| Unknown/stacked decorator, setter overload, class-dependent default/annotation | Refuse instead of guessing descriptor or class-scope behavior. |
| Metaclass, decorated class, slots | Refuse mechanical class move; review object-model effects first. |
| Moved method resolves global state through a source cycle | Require independent import owner before moving; dynamic namespace access/global writes refuse. |
| Destination clash, stale source, duplicate definition or existing manifest | Refuse before writing; regenerate the plan from the current snapshot. |
| Container hash waived to allow a move | Manifest oracle reconstructs whole class, bindings and bases; class-wide allow-lists are prohibited. |
| Source changed after a verified cluster | Compare each manifest at its recorded output commit in a worktree and check final sources against reviewed outputs. |
| Partial statements, return/yield/await or nonlocal/global extraction | Rope and the independent structural check refuse; choose a complete range. |
| Escaping, recursive, complex-signature or nonlocal nested closure | Hoist refuses; use a separately reviewed design change. |
| Excessive extracted parameters | Quality limit refuses; select a more cohesive range. |
| Support file/mixin accidentally collected | Reject discovery-pattern names; compare exact unittest/pytest IDs. |
| Test preamble defines a used constant/helper | Inventory lists the prerequisite; refusal names method and line. First relocate globals into non-discovered support, then recollect and re-freeze. |
| Namespace tests fail unittest importability checks | Collector auto preset uses the tests directory as top level; preserve that resolved setting and bare-module ID spelling throughout. |
| Inherited lint findings appear at the new path | Finalize reviews destination per-file ignores; no unmanifested body edits or blanket suppression of new defects. |
| Bad quality JSON breaks an edit hook | The standalone size hook reads only its flag/env setting, never decomposition configuration. Validate JSON separately in planning. |
| ID count preserved but a test changes shard or identity | Compare exact IDs and per-shard memberships, not counts alone. |
| Only giant functions moved into smaller modules | Quality report still flags function/class targets; record debt and continue the plan. |

The legacy split-module failure modes above still apply. For decompose, whole-file
manifest checking also rejects undeclared import fixes after extraction.

A failed gate is information, not an emergency. The ladder in SKILL.md (resume → stronger
model → re-plan smaller → stop) bounds the cost of any one cluster and keeps the main
context free of debugging noise: the extractor debugs in its own context; the
orchestrator only sees the JSON report.
