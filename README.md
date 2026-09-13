# refactor-python

A Claude Code plugin for safely decomposing oversized Python modules in agent-written
codebases, and for keeping them small afterwards.

**What it enforces:** relocations happen only through rope (deterministic, project-wide
import rewriting); every step must pass a gate (compile, ruff, types, import contracts,
AST body-hash oracle, tests) before it is committed; extraction fans out to parallel
subagents in git worktrees and fans back in one merge at a time; a headless Codex (GPT-5.6 Sol, ultra) review followed by a Fable 5.1 review that adjudicates
both the diff and Codex's findings, with a consensus round on disagreements the result; a hook and CI stop files from regrowing.

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
```

For several modules or a plan with many clusters:

```
/refactor-python:split-module-workflow   with args {"modules": ["app/core/engine.py", "app/api/handlers.py"]}
```
or just say "use a workflow to split app/core/engine.py".

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

## Models

Planner and reviewer: `claude-fable-5-1`. Critic: `claude-opus-5` (deliberately not the
planner's model). Extractors and gate runner: `claude-opus-4-8`, escalating to Opus 5 on
repeated gate failure. Rationale and overrides: `skills/split-module/references/model-roles.md`.

## Second-opinion review (Codex)

Phase 6a runs `scripts/codex_review.sh`, which follows the operational playbook of the
[codex-orchestrator](https://github.com/alexzh3/codex-orchestrator) plugin (install it too;
its skill is the reference for locating the binary, run modes, and resume/consensus).
Requires Codex CLI or the IDE extension, signed in. `ultra` needs the ChatGPT backend; on
API-key providers the script falls back to `xhigh`.

## Requirements

Python ≥ 3.9, git, and: rope, ruff, pytest, mypy or pyright (required); import-linter,
grimp, radon, vulture (recommended). `preflight.sh --install` installs the Python ones
with uv or pip.

## Validate the plugin

```bash
claude plugin validate ./refactor-python --strict
```
