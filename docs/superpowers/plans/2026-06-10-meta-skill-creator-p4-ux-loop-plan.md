# Meta Skill Creator P4 UX Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make creator lifecycle state visible and actionable in WebUI/final replies while feeding failed creator attempts back into learning memory.

**Architecture:** Keep lifecycle facts in `proposals_lib`, expose read-only audit through `rpc_proposals`, render proposal health and dry-run hints in `skills.js`, enrich approval cards in `approvals.js`, and wire final creator status through `meta-skill-creator/SKILL.md`.

**Tech Stack:** Python, pytest, static JS tests, OpenSquilla JSON-RPC handlers, bundled meta-skill YAML.

---

### Task 1: Proposal Audit RPC

**Files:**
- Modify: `src/opensquilla/gateway/rpc_proposals.py`
- Test: `tests/test_gateway/test_rpc_proposals.py`

- [ ] Write failing test `test_audit_returns_drift_issues`.
- [ ] Run `.venv/bin/pytest tests/test_gateway/test_rpc_proposals.py::test_audit_returns_drift_issues -q`.
- [ ] Add `exec.proposals.audit` handler that calls `proposals_lib.audit_proposal_drift(_home())`.
- [ ] Re-run the test and commit.

### Task 2: Proposal Health in Skills WebUI

**Files:**
- Modify: `src/opensquilla/gateway/static/js/views/skills.js`
- Modify: `src/opensquilla/gateway/static/css/views/skills.css`
- Test: `tests/test_gateway/test_skills_view_static.py`

- [ ] Write static tests that `_loadProposals()` calls `exec.proposals.audit`, proposal rows render issue chips, and proposal detail includes a dry-run/sample prompt section.
- [ ] Run targeted static tests and confirm RED.
- [ ] Add `_proposalAudit`, `_proposalIssues`, `_renderProposalHealth`, `_renderProposalIssueChips`, and `_renderProposalDryRun`.
- [ ] Add minimal CSS for health strip / issue chips / dry-run block.
- [ ] Re-run tests and commit.

### Task 3: Persist-Aware Creator Final Response

**Files:**
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Test: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [ ] Write failing DAG test that `final_response` depends on `persist` and includes `outputs.persist`.
- [ ] Run the targeted test and confirm RED.
- [ ] Update final response dependency and template branch.
- [ ] Re-run the targeted meta-skill creator tests and commit.

### Task 4: Failed Creator Learning Events

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_skills/test_creator_proposer.py`

- [ ] Write failing tests for sanitized `failed` events and for `meta_skill_persist_proposal()` recording subprocess / invalid JSON / refused-write failures.
- [ ] Run targeted tests and confirm RED.
- [ ] Add `failed` to allowed learning event types and record failed persistence outputs when `home` is available.
- [ ] Re-run targeted tests and commit.

### Task 5: Approval Review Context

**Files:**
- Modify: `src/opensquilla/gateway/static/js/views/approvals.js`
- Test: `tests/test_gateway/test_approvals_view_static.py`

- [ ] Write failing static test that approval cards render optional reason/risk/source context.
- [ ] Run targeted test and confirm RED.
- [ ] Add `_approvalContext()` and render it under metadata when fields exist.
- [ ] Re-run approvals static tests and commit.

### Task 6: Proposal Lifecycle Affordances

**Files:**
- Modify: `src/opensquilla/gateway/static/js/views/skills.js`
- Test: `tests/test_gateway/test_skills_view_static.py`

- [ ] Write failing static test that proposal rows expose refresh/audit/patch/benchmark affordances without destructive defaults.
- [ ] Run targeted test and confirm RED.
- [ ] Add non-destructive action buttons that show command hints or detail panes for refresh/patch/benchmark/audit.
- [ ] Re-run static tests and commit.

### Final Verification

- [ ] Run `.venv/bin/pytest tests/test_gateway/test_rpc_proposals.py tests/test_gateway/test_skills_view_static.py tests/test_gateway/test_approvals_view_static.py tests/test_skills/test_proposals_lib.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py -q`.
- [ ] Run `.venv/bin/ruff check src/opensquilla/gateway/rpc_proposals.py src/opensquilla/skills/proposals_lib.py src/opensquilla/skills/creator/proposer.py tests/test_gateway/test_rpc_proposals.py tests/test_gateway/test_skills_view_static.py tests/test_gateway/test_approvals_view_static.py tests/test_skills/test_proposals_lib.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py`.
- [ ] Run `git diff --check` and `git status --short`.

