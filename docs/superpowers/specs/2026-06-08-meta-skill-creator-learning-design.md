# Meta-Skill Creator Learning Design

## Purpose

OpenSquilla already has a stronger MetaSkill creation pipeline than a plain
SKILL.md generator: generated candidates pass through clarification, pattern
selection, slot filling, assembly, collision review, lint, risk classification,
smoke tests, runtime E2E comparison, preview, proposal persistence, and optional
auto-enable gates.

This design extends that foundation by borrowing the best ideas from Claude
Code and Hermes Agent without weakening OpenSquilla's gated proposal lifecycle.

## External Models Reviewed

Claude Code treats skills and slash commands as one user-facing invocation
surface. A project or user skill can be invoked manually, invoked by the model,
or disabled from model invocation. Claude skills also support dynamic context
snippets, allowed tools, isolated context modes, and a Skill Creator plugin that
supports create, eval, improve, and benchmark workflows. Claude's
`run-skill-generator` pattern is especially relevant: it turns a successful
project run or verification recipe into a reusable project skill.

Hermes Agent treats skills as agent-managed procedural memory. Its
`skill_manage` tool supports create, patch, edit, delete, write_file, and
remove_file operations, so the agent can refine skills after real usage. Hermes
also has skill bundles: lightweight workflow profiles that load several skills
behind one slash command without requiring a full executable DAG. Hermes skills
declare tool requirements, fallback behavior, platform constraints, config
requirements, and hub/tap distribution metadata.

## Learning Method: Borrow Principles, Not Surfaces

Claude Code and Hermes Agent should be treated as reference systems, not as
implementation templates. A feature is only worth adopting when its underlying
principle maps cleanly onto OpenSquilla's proposal-gated MetaSkill lifecycle.

Use this filter before importing an external pattern:

1. **Separate surface from mechanism.** XML tags, section names, and prompt
   phrasing are surface. Progressive disclosure, class-first skill memory,
   proposal gates, repair loops, and eval-backed prompt behavior are mechanisms.
2. **Identify the product invariant.** Claude optimizes for reliable context
   loading and structured instruction following. Hermes optimizes for procedural
   memory that improves over repeated use. OpenSquilla optimizes for reviewable,
   gated, multi-skill orchestration proposals.
3. **Map to an existing lifecycle stage.** Prefer fitting a principle into
   clarification, slot generation, proposal gates, repair hints, benchmark,
   learning summary, or approval review. Avoid adding a parallel creator path.
4. **Define a measurable behavior.** Every adopted principle needs a prompt
   contract, gate result, benchmark case, or regression test that can fail when
   the principle is removed.
5. **Reject mismatched mechanisms.** Do not import a mechanism just because it
   works elsewhere if it bypasses proposal gates, weakens reviewability, or
   duplicates an existing OpenSquilla layer.

### Reference Pattern Mapping

| Reference pattern | Why it works there | OpenSquilla-compatible lesson | Adopt now? | Validation signal |
| --- | --- | --- | --- | --- |
| Claude XML/sectioned prompting | Reduces ambiguity between role, context, examples, constraints, and output | Use structured prompt regions for slot filling and gate repair prompts, but keep schema validation as the source of truth | Yes | Prompt contract tests plus schema-validation retries |
| Claude examples / eval-first guidance | Gives the model positive and negative target behavior before generation | Pair every generated activation surface with positive and negative prompts, not just a prose trigger | Yes | Activation eval gate, negative prompt false-positive checks |
| Claude supporting files / progressive disclosure | Keeps the main skill small while allowing deep references on demand | Keep generated `SKILL.md` concise and move bulky references/scripts into proposal bundle files | Yes, when bundle file support is used | Proposal manifest lists linked files; lint rejects oversized SKILL.md |
| Hermes `skill_manage` patch-over-rewrite | Prevents accumulated duplicate skills and makes small corrections cheap | Prefer patching pending proposals or superseding accepted skills over regenerating from scratch | Yes for proposals, later for accepted-skill replacement UX | Proposal revision lineage and stale-gate refresh tests |
| Hermes class-first skill review | Captures reusable task classes rather than one-off session transcripts | Generate meta-skills for a recurring task class, not a single-session replica | Yes | `TASK_CLASS` preservation tests and trigger-boundary gates |
| Hermes background skill review | Learns after task completion without interrupting the main task | Useful for future auto-propose suggestions, but must remain advisory and gated | Later | Learning events create draft seeds, not installed skills |
| Hermes agent-managed direct skill creation | Makes the agent self-improving quickly | Too permissive for OpenSquilla installable MetaSkills because it can bypass proposal gates | No direct adoption | No path writes installed skills without proposal gates |
| Hermes bundles / slash profiles | Loads a repeated skill set without executable DAG overhead | Add a separate bundle/profile layer only for non-DAG combinations | Later | Loader keeps `kind: bundle` separate from `kind: meta` |

