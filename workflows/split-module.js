export const meta = {
  name: 'split-module-workflow',
  description: 'Split one or more oversized Python modules into packages: plan -> critique -> sequential rope extraction in worktrees -> gated merge by commit -> finalize -> independent review.',
  whenToUse: 'A plan with more than ~6 clusters, or several god-modules at once. For one small module use the /refactor-python:split-module skill directly.',
  phases: [
    { title: 'Preflight' },
    { title: 'Plan' },
    { title: 'Critique' },
    { title: 'Package' },
    { title: 'Extract' },
    { title: 'Merge' },
    { title: 'Finalize' },
    { title: 'Codex review' },
    { title: 'Review' },
  ],
}

// args: { modules: ["app/core/engine.py", ...], pkgDir?: "app/core", maxLines?: 500,
//         parallel?: false,                       // extract a wave's clusters concurrently (only sensible across different source modules)
//         doneWaves?: { "<stem>": [0, 1] },       // waves already closed on the branch (restart after a stop; derive from the re-export commits)
//         doneClusters?: { "<stem>": ["name"] }, // clusters of the current wave already merged (derive from git ls-tree of the package)
//         codexDone?: { "<module path>": {ok, path, verdict, thread} } }  // a Codex review the orchestrator ran detached
// Restart rule: prefer a FRESH launch with doneWaves/doneClusters over resumeFromRunId. Resume caching is a prefix of the
// agent() call sequence, so after any control-flow change the cached extract results of an old tip are replayed and merged,
// and a resumed instance reuses worktree numbers. Remove the run's worktrees and branches before relaunching.
const modules = (args && args.modules) || []
if (!modules.length) throw new Error('args.modules is required: ["path/to/module.py", ...]')
const maxLines = (args && args.maxLines) || 500
const S = '${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts'
const RULES = `Rules: move only with ${S}/rope_move.py; never edit bodies; never add abstractions; ` +
  `never Read a god-module whole; never edit __all__, CHANGELOG.md, docs or tests; one cluster per commit; gate with ${S}/verify.sh before every commit; ` +
  `report the commit SHA (git rev-parse HEAD after your commit).`
