# Model roles

Only three models are allowed in this plugin: `claude-fable-5-1`, `claude-opus-5`,
`claude-opus-4-8`. All three have 1M-token context windows, so context size is not
the reason for choosing between them; judgment quality, independence of review, cost,
and latency are.

| Role | Agent | Model | Why |
|---|---|---|---|
| Orchestrator | your main session | whatever you started Claude Code with; Fable 5.1 or Opus 5 recommended | Holds the plan and makes merge decisions. Its context must stay clean, which is why everything noisy is delegated. |
| Planner | `split-planner` | **Fable 5.1**, effort high, read-only | One call, highest-stakes judgment in the whole process: where to cut, who owns state, what order. Worth the strongest model. |
| Plan critic | `plan-critic` | **Opus 5**, read-only | A *different* model than the planner reviews the plan. Self-review by the same model reproduces the same blind spots (the "self-refactoring" result: models rubber-stamp their own output). |
| Extractor (N in parallel) | `extractor` | **Opus 4.8**, effort medium, `isolation: worktree` | The work is mechanical and tool-gated: run rope, run the gate, commit. Reliability comes from the tools, not the model, so use the cheapest approved model and parallelize. Escalates to Opus 5 on second failure. |
| Gate runner | `gate-runner` | **Opus 4.8**, effort low | Runs `verify.sh` in the fan-in step and returns a short verdict so test output never lands in the orchestrator's context. |
| Second-opinion reviewer | `scripts/codex_review.sh` (headless OpenAI Codex, not a Claude subagent) | **GPT-5.6 Sol**, reasoning effort `ultra` (falls back to `xhigh` on API-key backends) | A different model *family* reviews first. Cross-family review catches errors a same-family reviewer shares; the codex-orchestrator research notes warn that blind consensus is a popularity trap, so its verdict is evidence to adjudicate, not a vote. |
| Adjudicating reviewer | `refactor-reviewer` | **Fable 5.1**, effort high, read-only | Catching a subtle behavior change in a large diff is the second-hardest judgment call. Must not be the extractor's model. |

## Escalation ladder (per cluster)

1. Extractor (Opus 4.8) fails the gate → resume it with the gate output.
2. Fails again → fresh extractor on **Opus 5** (pass `model: claude-opus-5` when spawning).
3. Fails again → planner (Fable 5.1) re-plans that cluster smaller.
4. Fails again → stop and report. Do not try Fable as an extractor "to brute-force it";
   a cluster that three attempts cannot move mechanically has a structural problem that
   needs a human decision.

## Cost notes

Pricing differs by roughly 2x between tiers (Opus 4.8 is the cheapest of the three at
the time of writing; Fable the most expensive). A split of one 12K-line module typically
runs 1 planner call, 1 critic call, 5–15 extractor runs, and 1 reviewer call, so the
extractor tier dominates cost; that is why extractors default to Opus 4.8.

## Overriding

- Force every subagent onto one model for a session:
  `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5 CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1 claude`.
- Change a single role: edit the `model:` line in `agents/<role>.md` (plugin agents
  cannot be edited in place after install; copy the file to `.claude/agents/` and it
  takes precedence).
- Per-invocation: the orchestrator can pass `model` when spawning an agent; that wins
  over the frontmatter.

## Review order and why
1. **Codex (GPT-5.6 Sol, ultra) reviews first**, blind, from the diff and the plan.
2. **Fable 5.1 reviews second**, doing its own check *before* reading Codex's verdict (to avoid
   anchoring), then confirms or refutes each Codex finding with evidence.
3. **One consensus round** on refuted findings via `codex exec resume` (the script's
   `--followup`). Unresolved disagreements go to the user with both sides' evidence.
Override the Codex model/effort with `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT`.
