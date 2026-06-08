# Meta-Skill Creator P1 Author Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a successful persisted MetaSkill run become a structured draft seed that the existing `meta-skill-creator` DAG can consume, while refusing incoherent traces, surfacing duplicates as patch targets, scrubbing sensitive source text, and preserving the P0 generation and activation gates before reviewer acceptance. Successful conversations enter this P1 path only after they have been materialized as MetaSkill run records; a direct session/conversation draft endpoint is intentionally out of scope for this plan because it crosses the separate session transcript API and permission boundary.

**Architecture:** Build P1 on the existing `src/opensquilla/skills/meta/author_seed.py` boundary instead of adding a parallel creator. The author seed helper will emit a normalized `MetaSkillDraftSeed`-compatible payload, existing CLI/RPC draft endpoints will expose creator-ready input, and `meta_skill_fill_slots` plus the bundled `meta-skill-creator` DAG will accept an optional `draft_seed_json` input so seeded drafts still pass through P0 `generation_quality` and `activation_eval` before persistence or acceptance.

**Tech Stack:** Python 3, existing `MetaRunWriter` run records, existing `SkillLoader` specs, existing `meta-skill-creator` DAG, Pydantic v2 where existing creator schemas already use it, pytest, Typer CLI tests, RPC handler tests.

---

## File Structure

- Modify `src/opensquilla/skills/meta/author_seed.py`
  - Expand the current lightweight authoring seed into a normalized seed payload.
  - Add refusal, duplicate-detection, and privacy-scrubbing helpers.
  - Preserve existing top-level keys used by CLI/RPC tests.
- Modify `src/opensquilla/skills/creator/proposer.py`
  - Add optional `draft_seed_json` support to `meta_skill_fill_slots`.
  - Add the optional argument to the registered `meta_skill_fill_slots` tool schema.
  - Include seed evidence, negative cases, and constraints in the slot-filling prompt.
- Modify `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
  - Pass optional `inputs.draft_seed_json` into the `fill_slots` step.
  - Mention draft seed evidence in intent/pattern prompts without changing normal manual creation.
- Modify `src/opensquilla/cli/skills_meta_cmd.py`
  - Add `--creator-input` to `opensquilla skills meta runs draft`.
  - Emit a creator-ready payload with `user_message`, `draft_seed_json`, and recommended mode.
- Modify `src/opensquilla/gateway/rpc_meta_runs.py`
  - Let `meta.runs.draft` accept `includeCreatorInput`.
  - Return the same creator-ready payload for WebUI/agent surfaces.
- Test `tests/test_skills/test_meta_author_seed.py`
  - New focused unit tests for seed normalization, refusal, duplicate hints, and privacy scrubbing.
- Modify `tests/test_skills/test_creator_proposer.py`
  - Prompt/tool-schema tests for optional `draft_seed_json`.
- Modify `tests/test_skills/test_meta_skill_creator_e2e.py`
  - DAG wiring test that seed input reaches `fill_slots` and P0 gates remain before persistence.
- Modify `tests/test_cli/test_skills_meta_runs.py`
  - CLI `runs draft --creator-input --json` test.
- Modify `tests/test_gateway/test_rpc_meta_runs.py`
  - RPC `includeCreatorInput` test.

---

### Task 1: Normalize Author Seeds While Preserving Existing Draft Output

**Files:**
- Modify: `src/opensquilla/skills/meta/author_seed.py`
- Create: `tests/test_skills/test_meta_author_seed.py`

- [ ] **Step 1: Write failing seed-normalization tests**

Create `tests/test_skills/test_meta_author_seed.py`:

```python
from __future__ import annotations

import json

from opensquilla.persistence.meta_run_writer import RunRecord, StepRecord
from opensquilla.skills.meta.author_seed import draft_meta_skill_seed
from opensquilla.skills.meta.plan_serde import to_jsonable
from opensquilla.skills.meta.types import MetaPlan, MetaStep