### Adoption Gates

An external idea can move from "interesting" to "implemented" only if it passes
these gates:

- **Fit gate:** The idea maps to an existing OpenSquilla lifecycle stage or a
  clearly separate new layer. If it creates a second creator pipeline, reject it.
- **Safety gate:** The idea cannot promote, install, or modify user-visible
  skills without the existing proposal review path.
- **Quality gate:** The idea has at least one measurable claim, such as fewer
  overbroad triggers, better input/output contract retention, or lower false
  positive activation.
- **Regression gate:** The claim is locked by a test or benchmark. Prompt-only
  behavior must have tests for the decisive strings or structured fields.
- **UX gate:** The user can understand what happened: preview, proposal id,
  gate blockers, repair hints, or review summary.

This keeps the creator from becoming a pile of borrowed prompt snippets. The
goal is to absorb Claude's structured generation discipline and Hermes'
procedural-memory discipline while preserving OpenSquilla's core advantage:
reviewable, gated, multi-skill orchestration.

## Design Goals

1. Preserve OpenSquilla's gated MetaSkill proposal lifecycle as the source of
   truth for installable workflows.
2. Add a lightweight author entry that can turn a successful conversation,
   MetaSkill run, or run summary into a proposal draft.
3. Improve generation quality through explicit intent distillation, workflow
   shape selection, slot completion, output-contract synthesis, and generator
   rationale.
4. Measure activation quality with explicit positive and negative prompts before
   relying on a generated trigger or description.
5. Support small proposal improvements through patch/edit flows instead of
   forcing full regeneration.
6. Add a lightweight bundle/profile layer for repeated skill combinations that
   do not need a DAG.
7. Extend existing conditional visibility metadata so skills and MetaSkills only
   surface when their tool and platform requirements can be satisfied.
8. Add evaluation and benchmark loops for creator output quality, using
   existing `eval_prompts`, `output_contract`, and runtime E2E infrastructure.
9. Support lifecycle controls for versioning, supersession, deprecation, and
   one-command rollback of promoted proposals.

## Non-Goals

- Do not replace MetaSkills with Hermes-style bundles. Bundles are a lighter
  layer for non-DAG workflows.
- Do not let agent-managed edits bypass proposal gates.
- Do not auto-install user-visible skills from raw conversation history.
- Do not enable arbitrary shell-based dynamic context injection by default.
- Do not merge all ordinary skills and MetaSkills into one internal type.
- Do not add learned trigger models, analytics dashboards, cross-tenant sharing,
  or concurrent patch merge UX in P0 or P1.

## Architecture

### Layer 1: Draft Seeds

Add a deterministic draft seed helper that accepts a run summary, conversation
summary, or explicit user description and produces a normalized
`MetaSkillDraftSeed` payload:

```json
{
  "source_kind": "conversation|meta_run|manual",
  "goal": "string",
  "observed_steps": ["skill-name"],
  "candidate_triggers": ["string"],
  "inputs": ["string"],
  "outputs": ["string"],
  "constraints": ["string"],
  "negative_cases": ["string"],
  "evidence_refs": ["run-id or session-id"]
}
```

The seed helper does not write proposals. It only prepares structured input for
the existing `meta-skill-creator` DAG.

### Layer 1A: Generation Capability Scaffold

Generation quality should improve before evaluation filters the output. Treat the
creator as a structured authoring pipeline, not a single text-generation prompt.

The generator should make these decisions explicit:

- intent distillation: one-sentence goal, target user outcome, and stop
  condition;
- workflow shape: ordinary skill, MetaSkill, bundle, patch to an existing
  proposal, or refusal;
- pattern selection: which reusable workflow archetype or successful prior
  proposal it is adapting;
- slot completion: required inputs, outputs, constraints, tools, gates, and
  failure handling;
- output contract synthesis: final response sections, artifacts, and completion
  evidence;
- alternative rejection: nearby shapes or skills considered and why they were
  not chosen.

Each candidate should carry a compact `generation_rationale` with the selected
shape, source evidence, filled slots, unresolved assumptions, and rejected
alternatives. This rationale is not user-facing prose; it is review and gate
material that makes creator capability measurable.

Use a curated pattern library rather than open-ended generation whenever
possible. Good patterns include verification recipe, research-to-report,
debug-fix-verify, data-generation pipeline, UI QA loop, and recurring operator
brief. The pattern library can be seeded from accepted proposals and successful
runs, but additions remain reviewable.

### Layer 2: Creator Modes

Extend creator modes conceptually:

- `PREVIEW_ONLY`: no persistence, no heavy gates.
- `PERSISTED_PROPOSAL`: write a reviewable proposal after deterministic gates.
- `FULL_GATED`: run baseline comparison and runtime E2E before eligibility.
- `PATCH_PROPOSAL`: apply a bounded edit to an existing proposal, then rerun
  impacted gates.
- `BENCHMARK`: compare current and revised proposal behavior over eval prompts
  without promoting either version.

The first three modes already exist. `PATCH_PROPOSAL` and `BENCHMARK` should
reuse the same proposal store and gate result shape.

### Layer 2A: Activation Eval-Benchmark

Treat the generated name, description, triggers, and negative-space clauses as
first-class artifacts. Before a proposal is accepted or benchmarked, evaluate
whether it activates for intended prompts and stays silent for neighboring
prompts.

Each candidate should carry or derive:

- `trigger_when`: examples and compact rules for when the workflow should fire;
- `skip_when`: examples and compact rules for adjacent cases it must not steal;
- positive activation prompts;
- negative activation prompts;
- activation accuracy summary with variance across prompt variants.

Negative prompts must be partly catalog-derived from adjacent installed skills,
not only author-written or candidate-derived. Otherwise activation quality becomes
a vanity score that misses real trigger collision. P0 should scope adjacent skills
through the cheapest available neighborhood signal first, such as trigger text,
topic tags, or embedding buckets if already available.

Activation scoring should include a determinism contract:

- true-positive rate and false-positive rate over fixed positive/negative sets;
- repeated samples per prompt;
- mean and variance recorded in the proposal gate result;
- hard thresholds that fail eligibility when exceeded;
- an eval budget per proposal so creation cost stays reviewable.

This pass is separate from runtime logic E2E. A candidate can have correct DAG
logic and still be unsafe if its description or triggers misroute user intent.
Description optimization should iterate only the activation surface. Logic
optimization should iterate the DAG, step inputs, output contract, and gates.
Generation optimization should iterate intent distillation, shape choice, pattern
selection, slot completion, and output contract synthesis.

### Layer 3: Proposal Patch/Edit

Proposal editing should operate on pending proposals, not installed bundled
skills. The patch flow:

1. Load `SKILL.md` and `gates.json` for a pending proposal.
2. Apply a small structured patch request.
3. Parse and lint the revised candidate.
4. Rerun collision, risk, smoke, and runtime E2E gates when affected.
5. Store a new proposal revision record while preserving the original.

Patch requests should prefer small changes: trigger refinement, step wording,
metadata additions, output contract adjustment, eval prompt additions, or
dependency correction.

Proposal revisions should preserve lineage:

- parent proposal id or revision id;
- semantic version or monotonically increasing revision number;
- owner/source of the change;
- supersedes/deprecates metadata when replacing an accepted skill;
- rollback target when a promoted revision regresses.

### Layer 4: Skill Bundles

