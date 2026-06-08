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

## Design Goals

1. Preserve OpenSquilla's gated MetaSkill proposal lifecycle as the source of
   truth for installable workflows.
2. Add a lightweight author entry that can turn a successful conversation,
   MetaSkill run, or run summary into a proposal draft.
3. Support small proposal improvements through patch/edit flows instead of
   forcing full regeneration.
4. Add a lightweight bundle/profile layer for repeated skill combinations that
   do not need a DAG.
5. Extend existing conditional visibility metadata so skills and MetaSkills only
   surface when their tool and platform requirements can be satisfied.
6. Add evaluation and benchmark loops for creator output quality, using
   existing `eval_prompts`, `output_contract`, and runtime E2E infrastructure.

## Non-Goals

- Do not replace MetaSkills with Hermes-style bundles. Bundles are a lighter
  layer for non-DAG workflows.
- Do not let agent-managed edits bypass proposal gates.
- Do not auto-install user-visible skills from raw conversation history.
- Do not enable arbitrary shell-based dynamic context injection by default.
- Do not merge all ordinary skills and MetaSkills into one internal type.

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

- `eval_prompts` supply positive and negative activation cases.
- `output_contract` defines required final sections and artifacts.
- runtime E2E compares candidate MetaSkill output against a no-meta baseline.
- benchmark mode compares proposal revision A/B outputs over the same prompts.

This borrows Claude's create/eval/improve/benchmark lifecycle while keeping
OpenSquilla's proposal gates.

## Data Flow

### Conversation or Run to Proposal

1. User clicks or asks "turn this run into a MetaSkill draft."
2. Draft seed helper summarizes the observed workflow.
3. `meta-skill-creator` receives the seed as structured intent.
4. Existing slot filling and assembly produce a candidate.
5. Existing gates run according to creator mode.
6. User reviews preview or pending proposal.
7. Accept flow promotes only eligible proposals unless force is explicit.

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

## Error Handling

- If a draft seed lacks a clear goal, output shape, or trigger boundary, pause
  for structured clarification.
- If a proposed patch changes step topology, rerun all gates instead of only
  lint.
- If requirements are unsatisfied, refuse auto-enable and expose the unmet
  requirement in CLI/WebUI.
- If benchmark prompts are missing, generate only a preview and mark benchmark
  unavailable.
- If runtime E2E context is unavailable, persist only as an ineligible proposal
  with the failure reason preserved.
- If a bundle attempts to express step dependencies, recommend conversion to a
  MetaSkill rather than adding hidden DAG semantics to bundles.

## Testing Strategy

1. Parser and loader tests preserve new metadata fields.
2. Draft seed tests convert run summaries into deterministic seed payloads.
3. Creator DAG tests verify seed payloads reach slot filling without losing raw
   user constraints.
4. Proposal patch tests verify revisions preserve lineage and rerun gates.
5. Bundle tests verify model-visible summaries and CLI/WebUI listing behavior.
6. Conditional visibility tests cover satisfied, missing, and fallback cases.
7. Benchmark tests compare two proposal revisions over fixed eval prompts.
8. Regression tests ensure existing `PREVIEW_ONLY`, `PERSISTED_PROPOSAL`, and
   `FULL_GATED` behavior remains unchanged.

## Rollout Plan

### P0: Author Entry

Implement draft seeds from run summaries and conversations. Feed seeds into the
existing creator DAG. Do not add new proposal mutation or bundle behavior yet.

### P1: Proposal Iteration

Add patch/edit proposal revisions, benchmark mode, and eval-prompt reuse. Keep
promotion rules unchanged.

### P2: Lightweight Workflow Profiles

Add skill bundles and conditional visibility metadata. Start with CLI/WebUI
visibility and prompt-injection behavior before any automatic routing changes.

### P3: Learning Loop

Use repeated successful patterns, user corrections, and benchmark results to
suggest proposal drafts or revisions. Suggestions remain reviewable proposals,
not automatic installed skills.

## Success Criteria

- A successful run can become a reviewable MetaSkill draft without manual YAML
  authoring.
- Pending proposals can be improved through bounded patches.
- Users can represent repeated skill co-loading as bundles without building a
  full MetaSkill.
- Auto-enable remains at least as conservative as today.
- Creator quality can be compared over eval prompts before promotion.
- Missing tools or platform requirements are visible before invocation.

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

## Evidence Anchors

- OpenSquilla already supports creator modes, proposal gates, runtime E2E,
  auto-enable eligibility, and a first slice of conditional activation metadata.
- Claude Code contributes the skill/command unification, dynamic context,
  isolated skill execution, and create/eval/improve/benchmark lifecycle.
- Hermes Agent contributes agent-managed skill patching, skill bundles,
  conditional requirements, hub/tap lifecycle, and procedural-memory learning.