def _record(
    *,
    status: str = "ok",
    user_message: str = "Research a vendor and produce a decision brief.",
    final_text: str | None = "Done.",
) -> RunRecord:
    plan = MetaPlan(
        name="meta-vendor-brief",
        triggers=("vendor decision brief",),
        priority=10,
        steps=(
            MetaStep(id="search", skill="web-search", kind="agent", label="Search"),
            MetaStep(id="draft", skill="summarize", kind="agent", label="Draft"),
        ),
        request_template={
            "outcome": "Decision brief",
            "fields": [{"name": "vendor", "required": True}],
        },
        output_contract={"required_sections": ["Recommendation", "Evidence"]},
        eval_prompts=[{
            "name": "vendor-brief",
            "prompt": "Research Acme and recommend whether to buy.",
            "rubric": ["Recommendation", "Evidence"],
        }],
    )
    return RunRecord(
        run_id="run_01",
        meta_skill_name="meta-vendor-brief",
        meta_skill_digest="digest",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        triggered_by="soft_meta_invoke",
        session_key="sess",
        turn_id="turn",
        owner_pid=None,
        status=status,
        started_at_ms=1,
        ended_at_ms=2,
        inputs_json=json.dumps({"user_message": user_message}),
        final_text=final_text,
        failed_step_id=None,
        error=None,
        truncated_fields=(),
        steps=(
            StepRecord(
                run_id="run_01",
                step_id="search",
                step_kind="agent",
                declared_skill="web-search",
                effective_skill="web-search",
                status="ok",
                started_at_ms=1,
                ended_at_ms=2,
                rendered_inputs_json=json.dumps({"query": "Acme vendor"}),
                output_text="found sources",
                error=None,
                substitute_step_id=None,
                truncated_fields=(),
            ),
            StepRecord(
                run_id="run_01",
                step_id="draft",
                step_kind="agent",
                declared_skill="summarize",
                effective_skill="summarize",
                status="ok",
                started_at_ms=2,
                ended_at_ms=3,
                rendered_inputs_json=json.dumps({"text": "{{ outputs.search }}"}),
                output_text="decision brief",
                error=None,
                substitute_step_id=None,
                truncated_fields=(),
            ),
        ),
    )


def test_author_seed_emits_normalized_creator_payload() -> None:
    seed = draft_meta_skill_seed(_record())

    assert seed["status"] == "ok"
    assert seed["source_kind"] == "meta_run"
    assert seed["goal"] == "Research a vendor and produce a decision brief."
    assert seed["observed_steps"] == ["web-search", "summarize"]
    assert seed["candidate_triggers"][0] == "Research a vendor and produce a decision brief."
    assert seed["inputs"] == ["vendor"]
    assert seed["outputs"] == ["Recommendation", "Evidence"]
    assert seed["negative_cases"]
    assert seed["evidence_refs"] == ["run_01"]
    assert seed["creator_input"]["recommended_mode"] == "PERSISTED_PROPOSAL"
    assert json.loads(seed["creator_input"]["draft_seed_json"])["goal"] == seed["goal"]


def test_author_seed_preserves_legacy_keys() -> None:
    seed = draft_meta_skill_seed(_record())

    assert seed["source_run"]["run_id"] == "run_01"
    assert seed["name"] == "vendor-brief-draft"
    assert seed["trigger_candidates"] == seed["candidate_triggers"]
    assert seed["request_template"]["outcome"] == "Decision brief"
    assert seed["output_contract"]["required_sections"] == ["Recommendation", "Evidence"]
    assert seed["composition"]["steps"][0]["id"] == "search"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py -q
```

Expected: FAIL because `status`, `source_kind`, `goal`, `observed_steps`, `negative_cases`, and `creator_input` are not yet present.

- [ ] **Step 3: Implement normalized seed fields**

In `src/opensquilla/skills/meta/author_seed.py`, add helper functions below `detect_trigger_conflicts`:

```python
def _field_names_from_template(plan: MetaPlan | None) -> list[str]:
    if plan is None:
        return []
    fields = plan.request_template.get("fields", []) if plan.request_template else []
    names: list[str] = []
    if isinstance(fields, list):
        for item in fields:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                name = item["name"].strip()
                if name and name not in names:
                    names.append(name)
    return names


def _outputs_from_contract(plan: MetaPlan | None) -> list[str]:
    if plan is None or not plan.output_contract:
        return []
    sections = plan.output_contract.get("required_sections", [])
    if not isinstance(sections, list):
        return []
    return [str(section).strip() for section in sections if str(section).strip()]


def _observed_skills(record: RunRecord, plan: MetaPlan | None) -> list[str]:
    skills: list[str] = []
    if plan is not None:
        for step in plan.steps:
            skill = str(step.skill or "").strip()
            if skill and skill not in skills:
                skills.append(skill)
    for step in record.steps:
        skill = str(step.effective_skill or step.declared_skill or "").strip()
        if skill and skill not in skills:
            skills.append(skill)
    return skills