Add a lightweight `kind: bundle` or separate bundle manifest for repeated
combinations of skills that do not require runtime DAG orchestration. The exact
manifest shape should be chosen after a loader spike; if `kind: bundle` is used,
the loader and parser must keep bundles separate from `kind: meta` plans so a
bundle cannot be passed to the MetaSkill scheduler by mistake.

Bundles should provide:

- name;
- description;
- triggers or slash command alias;
- ordered or unordered skill list;
- optional prompt prefix;
- conditional visibility metadata;
- no step outputs;
- no proposal auto-enable unless converted into a MetaSkill.

Bundles serve the Hermes-style use case: "load these skills together because
this domain frequently needs them." They are not substitutes for MetaSkills
that need branching, validation gates, run history, or composed outputs.
Bundle validation should reject prompt prefixes that smuggle execution ordering
or step dependencies. If ordering matters, the bundle should be converted into a
MetaSkill draft.

### Layer 5: Conditional Visibility

OpenSquilla already parses conditional activation fields from
`metadata.opensquilla`, including `requires_tools` and `fallback_for_toolsets`.
This design extends that existing namespace instead of introducing a parallel
manifest location.

The target metadata shape is:

```yaml
metadata:
  opensquilla:
    requires_tools: []
    fallback_for_toolsets: []
    requires_toolsets: []
    fallback_for_tools: []
    platforms: []
    config_keys: []
```

Initial implementation should reuse existing `requires_tools` and
`fallback_for_toolsets` behavior first. New fields require parser, snapshot,
serde, prompt-injection, CLI, and WebUI tests before they influence routing.

The initial behavior should be conservative:

- missing hard requirements hide or downgrade model-visible invocation;
- WebUI shows unmet requirements before run;
- proposal auto-enable refuses candidates with unsatisfied hard requirements;
- fallback metadata only affects display and future routing until tested.

### Layer 6: Evaluation and Benchmarking

Creator output should be evaluated through explicit prompts and contracts:

- generation-quality checks verify shape choice, slot completeness, minimality,
  output-contract fit, tool correctness, and rationale quality.
- `eval_prompts` supply positive and negative activation cases.
- `output_contract` defines required final sections and artifacts.
- runtime E2E compares candidate MetaSkill output against a no-meta baseline.
- benchmark mode compares proposal revision A/B outputs over the same prompts.
- continuous drift audits rerun activation and runtime checks after model
  upgrades, tool/API changes, and new sibling skills.
- trigger-collision audits rerun when any new skill or MetaSkill lands, because
  a later proposal can poach intent from an older one.

No-workflow baseline comparison must include misroute prompts, not only intended
positive prompts. A candidate that improves intended prompts but steals adjacent
intent should remain ineligible. Drift and collision audits should use scoped
neighborhoods and explicit budgets, because full catalog re-audit is quadratic as
the catalog grows.

This borrows Claude's create/eval/improve/benchmark lifecycle while keeping
OpenSquilla's proposal gates.

Generation benchmarking should diagnose the creator output before activation or
runtime scoring hides the reason for failure. A failed candidate should be tagged
with a small failure taxonomy, such as wrong workflow shape, missing required
slot, overbroad trigger, under-specified output contract, unnecessary tool,
missing gate, duplicate existing skill, or privacy-unsafe example.

### Layer 7: Procedural Memory Feedback

Successful trajectories and user corrections should feed future proposal drafts
without automatically installing anything.

- Repeated successful ad-hoc trajectories become draft seeds after a frequency
  threshold and outcome-quality signal.
- User corrections become local guardrails for the affected proposal and can be
  suggested for sibling bundles or related MetaSkills.
- Correction propagation stays reviewable: it creates patch suggestions or
  benchmark cases, not direct edits to installed skills.
- Memory-derived drafts should be rate-limited so repeated low-quality behavior
  does not flood the proposal queue.
- A future router MetaSkill may become useful once the retained catalog grows
  enough that retrieval and trigger collision become the bottleneck.

### Layer 7A: Continuous Meta-Skill Optimization

Hermes Agent's strongest lesson is that skills should evolve from usage,
corrections, and failures. OpenSquilla should adopt that loop without adopting
Hermes-style direct installed-skill mutation. The safe OpenSquilla loop is:

