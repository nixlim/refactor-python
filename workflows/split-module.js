export const meta = {
  name: 'split-module-workflow',
  description: 'Split one or more oversized Python modules into packages: plan -> critique -> parallel rope extraction in worktrees -> gated merge -> independent review.',
  whenToUse: 'A plan with more than ~6 clusters, or several god-modules at once. For one small module use the /refactor-python:split-module skill directly.',
  phases: [
    { title: 'Preflight' },
    { title: 'Plan' },
    { title: 'Critique' },
    { title: 'Package' },
    { title: 'Extract' },
    { title: 'Merge' },
    { title: 'Codex review' },
    { title: 'Review' },
    { title: 'Finalize' },
  ],
}

// args: { modules: ["app/core/engine.py", ...], pkgDir?: "app/core", maxLines?: 500 }
const modules = (args && args.modules) || []
if (!modules.length) throw new Error('args.modules is required: ["path/to/module.py", ...]')
const maxLines = (args && args.maxLines) || 500
const S = '${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts'
const RULES = `Rules: move only with ${S}/rope_move.py; never edit bodies; never add abstractions; ` +
  `never Read a god-module whole; one cluster per commit; gate with ${S}/verify.sh before every commit.`

const PLAN_SCHEMA = {
  type: 'object', required: ['planPath', 'pkgDir', 'waves'],
  properties: {
    planPath: { type: 'string' }, pkgDir: { type: 'string' },
    waves: { type: 'array', items: { type: 'array', items: {
      type: 'object', required: ['cluster', 'dest', 'symbols'],
      properties: { cluster: { type: 'string' }, dest: { type: 'string' }, symbols: { type: 'array', items: { type: 'string' } } } } } },
  },
}
const VERDICT = { type: 'object', required: ['verdict'], properties: { verdict: { type: 'string', enum: ['APPROVE', 'REVISE', 'BLOCK', 'REJECT'] }, blocking: { type: 'array', items: { type: 'string' } }, disputed: { type: 'array', items: { type: 'string' } } } }
const EXTRACT = { type: 'object', required: ['cluster', 'branch', 'gate'], properties: { cluster: { type: 'string' }, branch: { type: 'string' }, commit: { type: ['string', 'null'] }, gate: { type: 'string', enum: ['pass', 'fail'] }, notes: { type: 'string' } } }
const GATE = { type: 'object', required: ['gate'], properties: { gate: { type: 'string', enum: ['PASS', 'FAIL'] }, cause: { type: 'string' } } }

// ---------------------------------------------------------------- Preflight
phase('Preflight')
const pre = await agent(
  `Run 'bash ${S}/preflight.sh' from the repo root; if it fails on missing tools run 'bash ${S}/preflight.sh --install' once and re-run. ` +
  `Then ensure: feature branch (create refactor/split-batch if on default), clean tree, tests pass (pytest -q -x). ` +
  `Create .refactor/, run 'python3 ${S}/snapshot_bodies.py snapshot <parent dir of each module> --out .refactor/before-<stem>.json' per module, ` +
  `and 'python3 ${S}/check_file_length.py --write-baseline .refactor-baseline.json .'. Commit as "refactor: freeze baseline". ` +
  `Report JSON {ok:boolean, branch:string, baselineCommit:string, problems:string[]}.`,
  { label: 'preflight', model: 'claude-opus-4-8', schema: { type: 'object', required: ['ok', 'branch', 'baselineCommit'], properties: { ok: { type: 'boolean' }, branch: { type: 'string' }, baselineCommit: { type: 'string' }, problems: { type: 'array', items: { type: 'string' } } } } },
)
if (!pre || !pre.ok) throw new Error('preflight failed: ' + JSON.stringify(pre && pre.problems))
log(`preflight ok on ${pre.branch}`)

// -------------------------------------------------------------------- Plan
phase('Plan')
const plans = await pipeline(modules,
  (mod) => agent(
    `Module: ${mod}. Run 'python3 ${S}/inventory.py ${mod} --repo-root . --module-name <dotted> --json .refactor/inventory-<stem>.json > .refactor/inventory-<stem>.md'. ` +
    `Then produce the split plan exactly as your instructions specify (targets, state owners, waves). ` +
    `Return JSON {planPath, pkgDir, waves:[[{cluster,dest,symbols[]}]]} where waves are ordered and clusters within a wave are disjoint.`,
    { label: 'plan:' + mod, phase: 'Plan', agentType: 'refactor-python:split-planner', schema: PLAN_SCHEMA },
  ).then((p) => ({ mod, ...p })),
)