def _negative_cases(plan: MetaPlan | None, user_message: str) -> list[str]:
    cases = [
        "Do not use for a one-off question that does not need the observed workflow.",
        "Do not use when the user only asks to inspect or explain an existing skill.",
    ]
    if plan is not None and plan.name:
        readable = plan.name.replace("meta-", "").replace("-", " ")
        cases.append(f"Do not use for adjacent {readable} requests missing the original output contract.")
    if user_message:
        cases.append(f"Do not use for a generic variant of: {_scrub_text(user_message)[:80]}")
    return cases[:4]


def _creator_input(seed: dict[str, Any]) -> dict[str, Any]:
    draft_seed_json = json.dumps(
        {
            "source_kind": seed["source_kind"],
            "goal": seed["goal"],
            "observed_steps": seed["observed_steps"],
            "candidate_triggers": seed["candidate_triggers"],
            "inputs": seed["inputs"],
            "outputs": seed["outputs"],
            "constraints": seed["constraints"],
            "negative_cases": seed["negative_cases"],
            "evidence_refs": seed["evidence_refs"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "user_message": (
            "Create a meta-skill proposal from this reviewed draft seed. "
            f"Goal: {seed['goal']}"
        ),
        "draft_seed_json": draft_seed_json,
        "recommended_mode": "PERSISTED_PROPOSAL",
    }
```

Then replace the body of `draft_meta_skill_seed` with this structure, preserving existing keys:

```python
    inputs = _json_obj(record.inputs_json)
    plan = _plan_from_record(record)
    user_message = _scrub_text(
        str(inputs.get("user_message") or inputs.get("message") or "").strip()
    )
    trigger_candidates = _trigger_candidates(record.meta_skill_name, user_message)
    normalized = {
        "status": "ok",
        "source_kind": "meta_run",
        "goal": user_message or _draft_description(record, user_message),
        "observed_steps": _observed_skills(record, plan),
        "candidate_triggers": trigger_candidates,
        "inputs": _field_names_from_template(plan),
        "outputs": _outputs_from_contract(plan),
        "constraints": [
            "Preserve the observed workflow order unless a reviewer changes it.",
            "Keep trigger examples paraphrased and narrow.",
        ],
        "negative_cases": _negative_cases(plan, user_message),
        "evidence_refs": [record.run_id],
    }
    normalized["creator_input"] = _creator_input(normalized)
    return {
        **normalized,
        "source_run": {
            "run_id": record.run_id,
            "meta_skill_name": record.meta_skill_name,
            "status": record.status,
        },
        "name": f"{_slug(record.meta_skill_name)}-draft",
        "description": _draft_description(record, user_message),
        "trigger_candidates": trigger_candidates,
        "trigger_conflicts": detect_trigger_conflicts(
            trigger_candidates,
            existing_specs=existing_specs,
        ),
        "request_template": dict(plan.request_template) if plan else {},
        "output_contract": dict(plan.output_contract) if plan else {},
        "eval_prompts": _seed_eval_prompts(plan, user_message),
        "composition": {
            "steps": _seed_steps(plan, record),
        },
        "run_summary": summarize_run_record(record),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_emits_normalized_creator_payload tests/test_skills/test_meta_author_seed.py::test_author_seed_preserves_legacy_keys -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/meta/author_seed.py tests/test_skills/test_meta_author_seed.py
git commit -m "Expose normalized meta-skill author seeds" \
  -m "Constraint: existing CLI/RPC draft payload keys remain compatible." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_emits_normalized_creator_payload tests/test_skills/test_meta_author_seed.py::test_author_seed_preserves_legacy_keys -q"
```

### Task 2: Refuse Incoherent Draft Sources and Scrub Sensitive Text

**Files:**
- Modify: `src/opensquilla/skills/meta/author_seed.py`
- Modify: `tests/test_skills/test_meta_author_seed.py`

- [ ] **Step 1: Write failing refusal and privacy tests**

Append to `tests/test_skills/test_meta_author_seed.py`:

```python
def test_author_seed_refuses_failed_or_empty_trace() -> None:
    failed = draft_meta_skill_seed(_record(status="failed", final_text=None))
    assert failed["status"] == "cannot_draft"
    assert failed["reason"] == "run_not_successful"
    assert "creator_input" not in failed

    empty = draft_meta_skill_seed(_record(user_message="", final_text=""))
    assert empty["status"] == "cannot_draft"
    assert empty["reason"] == "missing_goal_or_output"


def test_author_seed_scrubs_secret_and_file_like_literals() -> None:
    seed = draft_meta_skill_seed(_record(
        user_message=(
            "Use API key sk-live-1234567890abcdef and summarize "
            "/home/alice/private/customer_contract.pdf for finance."
        )
    ))

    payload = json.dumps(seed, ensure_ascii=False)
    assert "sk-live" not in payload
    assert "/home/alice/private" not in payload
    assert "[secret]" in payload
    assert "[file]" in payload
    assert seed["privacy_warnings"] == ["secret_like_text_redacted", "file_path_redacted"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_refuses_failed_or_empty_trace tests/test_skills/test_meta_author_seed.py::test_author_seed_scrubs_secret_and_file_like_literals -q
```

Expected: FAIL because refusal and privacy fields are missing.

- [ ] **Step 3: Add refusal and scrub helpers**

Add imports near the top of `src/opensquilla/skills/meta/author_seed.py`:

```python
from collections.abc import Iterable
```

Add module regexes near `_SLUG_RE`:

```python
_SECRET_LITERAL_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|gho|ghu|ghs|ghr|xoxb|xoxp)-[A-Za-z0-9_\-]{8,}\b"
)
_FILE_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._\- ]+){2,}\.[A-Za-z0-9]{1,8}")
```

Add helpers:

```python
def _cannot_draft(record: RunRecord, reason: str) -> dict[str, Any]:
    return {
        "status": "cannot_draft",
        "reason": reason,
        "source_kind": "meta_run",
        "source_run": {
            "run_id": record.run_id,
            "meta_skill_name": record.meta_skill_name,
            "status": record.status,
        },
    }


