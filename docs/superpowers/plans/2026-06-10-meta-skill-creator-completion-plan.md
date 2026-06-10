# Meta Skill Creator Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]` / `- [x]`) syntax for tracking.

**Goal:** Finish the remaining meta-skill-creator proposal lifecycle improvements: refreshed patch gates, lifecycle deprecation metadata, patch concurrency guards, bundle validation, conditional visibility, drift audits, and procedural learning feedback.

**Architecture:** Keep proposal lifecycle behavior centralized in `opensquilla.skills.proposals_lib`, with thin script/CLI/creator wrappers. Add deterministic local gates first, then expose them through the existing bundled proposal tool and `opensquilla skills meta proposals` command. Keep generated bundle assets as reviewable text files under the existing proposal directory.

**Tech Stack:** Python 3.12, pytest, Typer CLI, YAML/JSON proposal metadata, bundled `SKILL.md` meta plans.

---

### Task 1: Patch Gate Refresh

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
- Modify: `src/opensquilla/cli/skills_meta_cmd.py`
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_skills/test_meta_skill_proposals.py`
- Test: `tests/test_cli/test_skills_meta_runs.py`
- Test: `tests/test_skills/test_creator_proposer.py`

- [x] **Step 1: Write failing tests**

Add tests asserting `refresh_proposal_gates(home, proposal_id, generation_quality_result=..., activation_result=..., collision_result=..., risk_result=..., smoke_result=...)` updates stale gates on a patch child, clears `stale`, recomputes `auto_enable_eligible`, and still refuses acceptance when a required refreshed gate fails.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_skills/test_proposals_lib.py::test_refresh_proposal_gates_clears_patch_stale_gates -q
```

Expected: fail because `refresh_proposal_gates` does not exist.

- [x] **Step 3: Implement minimal refresh**

Add `refresh_proposal_gates(...) -> dict` in `proposals_lib.py`. It must validate proposal id, load pending proposal gates, normalize provided gate payloads through existing evaluator helpers, replace stale gate objects for provided gates, recompute `auto_enable_eligible` with `_enforce_required_creator_quality_gates`, write `gates.json`, and return `{status, proposal_id, auto_enable_eligible, refreshed}`.

- [x] **Step 4: Expose wrappers**

Add script action `refresh`, CLI action `opensquilla skills meta proposals refresh <id>`, and hidden creator function/tool `meta_skill_refresh_proposal_gates`.

- [x] **Step 5: Verify and commit**

Run proposal, CLI, creator wrapper, ruff, and `git diff --check`, then commit.

### Task 2: Lifecycle Deprecation Metadata

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
- Modify: `src/opensquilla/cli/skills_meta_cmd.py`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_cli/test_skills_meta_runs.py`

- [x] **Step 1: Write failing tests**

Add tests for `accept_proposal(..., replace=True, deprecates=["old-skill"], migration_notes="...")` recording `lifecycle.deprecates`, `lifecycle.migration_notes`, and archived target `lifecycle.deprecated_by`.

- [x] **Step 2: Implement metadata propagation**

Extend accept/script/CLI options without changing default accept behavior.

- [x] **Step 3: Verify and commit**

Run targeted proposal lifecycle tests, CLI tests, ruff, and commit.

### Task 3: Patch Concurrency Guards

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Test: `tests/test_skills/test_proposals_lib.py`

- [x] **Step 1: Write failing tests**

Add tests for `patch_proposal(..., expected_parent_revision=1)` accepting the current parent revision and refusing when the parent has a newer child revision marker.

- [x] **Step 2: Implement optimistic conflict detection**

Record `latest_child_proposal_id` and `latest_child_revision` on the parent gates after child creation. Refuse patch when `expected_parent_revision` does not match or an unacknowledged newer child exists.

- [x] **Step 3: Verify and commit**

Run patch tests, proposal tests, ruff, and commit.

### Task 4: Bundle Validation Enhancements

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Test: `tests/test_skills/test_proposals_lib.py`

- [x] **Step 1: Write failing tests**

Add tests that unsafe bundle content types, unreferenced script assets, and missing referenced bundle files produce `gates.bundle_validation.passed is False`.

- [x] **Step 2: Implement bundle validation gate**

Add deterministic `bundle_validation` with file count, reserved path diagnostics, and a simple reference check from `SKILL.md` to bundle file names.

- [x] **Step 3: Verify and commit**

Run bundle tests, proposal tests, ruff, and commit.

### Task 5: Conditional Visibility Metadata

**Files:**
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Test: `tests/test_skills/test_creator_proposer.py`
- Test: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [x] **Step 1: Write failing tests**

Add tests asserting generated frontmatter can include `metadata.opensquilla.requires_toolsets`, `fallback_for_tools`, `platforms`, and `config_keys`.

- [x] **Step 2: Update creator prompts and schema pass-through**

Keep fields optional and conservative; do not invent requirements when the user did not name tools/platform/config.

- [x] **Step 3: Verify and commit**

Run creator tests, meta serde, ruff, and commit.

### Task 6: Continuous Quality / Drift Audits

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/cli/skills_meta_cmd.py`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_cli/test_skills_meta_runs.py`

- [x] **Step 1: Write failing tests**

Add tests for `audit_proposal_drift(home)` reporting stale proposals, trigger collisions among pending proposals, and rollback-heavy managed skills.

- [x] **Step 2: Implement read-only audit**

Add a deterministic read-only report under library and CLI `proposals audit --json`.

- [x] **Step 3: Verify and commit**

Run audit tests, CLI tests, ruff, and commit.

### Task 7: Procedural Learning Feedback

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Test: `tests/test_skills/test_proposals_lib.py`
- Test: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [x] **Step 1: Write failing tests**

Add tests for `record_creator_learning_event(home, event)` storing compact JSONL feedback for accepted, benchmarked, and rolled-back proposals.

- [x] **Step 2: Implement JSONL memory**

Persist to `~/.opensquilla/creator-learning/events.jsonl`, sanitize fields, and include learning summary in `meta-skill-creator` context as optional input.

- [x] **Step 3: Verify and commit**

Run learning tests, meta DAG tests, ruff, and commit.
