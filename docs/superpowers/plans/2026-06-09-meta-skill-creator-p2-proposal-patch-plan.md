# Meta-Skill Creator P2 Proposal Patch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators and creator flows apply bounded structured edits to pending MetaSkill proposals, producing reviewable proposal revisions with lineage and stale gate protection.

**Architecture:** P2 extends the existing proposal store in `opensquilla.skills.proposals_lib`; it does not edit installed managed or bundled skills. A patch request parses a pending proposal's `SKILL.md`, applies only allowlisted frontmatter/body edits, writes a new proposal directory, records lineage in `gates.json`, reruns cheap lint when possible, and marks affected gates stale so acceptance still requires review or explicit force. CLI and the internal `skill-creator-proposals` wrapper expose the same library function.

**Tech Stack:** Python 3, existing proposal directories, PyYAML, `skill-creator-linter` via `meta_skill_lint_run`, pytest, Typer CLI tests, subprocess tests for bundled proposal script.

---

## File Structure

- Modify `src/opensquilla/skills/proposals_lib.py`
  - Add `patch_proposal(home, proposal_id, patch_request)` as the single library boundary.
  - Parse and re-render SKILL.md frontmatter while preserving body text.
  - Support small patch operations: `set_description`, `add_triggers`, `remove_triggers`, `merge_metadata_opensquilla`, `merge_output_contract`, `append_eval_prompts`, and `append_body`.
  - Write a new proposal revision with `revision` lineage metadata and stale gate markers.
- Modify `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
  - Add `--action patch`, `--patch-json`, `--patch-file`, and `--owner`.
  - Delegate to `proposals_lib.patch_proposal`.
- Modify `src/opensquilla/skills/bundled/skill-creator-proposals/SKILL.md`
  - Document the patch action and update entrypoint args so the tool can pass patch input.
- Modify `src/opensquilla/cli/skills_meta_cmd.py`
  - Add `opensquilla skills meta proposals patch <id> --patch-json ... [--json]`.
  - Keep ID validation and pending-proposal-only behavior.
- Modify `src/opensquilla/skills/creator/proposer.py`
  - Expose `meta_skill_patch_proposal` as an internal tool.
- Modify `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
  - Add `PATCH_PROPOSAL` to creator mode choices and route patch mode to `meta_skill_patch_proposal`.
- Test `tests/test_skills/test_proposals_lib.py`
  - Patch creates a new revision and preserves the parent.
  - Unsafe/freeform operations are refused.
  - Stale gates block normal acceptance.
- Test `tests/test_skills/test_meta_skill_proposals.py`
  - Bundled proposal script exposes `patch`.
- Test `tests/test_cli/test_skills_meta_runs.py`
  - CLI patch action returns the new proposal id and leaves the parent pending.
- Test `tests/test_skills/test_creator_proposer.py`
  - Internal patch tool is registered.
- Test `tests/test_skills/test_meta_skill_creator_e2e.py`
  - DAG has PATCH_PROPOSAL mode and patch route does not persist a fresh proposal.

---

### Task 1: Add Proposal Revision Patch Library

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `tests/test_skills/test_proposals_lib.py`

- [ ] **Step 1: Write failing tests**

Append tests that call `proposals_lib.patch_proposal()` with:

```python
patch = {
    "set_description": "Refined synthetic pipeline for proposal patch tests.",
    "add_triggers": ["refined synth trigger"],
    "remove_triggers": ["synth test trigger"],
    "merge_output_contract": {"required_sections": ["Summary", "Evidence"]},
    "append_eval_prompts": [{
        "name": "refined-positive",
        "prompt": "please use refined synth trigger",
        "rubric": ["Summary"],
    }],
    "merge_metadata_opensquilla": {"requires_tools": ["read_file"]},
    "append_body": "## Revision Notes\n\n- Tightened trigger and output contract.\n",
    "owner": "unit-test",
}
```

Assert the new proposal id differs from the parent, the parent `SKILL.md` is unchanged, the child frontmatter has revised description/triggers/output contract/eval prompts/metadata, child gates include `creator_mode == "PATCH_PROPOSAL"`, `revision.parent_proposal_id`, `revision.revision == 2`, `auto_enable_eligible is False`, and stale gate reasons block normal `accept_proposal()`.

Add a second test with `{"set_name": "evil"}` and assert `status == "refused"` plus no child proposal is created.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_proposals_lib.py::test_patch_proposal_creates_revision_and_stales_gates tests/test_skills/test_proposals_lib.py::test_patch_proposal_refuses_unsupported_operations -q
```

Expected: FAIL because `patch_proposal` is not defined.

- [ ] **Step 3: Implement library function**

Implement helpers in `proposals_lib.py`:

- `_split_skill_markdown(skill_md) -> (frontmatter, body)`
- `_render_skill_markdown(frontmatter, body) -> str`
- `_apply_patch_request(frontmatter, body, patch_request) -> (frontmatter, body, applied_ops)`
- `_stale_gate(name, previous, required=True) -> dict`
- `_next_revision(parent_gates, parent_id, patch_request) -> dict`
- `patch_proposal(home, proposal_id, patch_request) -> dict`

Allow only the operations listed in File Structure. Use `yaml.safe_load` / `yaml.safe_dump(sort_keys=False, allow_unicode=True)`. Generate the child with `atomic_write_proposal`. Run `meta_skill_lint_run(revised_skill_md)` for lint; if it cannot run, write a failed lint payload instead of pretending it passed. Mark smoke, collision, risk, generation, activation, and runtime E2E stale when not rerun.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_proposals_lib.py::test_patch_proposal_creates_revision_and_stales_gates tests/test_skills/test_proposals_lib.py::test_patch_proposal_refuses_unsupported_operations -q
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check src/opensquilla/skills/proposals_lib.py tests/test_skills/test_proposals_lib.py
```