```text
creator run, gate failure, benchmark, accept, rollback, or user correction
  -> sanitized creator learning event
  -> ranked lesson card
  -> generation prompt hint, patch proposal, or benchmark case
  -> normal proposal gates
  -> human review / accept / rollback
```

The learning layer should distinguish raw events from interpreted lessons.
Events are append-only facts. Lesson cards are derived, compact, ranked, and
safe to show to the generator.

Each lesson card should contain:

```json
{
  "lesson_id": "overbroad_trigger:fragile-skill:2",
  "pattern": "overbroad_trigger",
  "confidence": "medium",
  "evidence_count": 2,
  "sources": ["rolled_back", "activation_eval"],
  "problem": "Generated trigger caught generic summarize requests.",
  "recommendation": "Require action/domain nouns and add a skip eval prompt.",
  "prompt_hint": "Avoid generic verbs such as summarize or review unless paired with the task domain.",
  "patch_hint": {
    "remove_triggers": ["summarize", "review"],
    "append_eval_prompts": [
      {
        "name": "negative_generic_summary",
        "prompt": "summarize this generic note",
        "expect": "skip"
      }
    ]
  }
}
```

Initial taxonomy:

- `overbroad_trigger`: activation false positives, trigger collisions, or
  rollback reasons mentioning broad triggers.
- `missing_input_contract`: generation-quality or human-review feedback says
  required inputs/files/config are unclear.
- `missing_output_contract`: proposal lacks required final artifact, sections,
  or completion evidence.
- `weak_negative_cases`: candidate has triggers but no useful skip examples or
  negative eval prompts.
- `bad_eval_prompts`: activation examples are labels, self-referential, too
  similar, or fail to cover adjacent tasks.
- `benchmark_regression`: benchmark loses to a prior revision or introduces a
  case-level regression.
- `rollback_after_accept`: an accepted skill was reverted, so future generator
  output should treat that proposal as negative evidence.

Lesson cards should influence generation in three bounded ways:

1. `meta_skill_fill_slots` receives the highest-ranked relevant cards as
   advisory context and must decide whether each card applies to the current
   `TASK_CLASS`.
2. Patch mode can convert a lesson card into a bounded patch proposal, such as
   trigger refinement, `eval_prompts` addition, or output-contract merge.
3. Benchmark mode can convert lesson cards into fixed eval cases for comparing
   proposal revisions.

Lesson cards must never directly edit installed skills, silently promote a
proposal, or bypass stale-gate refresh. This preserves the safety benefit of
OpenSquilla's proposal lifecycle while borrowing Hermes' procedural-memory
feedback loop.

## Data Flow

### Conversation or Run to Proposal

1. User clicks or asks "turn this run into a MetaSkill draft."
2. Draft seed helper summarizes the observed workflow or refuses with a concrete
   reason when the trace lacks a coherent single intent.
3. Duplicate detection checks whether the seed should patch an existing proposal
   instead of creating a sibling.
4. Creator distills intent, selects workflow shape, chooses a pattern, fills
   slots, and emits `generation_rationale`.
5. Existing slot filling and assembly produce a candidate.
6. Generation-quality checks verify shape choice, completeness, minimality,
   output contract, and tool fit.
7. Activation eval checks positive, negative, and catalog-adjacent trigger
   behavior.
8. Existing gates run according to creator mode.
9. User reviews preview or pending proposal.
10. Accept flow promotes only eligible proposals unless force is explicit.

### Proposal Patch

1. User selects a pending proposal.
2. User asks for a bounded edit.
3. Patch helper edits candidate text or structured slots.
4. Gates rerun.
5. A new revision is written with gate deltas.
6. WebUI and CLI show revision lineage and current eligibility.

### Bundle to MetaSkill

1. User creates a bundle for repeated skill co-loading.
2. If later usage shows the bundle needs ordered outputs, branching, or quality
   gates, creator can convert the bundle into a MetaSkill draft seed.
3. Conversion enters the normal proposal lifecycle.

### Continuous Optimization

1. A creator event is recorded from persistence, benchmark, acceptance,
   rollback, gate failure, or explicit user correction.
2. Event sanitization strips secrets, unknown keys, multiline raw traces, and
   unbounded prose.
