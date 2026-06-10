# Meta Skill Creator P4 UX Loop Design

## Goal

Turn the existing meta-skill creator lifecycle signals into visible, actionable user experience: proposal health appears in the WebUI, final creator replies state saved status, failed generation attempts become learning events, and review surfaces show enough context for confident decisions.

## Approved Scope

The user approved six UX improvements:

1. Expose proposal audit and learning signals in WebUI.
2. Include real saved status and proposal id in meta-skill creator final replies.
3. Record failed generation/persistence events, not only accepted, benchmarked, and rollback events.
4. Add WebUI access to proposal lifecycle operations that already exist in CLI/RPC-adjacent code paths.
5. Enrich manual gate review cards with more decision context beyond title summary.
6. Add a lightweight generated-skill dry-run/sample-prompt review affordance.

## Design

The implementation stays conservative and builds on existing contracts:

- Add a read-only `exec.proposals.audit` RPC that returns `proposals_lib.audit_proposal_drift()`.
- Load audit output in `skills.js` beside proposal rows, then render issue chips on affected proposals and a compact proposal-health strip above the queue.
- Keep proposal mutation actions small: add refresh/audit visibility first, and surface patch/benchmark affordances through copyable command hints rather than creating complex editors in this pass.
- Make `meta-skill-creator` final response depend on `persist` and include `outputs.persist` when persistence ran.
- Extend creator learning event types with `failed`; record failed persistence subprocess, invalid JSON output, and refused proposal writes as sanitized learning events.
- In approvals view, keep summary-first title and add optional reason/risk/source subtitles when payload fields are present.
- Add a dry-run/sample-prompt section to proposal detail by deriving trigger examples from `show_proposal()` frontmatter and gates, without executing the skill.

## Non-Goals

- No automatic acceptance or rejection policy changes.
- No full visual redesign of the Skills or Approvals views.
- No free-form WebUI patch editor in this pass.
- No live execution of generated meta-skills from the proposal detail dialog.

## Tests

Use TDD for each behavior:

- RPC audit handler test.
- Static Skills view tests for loading/rendering audit data and proposal action affordances.
- Meta-skill DAG test for persist-aware final response.
- Creator proposer tests for failed learning events.
- Static Approvals view test for extra context fields.
- Proposals library or Skills view test for dry-run/sample prompt metadata.