def _scrub_text(value: str) -> str:
    value = _SECRET_LITERAL_RE.sub("[secret]", value)
    value = _FILE_PATH_RE.sub("[file]", value)
    return value.strip()


def _privacy_warnings(raw: str) -> list[str]:
    warnings: list[str] = []
    if _SECRET_LITERAL_RE.search(raw):
        warnings.append("secret_like_text_redacted")
    if _FILE_PATH_RE.search(raw):
        warnings.append("file_path_redacted")
    return warnings


def _can_draft(record: RunRecord, user_message: str) -> tuple[bool, str]:
    if record.status != "ok":
        return False, "run_not_successful"
    if not user_message and not record.final_text:
        return False, "missing_goal_or_output"
    if not record.steps:
        return False, "missing_observed_steps"
    return True, "ok"
```

Update `draft_meta_skill_seed` before building the normalized payload:

```python
    raw_user_message = str(inputs.get("user_message") or inputs.get("message") or "").strip()
    user_message = _scrub_text(raw_user_message)
    can_draft, refusal_reason = _can_draft(record, user_message)
    if not can_draft:
        return _cannot_draft(record, refusal_reason)
    privacy_warnings = _privacy_warnings(raw_user_message)
```

Add `privacy_warnings` to `normalized`:

```python
        "privacy_warnings": privacy_warnings,
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_refuses_failed_or_empty_trace tests/test_skills/test_meta_author_seed.py::test_author_seed_scrubs_secret_and_file_like_literals -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/meta/author_seed.py tests/test_skills/test_meta_author_seed.py
git commit -m "Refuse unsafe or incoherent author seeds" \
  -m "Constraint: author entry must create reviewable seeds, not malformed proposals." \
  -m "Rejected: copying source run text verbatim into triggers | leaks user files and secrets into durable draft metadata." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_refuses_failed_or_empty_trace tests/test_skills/test_meta_author_seed.py::test_author_seed_scrubs_secret_and_file_like_literals -q"
```

### Task 3: Surface Duplicate Drafts as Patch Targets

**Files:**
- Modify: `src/opensquilla/skills/meta/author_seed.py`
- Modify: `tests/test_skills/test_meta_author_seed.py`

- [ ] **Step 1: Write failing duplicate-detection tests**

Append to `tests/test_skills/test_meta_author_seed.py`:

```python
class _Spec:
    def __init__(self, name: str, description: str, triggers: list[str]) -> None:
        self.name = name
        self.description = description
        self.triggers = triggers