Commit with Lore trailers.

### Task 2: Expose Patch Through Script and CLI

**Files:**
- Modify: `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
- Modify: `src/opensquilla/skills/bundled/skill-creator-proposals/SKILL.md`
- Modify: `src/opensquilla/cli/skills_meta_cmd.py`
- Modify: `tests/test_skills/test_meta_skill_proposals.py`
- Modify: `tests/test_cli/test_skills_meta_runs.py`

- [ ] **Step 1: Write failing script and CLI tests**

Script test: create a proposal through `write_proposal`, call:

```bash
python proposals.py --action patch --home <home> --proposal-id <id> --patch-json '{"add_triggers":["patched trigger"],"owner":"script-test"}'
```

Assert JSON returns `status: ok`, `parent_proposal_id`, `proposal_id`, and child gates revision owner `script-test`.

CLI test: set `OPENSQUILLA_STATE_DIR`, create a proposal under `<tmp>/proposals/<id>`, call:

```bash
opensquilla skills meta proposals patch <id> --patch-json '{"set_description":"Patched from CLI"}' --json
```

Assert JSON returns the child proposal and parent remains.

- [ ] **Step 2: Implement entrypoints**

Add script action `patch`, arguments `--patch-json`, `--patch-file`, `--owner`, and helper `_load_patch_request(args)`. In CLI `proposals_cmd`, accept `action == "patch"` and forward parsed JSON to `proposals_lib.patch_proposal`.

- [ ] **Step 3: Run tests and commit**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_skill_proposals.py::test_patch_action_creates_revision tests/test_cli/test_skills_meta_runs.py::test_proposals_patch_cli_creates_revision -q
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py src/opensquilla/cli/skills_meta_cmd.py tests/test_skills/test_meta_skill_proposals.py tests/test_cli/test_skills_meta_runs.py
```

Commit with Lore trailers.

### Task 3: Wire PATCH_PROPOSAL Into Creator Tools and DAG

**Files:**
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Modify: `tests/test_skills/test_creator_proposer.py`
- Modify: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [ ] **Step 1: Write failing tool/DAG tests**

Add a proposer test that the tool registry exposes `meta_skill_patch_proposal` with params `proposal_id`, `patch_json`, `home`.

Add a DAG test that `creator_mode` choices include `PATCH_PROPOSAL`, that `patch` step uses `meta_skill_patch_proposal`, and that `persist` is guarded to exclude `PATCH_PROPOSAL`.

- [ ] **Step 2: Implement tool and DAG route**

Add `meta_skill_patch_proposal(proposal_id, patch_json, home="") -> str`, parsing JSON and delegating to `proposals_lib.patch_proposal`. Register it as `exposed_by_default=False`.

In `meta-skill-creator/SKILL.md`, add `PATCH_PROPOSAL` to creator mode choices, include instructions to choose it only when draft seed duplicate advice or user wording targets an existing pending proposal, add a `patch` tool step, and update `persist.when` to `outputs.creator_mode not in ('PREVIEW_ONLY', 'PATCH_PROPOSAL')`.

- [ ] **Step 3: Run tests and commit**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_creator_proposer.py::test_patch_proposal_tool_registered tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_routes_patch_proposal_without_persist -q
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check src/opensquilla/skills/creator/proposer.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py
```

Commit with Lore trailers.

### Task 4: Focused P2 Verification and Review

**Files:**
- All files touched in Tasks 1-3

- [ ] **Step 1: Run focused P2 tests**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest \
  tests/test_skills/test_proposals_lib.py \
  tests/test_skills/test_meta_skill_proposals.py \
  tests/test_cli/test_skills_meta_runs.py::test_proposals_patch_cli_creates_revision \
  tests/test_skills/test_creator_proposer.py::test_patch_proposal_tool_registered \
  tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_routes_patch_proposal_without_persist \
  -q
```

- [ ] **Step 2: Run lint**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check \
  src/opensquilla/skills/proposals_lib.py \
  src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py \
  src/opensquilla/cli/skills_meta_cmd.py \
  src/opensquilla/skills/creator/proposer.py \
  tests/test_skills/test_proposals_lib.py \
  tests/test_skills/test_meta_skill_proposals.py \
  tests/test_cli/test_skills_meta_runs.py \
  tests/test_skills/test_creator_proposer.py \
  tests/test_skills/test_meta_skill_creator_e2e.py
```

- [ ] **Step 3: Request final code review**

Ask an independent reviewer to inspect the P2 diff for proposal-store safety, path traversal, stale gate semantics, lineage correctness, and DAG route regressions. Fix any Critical or Important findings before final response.

