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
| Broken imports elsewhere | A caller in another package still imports the old path | `compileall`, mypy/pyright, tests | rope rewrites project-wide imports |
| Circular import | `partially initialized module` at import time | tests (import fails), `lint-imports` | playbook §Circular imports; thin `__init__` |
| Forked module state | Two modules each define `REGISTRY = {}` | reviewer (inventory lists state); tests if covered | single-owner rule; move state first |
| Lazy-import cover-up | Extractor hides a cycle with a function-local import | reviewer (must be flagged in report) | rule: flag every local import |
| Partial read of the file | Agent reasons over the first 2,000 lines as if whole | inventory-based planning; extractor never Reads whole file | playbook §Reading large files |
| Tangled commit | Refactor mixed with a bug fix or feature | reviewer; one-cluster-per-commit rule | commit message convention `refactor(<name>):` only |
| Merge conflict resolved by hand | Manual 3-way merge of a 10K-line file loses a hunk | `compare` after merge | rule: abort and re-extract on updated base |
| Regrowth after split | The new modules balloon again next week | `check_file_length.py` in hook + CI; import-linter | guardrails skill; baseline removal at finalize |
| Design drift | Extractor adds an ABC/registry to make a move compile | reviewer | rule: no new abstractions during a split |
| Wrong base branch | Worktree branched from `main`, commits do not apply to the refactor branch | preflight NOTE; merge fails | `worktree.baseRef: "head"` in settings |

## Escalation is part of the design

A failed gate is information, not an emergency. The ladder in SKILL.md (resume → stronger
model → re-plan smaller → stop) bounds the cost of any one cluster and keeps the main
context free of debugging noise: the extractor debugs in its own context; the
orchestrator only sees the JSON report.