3. Lesson-card builder groups recent events by taxonomy, skill name, proposal
   id, and reason.
4. Ranked cards are returned by `creator_learning_summary` alongside the
   existing human-readable summary.
5. The creator DAG injects cards into slot filling as advisory memory.
6. If a card targets an existing pending proposal, patch mode can produce a
   revision rather than regenerating a sibling.
7. If a card targets an accepted skill, the system creates a reviewable patch or
   replacement proposal and requires normal gates before promotion.
8. Benchmark mode replays lesson-derived eval cases to compare old and new
   proposal behavior.

## Error Handling

- If a draft seed lacks a clear goal, output shape, or trigger boundary, pause
  for structured clarification.
- If a proposed patch changes step topology, rerun all gates instead of only
  lint.
- If requirements are unsatisfied, refuse auto-enable and expose the unmet
  requirement in CLI/WebUI.
- If benchmark prompts are missing, generate only a preview and mark benchmark
  unavailable.
- If activation prompts are missing, derive a minimal positive/negative set from
  `trigger_when`, `skip_when`, candidate triggers, and catalog-adjacent skills
  before persistence.
- If generation rationale is missing or contradicts the assembled candidate,
  keep the proposal in preview-only or ineligible state until repaired.
- If the generator selects a MetaSkill for a non-DAG co-loading use case,
  recommend a bundle; if it selects a bundle for dependent steps, recommend a
  MetaSkill.
- If required slots cannot be inferred safely, request clarification rather than
  inventing inputs, gates, or output contracts.
- If a run summary contains file content, secrets, or personal data, paraphrase
  trigger examples and omit literal content from the draft.
- If duplicate detection finds a strong trigger overlap, recommend patching the
  existing proposal instead of creating a new sibling.
- If two reviewers try to patch the same pending proposal, take a sequential lock
  and surface the conflict rather than merging automatically.
- If runtime E2E context is unavailable, persist only as an ineligible proposal
  with the failure reason preserved.
- If a bundle attempts to express step dependencies, recommend conversion to a
  MetaSkill rather than adding hidden DAG semantics to bundles.
- If a promoted proposal regresses, rollback disables the promoted revision and
  restores the previous accepted version or pending proposal state.

## Testing Strategy

1. Parser and loader tests preserve new metadata fields.
2. Draft seed tests convert run summaries into deterministic seed payloads.
3. Generation-quality tests cover shape choice, pattern selection, slot
   completeness, output-contract synthesis, and rationale consistency.
4. Activation eval tests cover positive, negative, neighboring-domain, and
   variance cases for generated descriptions and triggers.
5. Creator DAG tests verify seed payloads reach slot filling without losing raw
   user constraints.
6. Draft refusal tests return `cannot draft` for incoherent or multi-intent
   traces.
7. Duplicate-detection tests redirect overlapping drafts to patch targets.
8. Privacy tests ensure draft triggers paraphrase rather than copy sensitive
   user content.
9. Proposal patch tests verify revisions preserve lineage and rerun gates.
10. Bundle tests verify model-visible summaries and CLI/WebUI listing behavior.
11. Conditional visibility tests cover satisfied, missing, and fallback cases.
12. Benchmark tests compare two proposal revisions over fixed eval prompts.
13. Drift tests rerun activation/collision checks after adding a sibling skill.
14. Rollback tests disable a promoted revision and restore previous behavior.
15. Regression tests ensure existing `PREVIEW_ONLY`, `PERSISTED_PROPOSAL`, and
   `FULL_GATED` behavior remains unchanged.
16. Lesson-card tests derive `overbroad_trigger`, `missing_input_contract`,
    `weak_negative_cases`, and `benchmark_regression` cards from sanitized
    learning events.
17. Prompt-consumption tests prove relevant lesson cards are passed into
    `meta_skill_fill_slots` and irrelevant cards remain advisory.
18. Safety tests prove lesson cards cannot directly mutate installed skills or
    bypass stale-gate refresh.

## Rollout Plan

### P0: Generation and Activation Foundation