// ---------------------------------------------------------------- Critique
phase('Critique')
const approved = []
for (const p of plans.filter(Boolean)) {
  let plan = p
  for (let round = 0; round < 2; round++) {
    const c = await agent(
      `Review the plan at ${plan.planPath} for module ${plan.mod} against .refactor/inventory-*.json. Return JSON {verdict, blocking[]}.`,
      { label: 'critique:' + plan.mod, phase: 'Critique', agentType: 'refactor-python:plan-critic', schema: VERDICT },
    )
    if (!c || c.verdict === 'APPROVE') break
    if (c.verdict === 'BLOCK') { log(`BLOCKED ${plan.mod}: ${(c.blocking || []).join('; ')}`); plan = null; break }
    plan = await agent(
      `Revise the plan at ${p.planPath} to address these blocking findings: ${JSON.stringify(c.blocking)}. Return the same JSON shape as before.`,
      { label: 'replan:' + p.mod, phase: 'Critique', agentType: 'refactor-python:split-planner', schema: PLAN_SCHEMA },
    ).then((np) => np && { mod: p.mod, ...np })
    if (!plan) break
  }
  if (plan) approved.push(plan)
}
if (!approved.length) throw new Error('no plan approved')

// ----------------------------------------------------------------- Package
phase('Package')
await pipeline(approved, (plan) => agent(
  `Convert ${plan.mod} to a package: 'git mv <dir>/<stem>.py <dir>/<stem>/__init__.py', then ` +
  `'bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot .refactor/before-<stem>.json', then commit "refactor(<stem>): convert module to package". ` +
  `Report JSON {gate:"PASS"|"FAIL", cause}.`,
  { label: 'package:' + plan.mod, phase: 'Package', model: 'claude-opus-4-8', schema: GATE },
))

