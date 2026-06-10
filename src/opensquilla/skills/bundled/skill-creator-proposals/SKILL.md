---
name: skill-creator-proposals
description: "Internal tool (not user-invocable). Called by meta-skill-creator's persist/patch/refresh/benchmark steps and by `opensquilla skills meta proposals` CLI to manage `~/.opensquilla/proposals/`: write_proposal / list / show / patch / refresh / benchmark / accept / rollback. Returns JSON."
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/proposals.py
  args:
    - --action
    - "{{ with.action | default('write_proposal') }}"
    - --skill-md-inline
    - "{{ with.skill_md | default('') }}"
    - --lint-result
    - "{{ with.lint_result | default('{}') }}"
    - --smoke-result
    - "{{ with.smoke_result | default('{}') }}"
    - --creator-mode
    - "{{ with.creator_mode | default('') }}"
    - --acceptance-result
    - "{{ with.acceptance_result | default('') }}"
    - --runtime-e2e-result
    - "{{ with.runtime_e2e_result | default('') }}"
    - --bundle-files-json
    - "{{ with.bundle_files_json | default('') }}"
    - --proposal-id
    - "{{ with.proposal_id | default('') }}"
    - --skill-name
    - "{{ with.skill_name | default('') }}"
    - --replace
    - "{{ with.replace | default(false) }}"
    - --deprecates
    - "{{ with.deprecates | default('') }}"
    - --migration-notes
    - "{{ with.migration_notes | default('') }}"
    - --patch-json
    - "{{ with.patch_json | default('') }}"
    - --patch-file
    - "{{ with.patch_file | default('') }}"
    - --owner
    - "{{ with.owner | default('') }}"
    - --collision-result
    - "{{ with.collision_result | default('') }}"
    - --risk-result
    - "{{ with.risk_result | default('') }}"
    - --generation-quality-result
    - "{{ with.generation_quality_result | default('') }}"
    - --activation-result
    - "{{ with.activation_result | default('') }}"
    - --candidate-proposal-id
    - "{{ with.candidate_proposal_id | default('') }}"
    - --eval-prompts
    - "{{ with.eval_prompts | default('') }}"
    - --comparison-result
    - "{{ with.comparison_result | default('') }}"
  parse: json
  timeout: 30
---

# Skill Creator Proposals

CRUD for meta-skill proposal candidates at `~/.opensquilla/proposals/<id>/`.

## Actions

- `write_proposal --skill-md path --lint-result json --smoke-result json [--creator-mode FULL_GATED --acceptance-result text --runtime-e2e-result json --bundle-files-json object]` — atomic write to `~/.opensquilla/proposals/<uuid8>/{SKILL.md,gates.json}` plus optional bundle files such as `scripts/*.py`, `evals/*.json`, or `references/*.md`. Returns `{proposal_id, auto_enable_eligible}`. In `FULL_GATED` mode runtime E2E must show the meta-skill route wins or ties against the no-meta highest-tier baseline with no regressions.
- `list` — enumerate proposals with their eligibility flag and bundle summary
- `show --proposal-id <id>` — return a proposal's `SKILL.md`, gate payload, and optional bundle manifest/files
- `patch --proposal-id <id> (--patch-json json | --patch-file path) [--owner owner]` — apply an allowlisted structured patch to a pending proposal and write a child revision with stale gates
- `refresh --proposal-id <id> [--smoke-result json --collision-result text --risk-result text --generation-quality-result json --activation-result json --acceptance-result json --runtime-e2e-result json]` — refresh stale gates on a pending revision and recompute eligibility
- `benchmark --proposal-id <baseline-id> --candidate-proposal-id <candidate-id> [--eval-prompts json --comparison-result json]` — record a non-promoting A/B report under `~/.opensquilla/proposal-benchmarks/<uuid8>/benchmark.json`, reusing proposal `eval_prompts` when explicit prompts are omitted
- `accept --proposal-id <id> [--force] [--replace --owner owner --deprecates name[,name] --migration-notes text]` — move proposal to `~/.opensquilla/skills/<name>/` so it gets loaded by MANAGED layer; refuses if any gate failed (unless `--force`). `--replace` archives the existing managed skill and records `lifecycle.supersedes`, deprecation metadata, migration notes, and a rollback target.
- `rollback --skill-name <name>` — restore a managed skill from its recorded rollback target and archive the current revision

## Atomicity

write_proposal writes to `~/.opensquilla/.tmp/proposal-<id>/` then `os.rename()` to the final location, so a partial write leaves no orphan proposal dir.

## Fallback

If invoked from chat, manually create the proposals dir, copy SKILL.md, run the skill-creator-linter to populate gates.json by hand.