Implement the minimal proposal fields, generation rationale, pattern-library
hooks, generation-quality checks, and activation eval-benchmark for generated
descriptions, triggers, `trigger_when`, and `skip_when`. This is the first slice
because a candidate that is badly generated or misfires should not proceed to
heavier authoring automation. P0 also needs a catalog-aware negative-prompt
sourcer; without it, the metric cannot predict collisions.

### P1: Author Entry

Implement draft seeds from run summaries and conversations. Feed seeds into the
existing creator DAG. Add refusal, duplicate detection, privacy scrubbing, and
automatic P0 activation evaluation before reviewer acceptance. Do not add new
proposal mutation or bundle behavior yet.

### P2: Proposal Iteration

Add patch/edit proposal revisions, benchmark mode, and eval-prompt reuse. Keep
promotion rules unchanged. Add revision lineage, supersession, and rollback
metadata before enabling installed-skill patch flows.

### P3: Continuous Quality and Lightweight Workflow Profiles

Add continuous trigger-collision audits, drift checks, skill bundles, and
conditional visibility metadata. Start with CLI/WebUI visibility and
prompt-injection behavior before any automatic routing changes.

### P4: Learning Loop

Use repeated successful patterns, user corrections, and benchmark results to
suggest proposal drafts or revisions. Suggestions remain reviewable proposals,
not automatic installed skills.

### P4A: Continuous Optimization Loop

Add lesson cards as the bridge between raw learning events and future creator
behavior. This slice should be implemented before any autonomous installed-skill
mutation is considered. P4A is allowed to influence prompts, patch proposals, and
benchmark cases; it is not allowed to promote, install, or edit accepted skills
without existing proposal gates.

## Success Criteria

- A successful run can become a reviewable MetaSkill draft without manual YAML
  authoring.
- Generated candidates explain their intent, workflow shape, selected pattern,
  completed slots, unresolved assumptions, and rejected alternatives.
- Candidate generation quality is checked for shape choice, slot completeness,
  output-contract fit, tool correctness, minimality, and rationale consistency.
- Generated triggers and descriptions have measured positive and negative
  activation behavior before acceptance, including catalog-derived negative
  prompts.
- Activation gates record true-positive rate, false-positive rate, sample count,
  variance, and budget usage.
- Drafting from a run can refuse incoherent traces, redirect duplicate drafts to
  existing proposals, and avoid copying sensitive literal content into triggers.
- Pending proposals can be improved through bounded patches.
- Users can represent repeated skill co-loading as bundles without building a
  full MetaSkill.
- Auto-enable remains at least as conservative as today.
- Creator quality can be compared over eval prompts before promotion.
- A promoted skill can be rolled back without manually editing files.
- Missing tools or platform requirements are visible before invocation.
- Repeated creator failures become ranked lesson cards with evidence counts,
  problem statements, recommendations, and bounded patch hints.
- Future generation receives relevant lesson cards as advisory JSON and can
  convert them into safer triggers, negative cases, eval prompts, and rationale.
- Lesson-derived improvements remain reviewable through proposal patches,
  gates, and benchmarks.

## P0 and P1 Acceptance Criteria

P0 generation and activation foundation:

Generation capability:

- Given the same seed, the creator emits a candidate with stable intent
  distillation, workflow shape, selected pattern, filled slots, output contract,
  and `generation_rationale`.
- Shape-choice fixtures cover ordinary skill, MetaSkill, bundle, patch target,
  and refusal outcomes.
- Required slots are either filled from evidence or explicitly marked as missing;
  the generator does not invent tools, gates, inputs, or output contracts.
- Generated candidates include a compact alternative-rejection list for at least
  one nearby workflow shape or existing skill.
- Generation-quality gates flag wrong shape, missing slot, unnecessary tool,
  under-specified output contract, duplicate existing skill, and privacy-unsafe
  example failures.

Activation measurement:

- Given a proposal with `trigger_when` and `skip_when`, evaluator emits
  true-positive rate and false-positive rate over at least 20 positive prompts
  and 20 negative prompts.
- Each activation prompt is sampled at least three times, with mean and variance
  recorded in the proposal gate result.
- At least half of negative prompts are catalog-derived from adjacent installed
  skill or MetaSkill surfaces.
- Initial hard gate target: true-positive rate at least 0.85, false-positive
  rate at most 0.10, and variance band at most 0.05.
