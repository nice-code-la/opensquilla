# Meta Skill Creator P3 Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add creator learning summaries, runtime visibility aliases, richer proposal audits, and summary-first manual approval titles.

**Architecture:** Keep lifecycle logic in `proposals_lib`, creator orchestration in `creator/proposer.py` plus the bundled `meta-skill-creator` DAG, runtime alias parsing in `SkillLoader`, and approval rendering in the existing approvals view. Each behavior is covered by targeted tests before implementation.

**Tech Stack:** Python, pytest, YAML frontmatter parsing, OpenSquilla SkillLoader, static JS view tests.

---

### Task 1: Creator Learning Summary

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_skills/test_creator_proposer.py`
- Test: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [ ] **Step 1: Write failing summary test**

Add a test that records accepted, benchmarked, and rolled-back events, calls `creator_learning_summary(home)`, and expects a compact text summary containing event counts, lessons, and rollback reasons without raw secrets.

- [ ] **Step 2: Run failing summary test**

Run: `pytest tests/test_skills/test_proposals_lib.py::test_creator_learning_summary_compacts_recent_events -q`

- [ ] **Step 3: Implement summary function**

Add a deterministic reader for `creator-learning/events.jsonl`, bounded line parsing, compact counts, recent lessons, and rollback warnings. Export it in `__all__`.

- [ ] **Step 4: Add hidden tool and DAG step tests**

Assert `meta_skill_creator_learning_summary` is registered but hidden, and assert `meta-skill-creator` has a `learning_summary` step that feeds `outputs.learning_summary` into `fill_slots`.

- [ ] **Step 5: Implement hidden tool and DAG wiring**

Add the hidden tool wrapper in `creator/proposer.py`, then add the `learning_summary` tool-call step and make `fill_slots` depend on it.

- [ ] **Step 6: Verify and commit**

Run:
`pytest tests/test_skills/test_proposals_lib.py::test_creator_learning_summary_compacts_recent_events tests/test_skills/test_creator_proposer.py::test_creator_package_import_registers_tools tests/test_skills/test_creator_proposer.py::test_creator_tools_hidden_from_owner_default tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_forwards_optional_learning_summary_to_fill_slots -q`

### Task 2: Conditional Visibility Aliases

**Files:**
- Modify: `src/opensquilla/skills/loader.py`
- Test: `tests/test_skills_memory_contract.py`

- [ ] **Step 1: Write failing alias test**

Create a temporary bundled skill whose `metadata.opensquilla` contains `requires_toolsets` and `fallback_for_tools`, then assert the loaded `SkillSpec` exposes those through `requires_tools` and `fallback_for_toolsets`.

- [ ] **Step 2: Run failing alias test**

Run: `pytest tests/test_skills_memory_contract.py::test_loader_maps_creator_visibility_aliases_to_runtime_fields -q`

- [ ] **Step 3: Implement alias resolution**

In `SkillLoader._load_skill_dir`, prefer canonical `requires_tools` / `fallback_for_toolsets`, otherwise read creator aliases `requires_toolsets` / `fallback_for_tools`. Normalize only list inputs.

- [ ] **Step 4: Verify and commit**

Run: `pytest tests/test_skills_memory_contract.py::test_loader_maps_creator_visibility_aliases_to_runtime_fields tests/test_skills_memory_contract.py::test_memory_skill_is_parseable_and_gated_on_read_tools -q`

### Task 3: Audit Enhancement

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Test: `tests/test_skills/test_proposals_lib.py`

- [ ] **Step 1: Write failing audit tests**

Add one test for `long_pending_proposal` using a controlled `now_ms`, and one test for `repeated_learning_signal` using two rollback events with the same reason.

- [ ] **Step 2: Run failing audit tests**

Run: `pytest tests/test_skills/test_proposals_lib.py::test_audit_proposal_drift_reports_long_pending_proposals tests/test_skills/test_proposals_lib.py::test_audit_proposal_drift_reports_repeated_learning_signals -q`

- [ ] **Step 3: Implement audit additions**

Add optional `long_pending_days` and `now_ms` parameters. Include issue counts for long-pending proposals and repeated learning signals.

- [ ] **Step 4: Verify and commit**

Run: `pytest tests/test_skills/test_proposals_lib.py::test_audit_proposal_drift_reports_stale_collision_and_rollback_heavy tests/test_skills/test_proposals_lib.py::test_audit_proposal_drift_reports_long_pending_proposals tests/test_skills/test_proposals_lib.py::test_audit_proposal_drift_reports_repeated_learning_signals -q`

### Task 4: Approval Summary Titles

**Files:**
- Modify: `src/opensquilla/gateway/static/js/views/approvals.js`
- Test: `tests/test_gateway/test_approvals_view_static.py`

- [ ] **Step 1: Write failing static test**

Assert `_renderApproval` derives the card title from `item.summary || item.toolName || item.pluginId || item.actionKind || 'Unknown'`.

- [ ] **Step 2: Run failing static test**

Run: `pytest tests/test_gateway/test_approvals_view_static.py::test_approval_card_title_prefers_summary_over_tool_name -q`

- [ ] **Step 3: Implement summary-first title**

Replace the title source with a helper or local variable that prefers `summary`, then falls back to existing fields.

- [ ] **Step 4: Verify and commit**

Run: `pytest tests/test_gateway/test_approvals_view_static.py -q`

### Task 5: Final Verification

**Files:**
- Validate changed files and core meta-skill tests.

- [ ] **Step 1: Run targeted suite**

Run:
`pytest tests/test_skills/test_proposals_lib.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py tests/test_skills_memory_contract.py tests/test_gateway/test_approvals_view_static.py -q`

- [ ] **Step 2: Run lint and whitespace checks**

Run:
`ruff check src/opensquilla/skills/proposals_lib.py src/opensquilla/skills/creator/proposer.py src/opensquilla/skills/loader.py tests/test_skills/test_proposals_lib.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py tests/test_skills_memory_contract.py`

Run:
`git diff --check`

- [ ] **Step 3: Final commit if needed**

Commit any remaining cleanup using the Lore commit protocol.

