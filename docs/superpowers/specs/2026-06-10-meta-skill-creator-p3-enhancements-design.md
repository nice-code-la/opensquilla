# Meta Skill Creator P3 Enhancements Design

## Goal

Make the meta-skill creator learn from its own proposal lifecycle, make its generated visibility hints affect runtime selection, expand read-only maintenance audits, and make manual approval cards easier to review by using request summaries as titles.

## Scope

This pass changes four narrow surfaces:

- Creator learning memory: summarize sanitized events from `creator-learning/events.jsonl` and feed the summary into `meta-skill-creator` automatically.
- Conditional visibility: treat creator-authored `metadata.opensquilla.requires_toolsets` as the runtime `requires_tools` source, and `fallback_for_tools` as `fallback_for_toolsets`, while preserving existing canonical fields.
- Proposal drift audit: add long-pending proposal and repeated learning-signal issues to the read-only audit report.
- Manual gate review UI: render an approval card title from `item.summary` first, then fall back to the current tool/action name.

## Architecture

Learning summary stays in `opensquilla.skills.proposals_lib` because that module already owns proposal lifecycle persistence. A hidden creator tool exposes the summary to the meta DAG, and the bundled `meta-skill-creator` plan calls it before slot filling.

Visibility aliasing stays in `SkillLoader` source parsing so generated skills behave correctly everywhere existing `SkillSpec.requires_tools` filtering is used. No new runtime eligibility path is added for `config_keys`; those remain advisory metadata until the project defines a config-gating contract.

Audit expansion stays read-only and deterministic. Proposal age uses the proposal `SKILL.md` mtime, with a default threshold of 14 days. Repeated learning signals are derived from sanitized JSONL rows and reported when the same rollback/failure signal repeats for the same skill or reason.

The approvals frontend change is purely presentational: `_renderApproval` chooses a human-readable title from `summary`, with the existing tool/action fallback to preserve older payloads.

## Testing

Use TDD for each behavior:

- Unit tests for summary generation, repeated-signal audit, and long-pending audit.
- Loader test for visibility aliases.
- Creator tool registration and DAG tests for automatic learning summary flow.
- Static approvals-view test that verifies summary-first title rendering.

## Non-Goals

- No automatic promotion, acceptance, or rollback policy changes.
- No environment/config-key hard gating.
- No approval API shape migration; the UI accepts summary when present and keeps backward compatibility.

