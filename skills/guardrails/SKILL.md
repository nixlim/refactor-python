---
name: guardrails
description: >-
  Install and verify file-size, complexity, and import-boundary guardrails for a Python
  repo worked on by AI agents: ruff complexity rules, a max-code-lines pre-commit hook with
  a grandfather baseline, import-linter contracts, a CI job, and a CLAUDE.md/AGENTS.md
  section that points at the enforcing tools. Use after a module split, when files keep
  growing, when setting up a new agent-driven repo, or when asked for "guardrails",
  "lint rules for file size", "stop files from getting huge", or "enforce module boundaries".
argument-hint: "[--max-lines N] [--no-import-linter]"
allowed-tools: Bash(python3 *), Bash(bash *), Bash(git *), Bash(ruff *), Bash(pre-commit *), Bash(lint-imports *), Read, Write, Edit, Grep, Glob
---

# Guardrails for agent-written Python

Prose rules in CLAUDE.md get partial compliance; deterministic checks get near-total
compliance. Install both, and make the prose point at the checks.

Templates live in `${CLAUDE_PLUGIN_ROOT}/skills/guardrails/templates/`. The size checker
is `${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts/check_file_length.py`.
Default budget: 500 code lines per file (override with `--max-lines`).

## Steps

1. **Detect existing config.** Look for `pyproject.toml` (`[tool.ruff]`), `ruff.toml`,
   `.pre-commit-config.yaml`, `.importlinter`, `[tool.importlinter]`, `CLAUDE.md`,
   `AGENTS.md`, and a CI workflow under `.github/workflows/`. Never overwrite; merge.
2. **Vendor the checker** so CI and pre-commit do not depend on the plugin being
   installed: copy `check_file_length.py` to `scripts/check_file_length.py` in the repo
   (create `scripts/` if needed) and `chmod +x` it.
3. **Baseline the present.** `python3 scripts/check_file_length.py --write-baseline .refactor-baseline.json --max <N> .`
   Files over budget today are grandfathered at their current size: they may shrink, not
   grow. Show the user the list; these are the candidates for `/refactor-python:split-module`.
4. **Ruff rules.** Merge `templates/pyproject.ruff.toml` into `[tool.ruff.lint]`:
   `C901` (complexity ≤ 10), `PLR0912/0913/0915` (branches/args/statements),
   `PLR0904` (public methods), `PLR1702` (nesting), plus `I` and `F`. If the repo has many
   existing violations, add `--add-noqa` once or keep them in `per-file-ignores` with a
   TODO; do not lower the thresholds.
5. **Pre-commit.** Merge `templates/pre-commit-config.yaml` (ruff, ruff-format, the
   vendored size check with baseline). Run `pre-commit install` and
   `pre-commit run --all-files`; fix or baseline until green.
6. **Import boundaries** (skip with `--no-import-linter`). Copy `templates/importlinter.ini`
   to `.importlinter`, set `root_package`, and write one `independence` contract listing
   the top-level feature packages plus one `layers` contract if the repo has a clear
   layering. Run `lint-imports`. Cycles found now are pre-existing; list them for the user
   rather than fixing them in this task.
7. **CI.** Merge `templates/ci-guardrails.yml` into the CI (GitHub Actions example;
   adapt the runner if needed). The job must fail the build on: ruff, size check,
   import contracts.
8. **Agent instructions.** Append `templates/CLAUDE.md.snippet` to `CLAUDE.md` (and to
   `AGENTS.md` if it exists; same text). Keep it under 25 lines; it references the tools
   rather than restating rules. If CLAUDE.md is already long (>200 lines), put the
   snippet at the top: rules near the top get followed more reliably.
9. **Claude Code hook.** If this plugin is enabled, the PostToolUse size guard is already
   active. For repos that must work without the plugin, merge `templates/settings.hooks.json`
   into `.claude/settings.json` (it calls the vendored script).
10. **Verify end to end.** Create a temporary 600-line file, confirm the hook message
    appears on write, confirm `pre-commit` and the CI command fail, delete the file.
    Report what was installed and the grandfathered list.

## Thresholds and why

| Rule | Default | Rationale |
|---|---|---|
| file code lines | 500 | Fits in one Read call with headroom; reviewable diff; parallel agents rarely collide on a 500-line file |
| function statements (PLR0915) | 50 | Ruff/Pylint default |
| complexity (C901) | 10 | Ruff/Pylint default |
| args (PLR0913) | 6 | Slightly above default 5 to reduce noise in agent code |
| nesting (PLR1702) | 4 | |

Raise a threshold only with a written reason in `pyproject.toml` next to the rule.