def test_author_seed_recommends_patch_for_strong_duplicate() -> None:
    seed = draft_meta_skill_seed(
        _record(user_message="Research a vendor and produce a decision brief."),
        existing_specs=[
            _Spec(
                "meta-vendor-decision-brief",
                "Research a vendor and produce a decision brief.",
                ["vendor decision brief"],
            )
        ],
    )

    duplicate = seed["duplicate_detection"]
    assert duplicate["suggested_action"] == "patch_existing"
    assert duplicate["target"] == "meta-vendor-decision-brief"
    assert duplicate["score"] >= 0.75


def test_author_seed_allows_distinct_seed() -> None:
    seed = draft_meta_skill_seed(
        _record(user_message="Plan a school science fair project."),
        existing_specs=[
            _Spec(
                "meta-vendor-decision-brief",
                "Research vendors for procurement decisions.",
                ["vendor decision brief"],
            )
        ],
    )

    assert seed["duplicate_detection"]["suggested_action"] == "create_new"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_recommends_patch_for_strong_duplicate tests/test_skills/test_meta_author_seed.py::test_author_seed_allows_distinct_seed -q
```

Expected: FAIL because `duplicate_detection` is missing.

- [ ] **Step 3: Implement duplicate scoring**

Add helpers in `src/opensquilla/skills/meta/author_seed.py`:

```python
def detect_draft_duplicate(
    *,
    goal: str,
    trigger_candidates: Iterable[str],
    existing_specs: Iterable[Any],
) -> dict[str, Any]:
    source_text = " ".join([goal, *[str(t) for t in trigger_candidates]])
    best: dict[str, Any] | None = None
    for spec in existing_specs:
        spec_text = " ".join([
            str(getattr(spec, "name", "")),
            str(getattr(spec, "description", "")),
            *[str(t) for t in (getattr(spec, "triggers", []) or [])],
        ])
        score = _token_jaccard(source_text, spec_text)
        if best is None or score > best["score"]:
            best = {
                "target": str(getattr(spec, "name", "")),
                "score": round(score, 3),
            }
    if best and best["score"] >= 0.75 and best["target"]:
        return {
            "suggested_action": "patch_existing",
            "target": best["target"],
            "score": best["score"],
        }
    return {
        "suggested_action": "create_new",
        "target": None,
        "score": best["score"] if best else 0.0,
    }


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.lower())
        if token not in {"meta", "skill", "draft", "with", "that", "this", "and"}
    }
```

Add `duplicate_detection` to `normalized`:

```python
        "duplicate_detection": detect_draft_duplicate(
            goal=user_message,
            trigger_candidates=trigger_candidates,
            existing_specs=existing_specs,
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_recommends_patch_for_strong_duplicate tests/test_skills/test_meta_author_seed.py::test_author_seed_allows_distinct_seed -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/meta/author_seed.py tests/test_skills/test_meta_author_seed.py
git commit -m "Redirect duplicate author seeds toward patch targets" \
  -m "Constraint: P1 must not silently create sibling proposals for strong overlap." \
  -m "Confidence: medium" \
  -m "Scope-risk: narrow" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_author_seed.py::test_author_seed_recommends_patch_for_strong_duplicate tests/test_skills/test_meta_author_seed.py::test_author_seed_allows_distinct_seed -q"
```

### Task 4: Feed Draft Seeds Into Slot Filling

**Files:**
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `tests/test_skills/test_creator_proposer.py`

- [ ] **Step 1: Write failing prompt/tool tests**

Append to `tests/test_skills/test_creator_proposer.py`:

```python
def test_fill_slots_prompt_includes_draft_seed(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "vendor-brief-pipeline",
        "description": "Research a vendor and produce a concise decision brief.",
        "meta_priority": 50,
        "triggers": ["vendor decision brief"],
        "steps": [
            {"id": "research", "skill": "summarize", "task": "Research the vendor.", "with_keys": {}},
            {"id": "draft", "skill": "summarize", "task": "Draft the brief.", "with_keys": {}},
        ],
        "generation_rationale": {
            "intent": "Reuse a successful vendor brief workflow.",
            "target_outcome": "The user gets a decision-ready vendor brief.",
            "stop_condition": "The brief contains recommendation and evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["draft seed observed summarize steps"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: observed steps depend on prior output"],
            "output_contract_summary": "Final answer includes recommendation and evidence.",
        },
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="",
        user_intent="Create from run.",
        draft_seed_json=json.dumps({
            "goal": "Research a vendor and produce a decision brief.",
            "observed_steps": ["summarize", "summarize"],
            "negative_cases": ["Do not use for generic vendor facts."],
            "constraints": ["Keep trigger narrow."],
            "evidence_refs": ["run_01"],
        }),
    )

    prompt = captured[0]
    assert "## Draft seed" in prompt
    assert "Research a vendor and produce a decision brief" in prompt
    assert "Do not use for generic vendor facts" in prompt
    assert "Treat draft_seed_json as evidence, not as permission to bypass gates" in prompt