// ---------------------------------------------------- Extract + Merge waves
for (const plan of approved) {
  const stem = plan.mod.replace(/^.*\//, '').replace(/\.py$/, '')
  const src = plan.mod.replace(/\.py$/, '/__init__.py')
  const snapshot = `.refactor/before-${stem}.json`

  for (let w = 0; w < plan.waves.length; w++) {
    const wave = plan.waves[w]
    phase('Extract')
    const brief = (c) =>
      `Cluster: ${c.cluster}\nSource module: ${src}\nDestination module: ${c.dest}\nSymbols to move, in this order: ${c.symbols.join(', ')}\n` +
      `Snapshot: ${snapshot}\nPackage dir for the gate: ${plan.pkgDir}\nScripts: ${S}\n${RULES}\nReturn the JSON report described in your instructions.`

    // one extractor per cluster, in parallel, each in its own worktree; escalate on failure
    let results = await parallel(wave.map((c) => () =>
      agent(brief(c), { label: `extract:${stem}:${c.cluster}`, phase: 'Extract', agentType: 'refactor-python:extractor', isolation: 'worktree', schema: EXTRACT })))
    results = results.filter(Boolean)
    const failed = wave.filter((c) => !results.find((r) => r.cluster === c.cluster && r.gate === 'pass'))
    if (failed.length) {
      log(`wave ${w + 1}: ${failed.length} cluster(s) failed on Opus 4.8; retrying on Opus 5`)
      const retry = await parallel(failed.map((c) => () =>
        agent(brief(c), { label: `extract2:${stem}:${c.cluster}`, phase: 'Extract', agentType: 'refactor-python:extractor', model: 'claude-opus-5', isolation: 'worktree', schema: EXTRACT })))
      results = results.filter((r) => r.gate === 'pass').concat(retry.filter(Boolean))
    }
    const passed = results.filter((r) => r.gate === 'pass')
    const stillFailed = wave.filter((c) => !passed.find((r) => r.cluster === c.cluster)).map((c) => c.cluster)
    if (stillFailed.length) log(`wave ${w + 1}: giving up on clusters ${stillFailed.join(', ')} (needs re-plan or human)`)

    // merge in plan order, one at a time; on conflict re-extract on the updated base
    phase('Merge')
    for (const c of wave) {
      let r = passed.find((x) => x.cluster === c.cluster)
      if (!r) continue
      for (let attempt = 0; attempt < 2; attempt++) {
        const m = await agent(
          `On branch ${pre.branch}, run 'git merge --no-ff ${r.branch}'. If git reports conflicts run 'git merge --abort' and return {gate:"FAIL", cause:"conflict"}. ` +
          `Otherwise run 'bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot ${snapshot}' and return {gate, cause}. Do not fix anything.`,
          { label: `merge:${stem}:${c.cluster}`, phase: 'Merge', agentType: 'refactor-python:gate-runner', schema: GATE },
        )
        if (m && m.gate === 'PASS') break
        if (m && m.cause === 'conflict' && attempt === 0) {
          r = await agent(brief(c), { label: `reextract:${stem}:${c.cluster}`, phase: 'Merge', agentType: 'refactor-python:extractor', isolation: 'worktree', schema: EXTRACT })
          if (!r || r.gate !== 'pass') break
          continue
        }
        log(`merge of ${c.cluster} failed: ${m && m.cause}`)
        break
      }
    }
    await agent(
      `On branch ${pre.branch}: add backwards-compatible re-exports to ${src} for every public symbol moved in wave ${w + 1} of ${plan.planPath} ` +
      `(explicit 'from .x import A as A' form; keep __all__ accurate), run 'bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot ${snapshot}', ` +
      `and commit "refactor(${stem}): re-exports for wave ${w + 1}". Return {gate, cause}.`,
      { label: `reexport:${stem}:w${w + 1}`, phase: 'Merge', model: 'claude-opus-5', schema: GATE },
    )
  }
}

// ------------------------------------------------------------ Codex review
phase('Codex review')
const codexReviews = await pipeline(approved, (plan) => agent(
  `On branch ${pre.branch}, run 'bash ${S}/codex_review.sh --base ${pre.baselineCommit} --plan ${plan.planPath} --out .refactor/codex-review-${plan.mod.replace(/[^A-Za-z0-9]+/g, '_')}.md'. ` +
  `Do not read the .jsonl log. Return JSON {ok:boolean, path:string, verdict:string, thread:string} from the output file (verdict "unavailable" and ok:false if codex is missing or failed).`,
  { label: 'codex:' + plan.mod, phase: 'Codex review', model: 'claude-opus-4-8', schema: { type: 'object', required: ['ok', 'path', 'verdict'], properties: { ok: { type: 'boolean' }, path: { type: 'string' }, verdict: { type: 'string' }, thread: { type: 'string' } } } },
).then((r) => ({ mod: plan.mod, ...(r || { ok: false, path: '', verdict: 'unavailable' }) })))

// ------------------------------------------------------------------ Review
phase('Review')
const reviews = await pipeline(approved, (plan) => agent(
  `Review the split of ${plan.mod}: baseline commit ${pre.baselineCommit}, HEAD of ${pre.branch}, plan ${plan.planPath}, snapshot .refactor/before-*.json, scripts ${S}, codex review at ${(codexReviews.find((c) => c.mod === plan.mod) || {}).path || 'unavailable'}. Do your own check first, then adjudicate every Codex finding as CONFIRMED/REFUTED/UNVERIFIABLE. Return JSON {verdict, blocking[], disputed[]}.`,
  { label: 'review:' + plan.mod, phase: 'Review', agentType: 'refactor-python:refactor-reviewer', schema: VERDICT },
).then((v) => ({ mod: plan.mod, ...(v || { verdict: 'REJECT', blocking: ['reviewer returned nothing'] }) })))

// -------------------------------------------------------------- Consensus
phase('Review')
const consensus = []
for (const r of reviews) {
  const cx = codexReviews.find((c) => c.mod === r.mod)
  for (const finding of (r.disputed || [])) {
    if (!cx || !cx.ok || !cx.thread) { consensus.push({ mod: r.mod, finding, outcome: 'codex unavailable; escalate to user' }); continue }
    const reply = await agent(
      `Run: bash ${S}/codex_review.sh --followup ${cx.thread} ${JSON.stringify(finding)} and return JSON {outcome:"AGREE"|"DISAGREE"|"RETRACT", evidence:string} from Codex's answer.`,
      { label: 'consensus:' + r.mod, phase: 'Review', model: 'claude-opus-4-8', schema: { type: 'object', required: ['outcome'], properties: { outcome: { type: 'string' }, evidence: { type: 'string' } } } },
    )
    consensus.push({ mod: r.mod, finding, outcome: (reply && reply.outcome) || 'no answer', evidence: reply && reply.evidence })
  }
}

// ---------------------------------------------------------------- Finalize
phase('Finalize')
const fin = await agent(
  `On ${pre.branch}: regenerate '.refactor-baseline.json' with 'python3 ${S}/check_file_length.py --write-baseline .refactor-baseline.json --max ${maxLines} .', ` +
  `run 'python3 ${S}/check_file_length.py --max ${maxLines} .' and report which of ${JSON.stringify(approved.map((p) => p.pkgDir))} still have files over budget. ` +
  `Then produce a Markdown report: per module the new files with code-line counts, symbols moved, gate status, Codex verdicts ${JSON.stringify(codexReviews.map((c) => ({ mod: c.mod, verdict: c.verdict })))}, Fable verdicts ${JSON.stringify(reviews)}, consensus record ${JSON.stringify(consensus)} (write it to .refactor/consensus.md; unresolved DISAGREE items must be listed for the user), and the exact gate command. Commit the baseline. Return the report as text.`,
  { label: 'finalize', phase: 'Finalize', model: 'claude-opus-5' },
)

return { branch: pre.branch, codexReviews, reviews, consensus, report: fin }
