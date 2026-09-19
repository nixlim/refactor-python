// Fork of split-module's phase structure. Deliberately separate call sequence:
// never resume a split-module run using this workflow.
export const meta = {
  name: 'decompose-workflow',
  description: 'Manifest-verified class, function and test decomposition, sequential per source, with ID gates and independent reviews.',
  whenToUse: 'Several class/function extraction clusters; use the decompose skill for one cluster.',
  phases: ['Preflight', 'Plan', 'Critique', 'Extract', 'Merge', 'Finalize', 'Codex review', 'Review']
    .map(title => ({ title })),
}

// args: {targets:[{source,className?,functionName?,testOnly?}], pkgDir,
//        focusedTests?, fullTests?, mintCmd?, codexDone?, completed?:[{cluster,commit,snapshot,manifest}]}
const options = args || {}
if (!options.targets?.length || !options.pkgDir) throw new Error('targets and pkgDir are required')
const S = '${CLAUDE_PLUGIN_ROOT}/skills/split-module/scripts'
const D = '${CLAUDE_PLUGIN_ROOT}/skills/decompose/scripts'
const gateSchema = { type: 'object', required: ['gate'], properties: {
  gate: { type: 'string', enum: ['PASS', 'FAIL'] }, cause: { type: 'string' },
  commit: { type: 'string' }, snapshot: { type: 'string' }, manifest: { type: 'string' },
} }
const verdictSchema = { type: 'object', required: ['verdict', 'blocking'], properties: {
  verdict: { type: 'string', enum: ['APPROVE', 'REVISE', 'BLOCK', 'REJECT'] },
  blocking: { type: 'array', items: { type: 'string' } },
  disputed: { type: 'array', items: { type: 'string' } },
} }
const planSchema = { type: 'object', required: ['path', 'clusters'], properties: {
  path: { type: 'string' }, clusters: { type: 'array', items: { type: 'object',
    required: ['id', 'tier', 'tool', 'arguments', 'testMode'], properties: {
      id: { type: 'string' }, tier: { type: 'integer', enum: [1, 2] },
      tool: { type: 'string', enum: ['move_methods.py', 'extract_ranges.py'] },
      arguments: { type: 'object' }, testMode: { type: 'string', enum: ['none', 'identity', 'mapping'] },
    } } },
} }
const records = [...(options.completed || [])]
const requirePass = (r, label) => {
  if (!r || r.gate !== 'PASS') throw new Error(`${label}: ${r?.cause || 'missing/failing verdict'}`)
  return r
}
const hooks = JSON.stringify({focusedTests: options.focusedTests, fullTests: options.fullTests, mintCmd: options.mintCmd})

phase('Preflight')
const pre = await agent(
  `Read the decompose SKILL and operations reference. Targets: ${JSON.stringify(options.targets)}. ` +
  `Run ${S}/preflight.sh --decompose; require a clean feature branch and passing full behavior baseline. ` +
  `Pass the configured module ceiling via REFACTOR_MAX_LINES for every gate. ` +
  `Snapshot the whole affected scope ${options.pkgDir}, types, quality settings and test IDs/shards for test targets. ` +
  `Commit evidence. Verify every completed record ${JSON.stringify(records)} exists and its SHA is an ancestor of HEAD, ` +
  `and verify the manifest at that commit. Do not infer completion from destination existence. ` +
  `Return {gate,cause,commit} where commit is the baseline SHA. Project hooks: ${hooks}.`,
  {label:'preflight', agentType:'refactor-python:gate-runner', schema:gateSchema},
)
requirePass(pre, 'preflight')
if (!pre.commit) throw new Error('missing baseline commit')

phase('Plan')
let plan = await agent(
  `Decompose mode. Read the decompose skill and references. Inventory ${JSON.stringify(options.targets)} with ` +
  `class_inventory.py/function_inventory.py, inspect the census and quality.py configuration. ` +
  `Produce a dependency-ordered plan with tool arguments, manifest dry runs, pinned IDs, testMode, state/import owners, ` +
  `refusals, target/ceiling debt and follow-ups. Every cluster is exactly one tier. Tier 3 requires separate operator review. ` +
  `Completed records to retain: ${JSON.stringify(records)}. Return {path,clusters:[{id,tier,tool,arguments,testMode}]}.`,
  {label:'plan', agentType:'refactor-python:split-planner', schema:planSchema},
)
let approved = false
phase('Critique')
for (let attempt = 0; attempt < 2; attempt++) {
  if (!plan?.clusters?.length) throw new Error('empty/missing plan')
  const critique = await agent(
    `Decompose mode: review ${plan.path} against actual inventories, census, manifests, pinned IDs and C6 quality rules. ` +
    `Check every refusal and import dependency; a missing dry-run proof blocks. Return verdict, blocking and disputed.`,
    {label:`critique:${attempt}`, agentType:'refactor-python:plan-critic', schema:verdictSchema},
  )
  if (critique?.verdict === 'APPROVE' && !critique.blocking?.length) { approved = true; break }
  if (!critique || critique.verdict === 'BLOCK' || attempt === 1) break
  plan = await agent(`Revise ${plan.path} for ${JSON.stringify(critique.blocking)}. Return the full plan schema.`,
    {label:'replan', agentType:'refactor-python:split-planner', schema:planSchema})
}
if (!approved) throw new Error('no approved decomposition plan')