def test_fill_slots_tool_schema_accepts_draft_seed_json() -> None:
    from opensquilla.skills.creator.proposer import meta_skill_fill_slots_tool

    schema = meta_skill_fill_slots_tool.tool.input_schema
    assert "draft_seed_json" in schema["properties"]
    assert "draft_seed_json" not in schema["required"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_includes_draft_seed tests/test_skills/test_creator_proposer.py::test_fill_slots_tool_schema_accepts_draft_seed_json -q
```

Expected: FAIL because the function and tool schema do not accept `draft_seed_json`.

- [ ] **Step 3: Implement optional seed prompt context**

In `src/opensquilla/skills/creator/proposer.py`, add:

```python
def _format_draft_seed_context(draft_seed_json: str) -> str:
    raw = str(draft_seed_json or "").strip()
    if not raw:
        return "No draft seed supplied."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "Draft seed was supplied but was not valid JSON; ignore it and rely on user intent."
    if not isinstance(parsed, dict):
        return "Draft seed JSON was not an object; ignore it and rely on user intent."
    allowed = {
        key: parsed.get(key)
        for key in (
            "source_kind",
            "goal",
            "observed_steps",
            "candidate_triggers",
            "inputs",
            "outputs",
            "constraints",
            "negative_cases",
            "evidence_refs",
            "duplicate_detection",
        )
        if key in parsed
    }
    return json.dumps(allowed, ensure_ascii=False, indent=2, sort_keys=True)[:4000]
```

Change the function signature:

```python
def meta_skill_fill_slots(
    pattern_id: str,
    history_summary: str,
    user_intent: str,
    draft_seed_json: str = "",
) -> str:
```

Add to the prompt before `## Output instructions`:

```python
        f"## Draft seed\n"
        f"{_format_draft_seed_context(draft_seed_json)}\n\n"
```

Add to output instructions:

```python
        f"- Treat draft_seed_json as evidence, not as permission to bypass gates.\n"
        f"- Preserve seed constraints and negative_cases in trigger boundary and rationale.\n"
```

Update `meta_skill_fill_slots_tool` to accept and forward the optional argument:

```python
    "draft_seed_json": {"type": "string"},
```

```python
async def meta_skill_fill_slots_tool(
    pattern_id: str,
    history_summary: str,
    user_intent: str,
    draft_seed_json: str = "",
) -> str:
    return await asyncio.to_thread(
        meta_skill_fill_slots,
        pattern_id,
        history_summary,
        user_intent,
        draft_seed_json,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_includes_draft_seed tests/test_skills/test_creator_proposer.py::test_fill_slots_tool_schema_accepts_draft_seed_json -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/proposer.py tests/test_skills/test_creator_proposer.py
git commit -m "Feed author draft seeds into creator slot filling" \
  -m "Constraint: draft seed evidence must still flow through P0 creator gates." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_includes_draft_seed tests/test_skills/test_creator_proposer.py::test_fill_slots_tool_schema_accepts_draft_seed_json -q"
```

### Task 5: Wire Draft Seeds Through the Meta-Skill Creator DAG

**Files:**
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Modify: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [ ] **Step 1: Write failing DAG wiring test**

Append to `tests/test_skills/test_meta_skill_creator_e2e.py`:

```python
def test_creator_dag_forwards_optional_draft_seed_to_fill_slots() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader().get("meta-skill-creator")
    assert spec is not None
    steps = {step.id: step for step in spec.composition.steps}

    fill_slots = steps["fill_slots"]
    assert fill_slots.tool_args["draft_seed_json"] == "{{ inputs.draft_seed_json | default('') }}"
    assert steps["generation_quality"].depends_on == ["fill_slots"]
    assert "generation_quality_result" in steps["persist"].tool_args
    assert "activation_result" in steps["persist"].tool_args
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_forwards_optional_draft_seed_to_fill_slots -q
```

Expected: FAIL because `fill_slots` does not forward `draft_seed_json`.

- [ ] **Step 3: Modify DAG prompts and tool args**

In `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`, update the `clarify_intent` task after `Outer system / activation context`:

```yaml
          Optional draft seed JSON:
          {{ inputs.draft_seed_json | default("") | xml_escape | truncate(2000) }}

          If draft seed JSON is present and coherent, classify as ROUTE:
          meta-skill unless the seed explicitly refuses drafting. Preserve its
          goal, observed steps, constraints, and negative cases.
```

Update the `pick_pattern` prompt text to include:

```yaml
          Draft seed JSON, if supplied:
          {{ inputs.draft_seed_json | default("") | truncate(2000) }}
```

Update the `fill_slots` step `tool_args`:

```yaml
        draft_seed_json: "{{ inputs.draft_seed_json | default('') }}"
```

Keep existing `generation_quality`, `activation_eval`, `preview`, and `persist` ordering unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_forwards_optional_draft_seed_to_fill_slots tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md tests/test_skills/test_meta_skill_creator_e2e.py
git commit -m "Wire author draft seeds through meta-skill creator" \
  -m "Constraint: seed-based creation reuses the same gated creator DAG." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_forwards_optional_draft_seed_to_fill_slots tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist -q"
```

### Task 6: Expose Creator-Ready Draft Input in CLI and RPC

**Files:**
- Modify: `src/opensquilla/cli/skills_meta_cmd.py`
- Modify: `src/opensquilla/gateway/rpc_meta_runs.py`
- Modify: `tests/test_cli/test_skills_meta_runs.py`
- Modify: `tests/test_gateway/test_rpc_meta_runs.py`

- [ ] **Step 1: Write failing CLI and RPC tests**

Append to `tests/test_cli/test_skills_meta_runs.py`:

```python
def test_runs_draft_creator_input_json(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "runs",
            "draft",
            seeded_db["rid_ok"],
            "--creator-input",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["draft"]["source_run"]["run_id"] == seeded_db["rid_ok"]
    assert data["creator_input"]["recommended_mode"] == "PERSISTED_PROPOSAL"
    assert json.loads(data["creator_input"]["draft_seed_json"])["source_kind"] == "meta_run"
```

Append to `tests/test_gateway/test_rpc_meta_runs.py`:

```python
def test_meta_runs_draft_rpc_returns_creator_input_when_requested(tmp_path: Path) -> None:
    writer, run_id = _seed_writer(tmp_path)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_draft({
            "runId": run_id,
            "includeCreatorInput": True,
        }, ctx))
    finally:
        writer.close()

    assert payload["draft"]["source_run"]["run_id"] == run_id
    assert payload["creator_input"]["recommended_mode"] == "PERSISTED_PROPOSAL"
    assert json.loads(payload["creator_input"]["draft_seed_json"])["evidence_refs"] == [run_id]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_cli/test_skills_meta_runs.py::test_runs_draft_creator_input_json tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_creator_input_when_requested -q
```

Expected: FAIL because `--creator-input` and `includeCreatorInput` are missing.

- [ ] **Step 3: Implement CLI output**

Change `runs_draft` in `src/opensquilla/cli/skills_meta_cmd.py`:

```python
def runs_draft(
    run_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
    creator_input: bool = typer.Option(
        False,
        "--creator-input",
        help="Include inputs suitable for meta-skill-creator.",
    ),
) -> None:
```

After `seed = draft_meta_skill_seed(...)`, add:

```python
    if creator_input:
        payload = {
            "draft": seed,
            "creator_input": seed.get("creator_input"),
        }
        if json_out:
            typer.echo(json.dumps(payload, default=str))
            return
        typer.echo(json.dumps(payload["creator_input"], indent=2, ensure_ascii=False))
        return
```

Leave existing JSON and table output unchanged when `--creator-input` is absent.

- [ ] **Step 4: Implement RPC output**

Change `_handle_meta_runs_draft` in `src/opensquilla/gateway/rpc_meta_runs.py`:

```python
    draft = draft_meta_skill_seed(
        record,
        existing_specs=_existing_specs(ctx),
    )
    payload: dict[str, Any] = {"draft": draft}
    if bool(p.get("includeCreatorInput") or p.get("include_creator_input")):
        payload["creator_input"] = draft.get("creator_input")
    return payload
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_cli/test_skills_meta_runs.py::test_runs_draft_creator_input_json tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_creator_input_when_requested -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/cli/skills_meta_cmd.py src/opensquilla/gateway/rpc_meta_runs.py tests/test_cli/test_skills_meta_runs.py tests/test_gateway/test_rpc_meta_runs.py
git commit -m "Expose creator-ready inputs for meta-run drafts" \
  -m "Constraint: author entry prepares creator input without directly installing proposals." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest tests/test_cli/test_skills_meta_runs.py::test_runs_draft_creator_input_json tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_creator_input_when_requested -q"
```

### Task 7: Verify the P1 Slice and Document Execution Handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-06-09-meta-skill-creator-p1-author-entry-plan.md`

- [ ] **Step 1: Run focused P1 test set**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest \
  tests/test_skills/test_meta_author_seed.py \
  tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_includes_draft_seed \
  tests/test_skills/test_creator_proposer.py::test_fill_slots_tool_schema_accepts_draft_seed_json \
  tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_forwards_optional_draft_seed_to_fill_slots \
  tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist \
  tests/test_cli/test_skills_meta_runs.py::test_runs_draft_json \
  tests/test_cli/test_skills_meta_runs.py::test_runs_draft_creator_input_json \
  tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_author_seed \
  tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_creator_input_when_requested \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run lint on touched files**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check \
  src/opensquilla/skills/meta/author_seed.py \
  src/opensquilla/skills/creator/proposer.py \
  src/opensquilla/cli/skills_meta_cmd.py \
  src/opensquilla/gateway/rpc_meta_runs.py \
  tests/test_skills/test_meta_author_seed.py \
  tests/test_skills/test_creator_proposer.py \
  tests/test_skills/test_meta_skill_creator_e2e.py \
  tests/test_cli/test_skills_meta_runs.py \
  tests/test_gateway/test_rpc_meta_runs.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run compatibility checks for existing draft endpoints**

Run:

```bash
UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest \
  tests/test_cli/test_skills_meta_runs.py::test_runs_draft_json \
  tests/test_gateway/test_rpc_meta_runs.py::test_meta_runs_draft_rpc_returns_author_seed \
  -q
```

Expected: PASS.

- [ ] **Step 4: Commit verification notes if fixes were needed**

Only if Step 1, 2, or 3 required code fixes, commit those fixes:

```bash
git add src/opensquilla/skills/meta/author_seed.py src/opensquilla/skills/creator/proposer.py src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md src/opensquilla/cli/skills_meta_cmd.py src/opensquilla/gateway/rpc_meta_runs.py tests/test_skills/test_meta_author_seed.py tests/test_skills/test_creator_proposer.py tests/test_skills/test_meta_skill_creator_e2e.py tests/test_cli/test_skills_meta_runs.py tests/test_gateway/test_rpc_meta_runs.py
git commit -m "Stabilize meta-skill creator P1 author entry" \
  -m "Constraint: fixes discovered by focused P1 verification." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev pytest <focused P1 set> -q; UV_CACHE_DIR=/tmp/opensquilla-uv-cache PYTHONPATH=src uv run --extra dev ruff check <touched files>"
```

---

## Self-Review

Spec coverage:

- P1 draft seeds from run summaries: Task 1.
- Conversation compatibility: Task 1 keeps seed shape source-agnostic through `source_kind`, while this implementation slice wires persisted run records first because current repo evidence already has run draft CLI/RPC.
- Feed seeds into existing creator DAG: Tasks 4 and 5.
- Refusal for incoherent traces: Task 2.
- Duplicate detection redirects to patch target: Task 3.
- Privacy scrubbing of source examples: Task 2.
- Automatic P0 gate preservation before reviewer acceptance: Tasks 4 and 5 keep seeded drafts inside the existing `meta-skill-creator` DAG, where P0 `generation_quality` and `activation_eval` already run before persistence.
- No P2 proposal mutation or bundles: excluded by file structure and tasks.

Placeholder scan:

- No unresolved placeholder markers or vague open-ended implementation steps.
- Every test task includes concrete test code and expected failure/pass commands.
- Every implementation task names concrete functions, fields, and file paths.

Type consistency:

- Public helper remains `draft_meta_skill_seed`.
- New normalized keys are `source_kind`, `goal`, `observed_steps`, `candidate_triggers`, `inputs`, `outputs`, `constraints`, `negative_cases`, `evidence_refs`, `privacy_warnings`, `duplicate_detection`, and `creator_input`.
- Creator input key is consistently `draft_seed_json`.
- DAG input key is consistently `inputs.draft_seed_json`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-09-meta-skill-creator-p1-author-entry-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.