const PARALLEL = !!(args && args.parallel)
const DONE_WAVES = (args && args.doneWaves) || {}
const DONE_CLUSTERS = (args && args.doneClusters) || {}
const CODEX_DONE = (args && args.codexDone) || {}

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
const GATE = { type: 'object', required: ['gate'], properties: { gate: { type: 'string', enum: ['PASS', 'FAIL'] }, cause: { type: 'string' }, commit: { type: 'string' } } }
const isConflict = (m) => !!(m && /conflict/i.test(String(m.cause || '')))   // gate-runners sometimes describe the conflict instead of returning the literal

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
const waveReports = []
for (const plan of approved) {
  const stem = plan.mod.replace(/^.*\//, '').replace(/\.py$/, '')
  const src = plan.mod.replace(/\.py$/, '/__init__.py')
  const pkgPath = plan.mod.replace(/\.py$/, '')
  const snapshot = `.refactor/before-${stem}.json`
  const doneWaves = new Set(DONE_WAVES[stem] || [])
  const doneClusters = new Set(DONE_CLUSTERS[stem] || [])
  const brief = (c) =>
    `Cluster: ${c.cluster}\nSource module: ${src}\nDestination module: ${c.dest}\nSymbols to move, in this order: ${c.symbols.join(', ')}\n` +
    `Snapshot: ${snapshot}\nPackage dir for the gate: ${plan.pkgDir}\nScripts: ${S}\n${RULES}\nReturn the JSON report described in your instructions.`
  const extract = (c, label, extra) => agent(brief(c), { label, phase: 'Extract', agentType: 'refactor-python:extractor', isolation: 'worktree', schema: EXTRACT, ...(extra || {}) })
  // Extract once, retry once on Opus 5. A passing report without a commit SHA is not a pass.
  const runCluster = async (c) => {
    let r = await extract(c, `extract:${stem}:${c.cluster}`)
    if (!r || r.gate !== 'pass' || !r.commit) {
      log(`${stem} ${c.cluster}: extractor failed (${r && r.notes}); retrying on Opus 5`)
      r = await extract(c, `extract2:${stem}:${c.cluster}`, { model: 'claude-opus-5' })
    }
    return r && r.gate === 'pass' && r.commit ? r : null
  }
  // Merge BY COMMIT SHA, never by the reported branch name; an unchanged HEAD is a failure, an ancestor is already merged.
  const mergeOne = (r, c) => agent(
    `On branch ${pre.branch} in the repository root. The extractor for cluster "${c.cluster}" committed ${r.commit}; merge BY COMMIT, never by branch name.\n` +
    `1. Require 'git rev-parse --abbrev-ref HEAD' to print ${pre.branch} and 'git status --porcelain --untracked-files=no' to be empty; otherwise return {gate:"FAIL", cause:"dirty or wrong branch"}.\n` +
    `2. If 'git merge-base --is-ancestor ${r.commit} HEAD' succeeds the commit is already merged: skip to step 4.\n` +
    `3. before=$(git rev-parse HEAD); 'git merge --no-ff ${r.commit} -m "Merge ${c.cluster} (${r.commit})"'. On conflicts run 'git merge --abort' and return {gate:"FAIL", cause:"conflict"}. Then require HEAD to differ from before; otherwise return {gate:"FAIL", cause:"nothing merged"}.\n` +
    `4. Run exactly: bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot ${snapshot} --strict-bodies\nReturn {gate, cause, commit:<git rev-parse HEAD>} from its "== gate results ==" block. Do not fix anything.`,
    { label: `merge:${stem}:${c.cluster}`, phase: 'Merge', agentType: 'refactor-python:gate-runner', schema: GATE },
  )

  for (let w = 0; w < plan.waves.length; w++) {
    const wave = plan.waves[w]
    if (doneWaves.has(w)) { waveReports.push({ mod: plan.mod, wave: w, merged: wave.map((c) => c.cluster), failed: [], end: 'done-before-launch' }); log(`${stem} wave ${w + 1}: already closed on the branch, skipping`); continue }
    const merged = []
    const failed = []
    const pending = wave.filter((c) => !doneClusters.has(c.cluster))
    for (const c of wave) if (doneClusters.has(c.cluster)) merged.push(c.cluster)

    phase('Extract')
    if (!PARALLEL || pending.length === 1) {
      // Sequential (default): every cluster rewrites the root's import block, so parallel extraction from one tip
      // only ever lands its first merge and re-extracts the rest. Each cluster starts from the merged HEAD.
      for (const c of pending) {
        const r = await runCluster(c)
        if (!r) { failed.push(c.cluster); break }
        phase('Merge')
        const m = await mergeOne(r, c)
        if (!m || m.gate !== 'PASS') { failed.push(c.cluster); log(`${stem} wave ${w + 1}: merge of ${c.cluster} failed: ${m && m.cause}`); break }
        merged.push(c.cluster)
        phase('Extract')
      }
    } else {
      const results = await parallel(pending.map((c) => () => runCluster(c).then((r) => r && { ...r, cluster: c.cluster })))
      phase('Merge')
      for (const c of pending) {
        let r = results.find((x) => x && x.cluster === c.cluster)
        if (!r) { failed.push(c.cluster); continue }
        let ok = false
        for (let attempt = 0; attempt < 2 && !ok; attempt++) {
          const m = await mergeOne(r, c)
          if (m && m.gate === 'PASS') { ok = true; break }
          if (isConflict(m) && attempt === 0) {
            // re-extract from the merged HEAD, never resolve by hand
            r = await extract(c, `reextract:${stem}:${c.cluster}`)
            if (!r || r.gate !== 'pass' || !r.commit) break
            continue
          }
          log(`${stem} wave ${w + 1}: merge of ${c.cluster} failed: ${m && m.cause}`)
          break
        }
        if (ok) merged.push(c.cluster); else failed.push(c.cluster)
      }
    }
    if (failed.length) {
      log(`${stem} wave ${w + 1}: stopping; failed clusters: ${failed.join(', ')}; merged: ${merged.join(', ')} (repoint a layout pin, apply the plan's fallback seam, or re-plan; then relaunch with doneWaves/doneClusters)`)
      waveReports.push({ mod: plan.mod, wave: w, merged, failed })
      break
    }

    // Wave close in two agents: the full test run can exhaust one agent's turn budget.
    phase('Merge')
    const prep = await agent(
      `On branch ${pre.branch} at the repository root, prepare the close of wave ${w + 1} of ${plan.planPath} (clusters: ${wave.map((c) => c.cluster).join(', ')}). Do NOT commit.\n` +
      `1. In ${src} rewrite the re-exports for every symbol moved in this wave to the explicit 'from .<target> import A as A' form; every name in __all__ and every attribute read by importers and tests must still resolve.\n` +
      `2. __all__ must be VERBATIM the baseline literal: compare it (ast) against 'git show ${pre.baselineCommit}:${plan.mod}'; if rope or an extractor appended names, restore the baseline list.\n` +
      `3. Require 'git diff ${pre.baselineCommit} --stat -- CHANGELOG.md' to be empty and every commit of this wave to touch only ${pkgPath}/ and import sites; report any other path in cause.\n` +
      `4. If the repository keeps digest/byte pins on the moved files (a manifest of subject hashes, fixture digests), re-mint them now.\n` +
      `5. Fast gate: bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot ${snapshot} --strict-bodies --fast\n` +
      `Return {gate, cause}: PASS only if steps 2, 3 and 5 hold. Leave the working tree as it is for the next agent.`,
      { label: `wave-end-prep:${stem}:w${w + 1}`, phase: 'Merge', model: 'claude-opus-5', schema: GATE },
    )
    const end = (!prep || prep.gate !== 'PASS') ? prep : await agent(
      `On branch ${pre.branch} at the repository root (working tree already prepared; do not edit anything). Run the full gate as ONE foreground Bash call with the maximum timeout (never background it, never poll): bash ${S}/verify.sh --pkg ${plan.pkgDir} --snapshot ${snapshot} --strict-bodies\n` +
      `If it passes: git add -A -- ${pkgPath} && git commit -m "refactor(${stem}): re-exports after wave ${w + 1} (${wave.map((c) => c.dest.replace(/^.*\//, '')).join(', ')})" and return {gate:"PASS", commit:<sha>}. Otherwise return {gate:"FAIL", cause:<the failing checks>} without committing.`,
      { label: `wave-end-gate:${stem}:w${w + 1}`, phase: 'Merge', agentType: 'refactor-python:gate-runner', schema: GATE },
    )
    waveReports.push({ mod: plan.mod, wave: w, merged, failed: [], end })
    if (!end || end.gate !== 'PASS') { log(`${stem} wave ${w + 1}: wave-end gate failed: ${end && end.cause}`); break }
    log(`${stem} wave ${w + 1} closed: ${merged.length} cluster(s), commit ${end.commit || ''}`)
  }
}
const stopped = waveReports.filter((r) => r.failed.length || !r.end || (r.end !== 'done-before-launch' && r.end.gate !== 'PASS'))
if (stopped.length) return { branch: pre.branch, status: 'stopped', waveReports }

// ---------------------------------------------------------------- Finalize (BEFORE the review: reviewers otherwise block on what this step changes)
phase('Finalize')
const fin = await agent(
  `On ${pre.branch}: 1. regenerate '.refactor-baseline.json' with 'python3 ${S}/check_file_length.py --write-baseline .refactor-baseline.json --max ${maxLines} .' (every entry pinned to its measured size; the old module entries disappear), ` +
  `run 'python3 ${S}/check_file_length.py --max ${maxLines} .' and report which of ${JSON.stringify(approved.map((p) => p.pkgDir))} still have files over budget. ` +
  `2. If import-linter is configured, add a layers contract per new package in wave order (bottom = the first wave's targets) and run 'lint-imports'; any ignore_imports you need is a deviation to report, not to hide. ` +
  `3. Update documentation that names the old module path, write ONE CHANGELOG entry for the whole split, re-mint any digest/byte pins on the moved files. ` +
  `4. Strict oracle per module: python3 ${S}/snapshot_bodies.py compare .refactor/before-<stem>.json <pkg dir> --strict. 5. Full gate per module: bash ${S}/verify.sh --pkg <pkg dir> --snapshot .refactor/before-<stem>.json --strict-bodies. ` +
  `6. Commit as "refactor: finalize split (baseline, import contract, docs)". ` +
  `Return a Markdown report: per module the new files with code-line counts and symbols moved, gate status of steps 1-5, every deviation, the tip SHA, and the exact gate commands.`,
  { label: 'finalize', phase: 'Finalize', model: 'claude-opus-5' },
)

// ------------------------------------------------------------ Codex review
// The headless review can run 20-30 minutes on a large split, longer than a subagent's turn budget. The orchestrator
// may run it detached and pass the result as args.codexDone; otherwise the agent gets ONE bounded foreground attempt.
phase('Codex review')
const codexReviews = await pipeline(approved, (plan) => CODEX_DONE[plan.mod]
  ? Promise.resolve({ mod: plan.mod, ...CODEX_DONE[plan.mod] })
  : agent(
    `On branch ${pre.branch}, run as ONE foreground Bash call with the maximum timeout (never background it): 'bash ${S}/codex_review.sh --base ${pre.baselineCommit} --plan ${plan.planPath} --out .refactor/codex-review-${plan.mod.replace(/[^A-Za-z0-9]+/g, '_')}.md'. ` +
    `Do not read the .jsonl log. Return JSON {ok:boolean, path:string, verdict:string, thread:string} from the output file. If the call times out or codex is missing, return ok:false and verdict "unavailable" with the reason; the orchestrator will run it detached and relaunch with args.codexDone.`,
    { label: 'codex:' + plan.mod, phase: 'Codex review', model: 'claude-opus-4-8', schema: { type: 'object', required: ['ok', 'path', 'verdict'], properties: { ok: { type: 'boolean' }, path: { type: 'string' }, verdict: { type: 'string' }, thread: { type: 'string' } } } },
  ).then((r) => ({ mod: plan.mod, ...(r || { ok: false, path: '', verdict: 'unavailable' }) })))

// ------------------------------------------------------------------ Review (of the finalized tree)
phase('Review')
const reviews = await pipeline(approved, (plan) => agent(
  `Review the FINALIZED split of ${plan.mod}: baseline commit ${pre.baselineCommit}, HEAD of ${pre.branch}, plan ${plan.planPath}, snapshot .refactor/before-*.json, scripts ${S}, codex review at ${(codexReviews.find((c) => c.mod === plan.mod) || {}).path || 'unavailable'}. ` +
  `Do your own check first (oracle compare over the snapshot's scope, __all__ verbatim, no CHANGELOG/test edits beyond layout pins, no direct patch.object sites on package aliases in tests), then adjudicate every Codex finding as CONFIRMED/REFUTED/UNVERIFIABLE with evidence. Return JSON {verdict, blocking[], disputed[]}.`,
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

return { branch: pre.branch, status: 'done', waveReports, report: fin, codexReviews, reviews, consensus }