for (const cluster of plan.clusters) {
  if (records.some(r => r.cluster === cluster.id)) continue
  if (![1,2].includes(cluster.tier) || !['move_methods.py','extract_ranges.py'].includes(cluster.tool)) {
    throw new Error('unsupported operation; tier 3 needs its own approved plan')
  }
  phase('Extract')
  const extracted = await agent(
    `Decompose mode, cluster ${JSON.stringify(cluster)}; plan ${plan.path}; scripts ${D}; gate scripts ${S}. ` +
    `Start from current merged HEAD in a worktree. Freeze a fresh per-cluster source snapshot before edits. ` +
    `Run the selected tool in dry-run then apply mode; never hand-edit bodies or repair a refusal. ` +
    `Preserve manifests and snapshots under unique .refactor names. Add reviewed ID maps for class shape. ` +
    `Run the strict manifest gate plus IDs/shards for test targets and focused tests from ${hooks}. ` +
    `Commit only on PASS; return {gate,cause,commit,snapshot,manifest}.`,
    {label:`extract:${cluster.id}`, agentType:'refactor-python:extractor', isolation:'worktree', schema:gateSchema},
  )
  requirePass(extracted, 'extract')
  if (!extracted.commit || !extracted.snapshot || !extracted.manifest) throw new Error('incomplete extraction evidence')
  phase('Merge')
  const merged = await agent(
    `Merge commit ${extracted.commit} by SHA on the target branch, require clean tree; abort on conflict. ` +
    `Decompose gate: bash ${S}/verify.sh --pkg ${options.pkgDir} --snapshot ${extracted.snapshot} ` +
    `--manifest ${extracted.manifest} --strict-bodies; include the plan's test-snapshot and test-mode for ` +
    `${cluster.testMode}. Focused test override from ${hooks}. Do not mint yet. Return gate/cause/commit.`,
    {label:`merge:${cluster.id}`, agentType:'refactor-python:gate-runner', schema:gateSchema},
  )
  requirePass(merged, 'merge')
  records.push({cluster:cluster.id, commit:extracted.commit, snapshot:extracted.snapshot, manifest:extracted.manifest})
  const close = await agent(
    `Close cluster ${cluster.id}: inspect bindings/class headers against its manifest, test IDs/shards, import contracts. ` +
    `Run the project mint hook and full tests from ${hooks}, commit only project-owned regenerated evidence on PASS. ` +
    `Do not modify source after manifest validation. Return gate/cause/commit.`,
    {label:`close:${cluster.id}`, agentType:'refactor-python:gate-runner', schema:gateSchema},
  )
  requirePass(close, 'wave close')
}

phase('Finalize')
const finalized = await agent(
  `Finalize ${plan.path}. Save records ${JSON.stringify(records)} as .refactor/decompose-records.json. ` +
  `Verify each manifest at its recorded output commit in a worktree; compare final sources against reviewed outputs. ` +
  `Measure modules/functions/classes using quality.py; debt needs reason and follow_up. Update docs, measured baselines ` +
  `and import contracts, including reviewed destination per-file lint ignores for inherited findings. ` +
  `Run full gate, ID/shard comparisons and mint hook from ${hooks}. No source edits without ` +
  `a new verified cluster. Commit on PASS and return gate/cause/commit.`,
  {label:'finalize', agentType:'refactor-python:gate-runner', schema:gateSchema},
)
requirePass(finalized, 'finalize')

phase('Codex review')
const codex = options.codexDone || await agent(
  `Run ${S}/codex_review.sh --base ${pre.commit} --plan ${plan.path} --out .refactor/codex-decompose.md. ` +
  `Review decompose manifests, recorded commit evidence, test identities and quality debt. Return verdict/blocking/disputed. ` +
  `If unavailable or timed out, return BLOCK with the cause; the orchestrator can run it detached and relaunch.`,
  {label:'codex', model:'claude-opus-4-8', schema:verdictSchema},
)
phase('Review')
const review = await agent(
  `Decompose mode: independently review baseline ${pre.commit} to finalized HEAD, ${plan.path}, ` +
  `.refactor/decompose-records.json and then adjudicate .refactor/codex-decompose.md. ` +
  `Verify manifests at their recorded output commits and run test identity checks; class headers must match manifests. ` +
  `Return verdict/blocking/disputed. Resolve disagreements with evidence before reporting APPROVE.`,
  {label:'review', agentType:'refactor-python:refactor-reviewer', schema:verdictSchema},
)
const done = codex?.verdict === 'APPROVE' && !codex.blocking?.length &&
  review?.verdict === 'APPROVE' && !review.blocking?.length && !review.disputed?.length
return {status:done ? 'done' : 'review-required', records, plan:plan.path, codex, review, commit:finalized.commit}