- Evaluator completes within a small review-cycle budget, initially targeting no
  more than 60 seconds per candidate on the standard test fixture.
- Known-good and known-bad proposal fixtures remain on the expected side of the
  gate in CI.

P1 author entry:

- Given a successful run trace, the system emits a draft containing name,
  description, `trigger_when`, `skip_when`, tools, and initial output contract.
- Trajectories without coherent single intent return `cannot draft: <reason>`
  instead of malformed drafts.
- Drafts auto-run through P0 activation evaluation and show scores before
  reviewer acceptance.
- Drafts with strong name or trigger overlap surface an existing proposal as a
  patch target instead of silently creating a sibling.
- Trigger examples paraphrase source material and do not embed literal user file
  content, secrets, or personal data.
- Recorded run-summary fixtures can produce drafts that meet P0 gates after at
  most one bounded human edit pass.

## Risks and Mitigations

- Risk: learning loop locks in bad behavior.
  Mitigation: suggestions become gated proposals, not installed skills.
- Risk: bundles and MetaSkills confuse users.
  Mitigation: bundles are "load these skills"; MetaSkills are "execute this
  workflow."
- Risk: patch/edit bypasses review.
  Mitigation: every revision gets gates and lineage.
- Risk: conditional visibility hides useful skills.
  Mitigation: start with WebUI/CLI warnings and model-summary downgrades before
  hard suppression.
- Risk: benchmark mode increases cost.
  Mitigation: make benchmark opt-in and reuse small eval prompt sets first.
- Risk: creator improves trigger scores while still generating weak workflows.
  Mitigation: add generation-quality gates for workflow shape, pattern selection,
  slot completeness, output-contract fit, and rationale consistency.
- Risk: generator invents plausible but unsupported workflow details.
  Mitigation: require each critical slot to be evidence-backed or explicitly
  marked missing for clarification.
- Risk: pattern library narrows creativity too much.
  Mitigation: allow a reviewed custom pattern path, but require an explicit
  alternative-rejection rationale and normal gates before promotion.
- Risk: activation scoring becomes noisy or expensive.
  Mitigation: record repeated-sample variance and enforce per-proposal eval
  budgets.
- Risk: catalog-wide collision audits become quadratic.
  Mitigation: scope audits to adjacent trigger neighborhoods before running
  deeper checks.
- Risk: draft seeds duplicate existing skills.
  Mitigation: run duplicate detection before proposal creation and suggest patch
  targets.
- Risk: run-summary drafts leak sensitive user details.
  Mitigation: paraphrase trigger examples and omit literal file, secret, and PII
  content.
- Risk: trigger text improves while DAG logic regresses.
  Mitigation: keep activation optimization and runtime logic optimization as
  separate passes with separate gates.
- Risk: corrections over-propagate into unrelated workflows.
  Mitigation: propagate corrections as suggestions and benchmark cases, not
  automatic edits.
- Risk: lesson cards become prompt injection carriers.
  Mitigation: derive cards only from sanitized event fields, cap text lengths,
  preserve source provenance, and inject them as advisory JSON rather than raw
  user prose.
- Risk: stale lessons keep penalizing a repaired skill.
  Mitigation: rank by recency and evidence count, keep accepted benchmark wins as
  counter-evidence, and expose source ids for review.
- Risk: lesson-card patches become too powerful.
  Mitigation: map cards only to existing bounded patch operations and rerun stale
  gates before accept.

## Evidence Anchors

- OpenSquilla already supports creator modes, proposal gates, runtime E2E,
  auto-enable eligibility, and a first slice of conditional activation metadata.
- Claude Code contributes the skill/command unification, dynamic context,
  isolated skill execution, readable skill artifacts, and
  create/eval/improve/benchmark lifecycle.
- Hermes Agent contributes agent-managed skill patching, skill bundles,
  conditional requirements, hub/tap lifecycle, and procedural-memory learning.
- Hermes-style direct skill mutation is intentionally not adopted for installed
  OpenSquilla skills; continuous optimization remains proposal-gated because
  persistent memory, self-authored skills, scheduling, and shell share an
  authority boundary in always-on agents.
