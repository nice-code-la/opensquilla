# Meta-Skill Creator P0 Generation and Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the P0 foundation that makes `meta-skill-creator` emit reviewable generation rationale, run deterministic generation-quality checks, run catalog-aware activation checks, and persist both gates into proposal metadata.

**Architecture:** Keep P0 inside the existing creator/proposal boundary. Extend the slot schemas with optional `generation_rationale`, add two focused deterministic gate modules under `src/opensquilla/skills/creator/`, expose them through internal creator tools in `proposer.py`, wire the bundled `meta-skill-creator` DAG to run them before persistence, and store their results in `gates.json`.

**Tech Stack:** Python 3, Pydantic v2, existing `SkillLoader`, existing deterministic trigger matcher, existing proposal gate JSON, pytest.

---

## File Structure

- Modify `src/opensquilla/skills/creator/patterns/schemas.py`
  - Add `GenerationRationale` and optional `generation_rationale` fields to all slot schemas.
- Create `src/opensquilla/skills/creator/quality.py`
  - Deterministic generation-quality gate for validated slot JSON.
- Create `src/opensquilla/skills/creator/activation.py`
  - Deterministic activation gate for candidate `SKILL.md` plus catalog-adjacent negative prompts.
- Modify `src/opensquilla/skills/creator/proposer.py`
  - Prompt slot filler to emit rationale, add internal tool wrappers for the two new gates, and pass results to persistence.
- Modify `src/opensquilla/skills/proposals_lib.py`
  - Parse, evaluate, persist, show, and include the new gates in eligibility.
- Modify `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
  - Add CLI args for `generation_quality_result` and `activation_result`.
- Modify `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
  - Insert generation-quality and activation-eval DAG steps after `assemble`; include their outputs in preview and persist.
- Test `tests/test_skills/test_creator_proposer.py`
  - Schema/rationale and prompt tests.
- Test `tests/test_skills/test_creator_quality.py`
  - Generation-quality gate fixtures.
- Test `tests/test_skills/test_creator_activation.py`
  - Activation gate fixtures.
- Test `tests/test_skills/test_proposals_lib.py`
  - Gate persistence and eligibility behavior.
- Test `tests/test_skills/test_meta_skill_creator_e2e.py`
  - DAG wiring and persisted gate payloads.

---

### Task 1: Add Generation Rationale to Slot Schemas

**Files:**
- Modify: `src/opensquilla/skills/creator/patterns/schemas.py`
- Modify: `tests/test_skills/test_creator_proposer.py`

- [ ] **Step 1: Write failing schema tests**

Append these tests to `tests/test_skills/test_creator_proposer.py`:

```python
def test_sequential_slots_accept_generation_rationale() -> None:
    slots = SequentialSlots(
        name="test-rationale",
        description="Synthetic pipeline with explicit generation rationale.",
        triggers=["rationale trigger"],
        steps=[
            {"id": "a", "skill": "summarize", "task": "process input"},
            {"id": "b", "skill": "memory", "task": "save result"},
        ],
        generation_rationale={
            "intent": "Turn source material into a saved summary.",
            "target_outcome": "The user gets a concise summary and durable memory entry.",
            "stop_condition": "Summary saved and final response reports completion evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user asked for process then save"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": [
                "ordinary_skill: requires two existing skills in sequence",
            ],
            "output_contract_summary": "Final answer reports summary and save status.",
        },
    )

    assert slots.generation_rationale.selected_shape == "metaskill"
    assert slots.generation_rationale.selected_pattern == "p1_sequential"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_sequential_slots_accept_generation_rationale -q
```

Expected: FAIL with an error showing `SequentialSlots` has no `generation_rationale` attribute or cannot validate it as a structured model.

- [ ] **Step 3: Add rationale model and fields**

In `src/opensquilla/skills/creator/patterns/schemas.py`, add this model after `_check_yaml_safe`:

```python
class GenerationRationale(BaseModel):
    intent: str = Field(min_length=10, max_length=400)
    target_outcome: str = Field(min_length=10, max_length=400)
    stop_condition: str = Field(min_length=10, max_length=400)
    selected_shape: str = Field(pattern=r"^(ordinary_skill|metaskill|bundle|patch|refusal)$")
    selected_pattern: str = Field(min_length=2, max_length=80)
    source_evidence: list[str] = Field(min_length=1, max_length=8)
    filled_slots: list[str] = Field(min_length=1, max_length=16)
    unresolved_assumptions: list[str] = Field(default_factory=list, max_length=8)
    rejected_alternatives: list[str] = Field(min_length=1, max_length=8)
    output_contract_summary: str = Field(min_length=10, max_length=500)

    @field_validator(
        "intent",
        "target_outcome",
        "stop_condition",
        "selected_shape",
        "selected_pattern",
        "output_contract_summary",
    )
    @classmethod
    def _scalar_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "generation_rationale scalar")

    @field_validator(
        "source_evidence",
        "filled_slots",
        "unresolved_assumptions",
        "rejected_alternatives",
        mode="before",
    )
    @classmethod
    def _list_items_yaml_safe(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    _check_yaml_safe(item, "generation_rationale item")
        return v
```

Add this field to `SequentialSlots` and `FanOutMergeSlots`:

```python
generation_rationale: GenerationRationale | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_sequential_slots_accept_generation_rationale -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/patterns/schemas.py tests/test_skills/test_creator_proposer.py
git commit -m "Require rationale-compatible creator slot schemas" \
  -m "Constraint: rationale is optional for backward compatibility with existing generated slots." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_sequential_slots_accept_generation_rationale -q"
```

### Task 2: Prompt Slot Filler to Emit Rationale

**Files:**
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `tests/test_skills/test_creator_proposer.py`

- [ ] **Step 1: Write failing prompt test**

Append this test to `tests/test_skills/test_creator_proposer.py`:

```python
def test_fill_slots_prompt_requires_generation_rationale(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "ok-pipeline",
        "description": "x" * 50,
        "meta_priority": 50,
        "triggers": ["t"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "t", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "t", "with_keys": {}},
        ],
        "generation_rationale": {
            "intent": "Create a concise two-step workflow.",
            "target_outcome": "The user gets processed text and saved output.",
            "stop_condition": "The final step reports saved output.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user requested two-step workflow"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: steps depend on prior output"],
            "output_contract_summary": "Final answer reports processing and save status.",
        },
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )

    assert captured
    prompt = captured[0]
    assert "generation_rationale" in prompt
    assert "selected_shape" in prompt
    assert "rejected_alternatives" in prompt
    assert "Do not invent tools, gates, inputs, or output contracts" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_requires_generation_rationale -q
```

Expected: FAIL because the prompt does not yet mention `generation_rationale`.

- [ ] **Step 3: Update slot filler prompt**

In `src/opensquilla/skills/creator/proposer.py`, inside `base_prompt`, add this block before `CRITICAL field-name rules:`:

```python
        f"Generation rationale rules:\n"
        f"- Include `generation_rationale` unless the schema rejects it.\n"
        f"- Set generation_rationale.selected_shape to `metaskill` for these pattern schemas.\n"
        f"- Set generation_rationale.selected_pattern to the exact pattern_id: {pattern_id}.\n"
        f"- Fill source_evidence with request/history facts that justify the candidate.\n"
        f"- Fill rejected_alternatives with at least one nearby shape or skill and why it was not chosen.\n"
        f"- Do not invent tools, gates, inputs, or output contracts. If evidence is missing, put the gap in unresolved_assumptions.\n"
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_requires_generation_rationale tests/test_skills/test_creator_proposer.py::test_meta_skill_fill_slots_with_stub_llm -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/proposer.py tests/test_skills/test_creator_proposer.py
git commit -m "Ask meta-skill slot filler for generation rationale" \
  -m "Constraint: rationale remains schema-validated and optional for older fixtures." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_fill_slots_prompt_requires_generation_rationale tests/test_skills/test_creator_proposer.py::test_meta_skill_fill_slots_with_stub_llm -q"
```

### Task 3: Add Generation-Quality Gate

**Files:**
- Create: `src/opensquilla/skills/creator/quality.py`
- Create: `tests/test_skills/test_creator_quality.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_skills/test_creator_quality.py`:

```python
from __future__ import annotations

import json

from opensquilla.skills.creator.quality import evaluate_generation_quality


def _valid_slots() -> dict:
    return {
        "name": "synth-quality-pipeline",
        "description": "Synthetic quality pipeline that processes then saves output.",
        "meta_priority": 50,
        "triggers": ["quality pipeline"],
        "steps": [
            {"id": "process", "skill": "summarize", "task": "Process the source", "with_keys": {}},
            {"id": "save", "skill": "memory", "task": "Save the result", "with_keys": {}},
        ],
        "generation_rationale": {
            "intent": "Create a process then save workflow.",
            "target_outcome": "The user gets processed output saved for reuse.",
            "stop_condition": "Saved output and final evidence are available.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user requested process then save"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: later step depends on earlier output"],
            "output_contract_summary": "Final answer reports processed output and save status.",
        },
    }


def test_generation_quality_passes_valid_slots() -> None:
    result = evaluate_generation_quality("p1_sequential", json.dumps(_valid_slots()))

    assert result["passed"] is True
    assert result["reason"] == "ok"
    assert result["failures"] == []
    assert result["summary"]["selected_shape"] == "metaskill"


def test_generation_quality_fails_missing_rationale() -> None:
    slots = _valid_slots()
    slots.pop("generation_rationale")

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "missing_generation_rationale" in result["failures"]


def test_generation_quality_fails_creator_internal_skill() -> None:
    slots = _valid_slots()
    slots["steps"][0]["skill"] = "skill-creator-proposals"

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "creator_internal_skill:skill-creator-proposals" in result["failures"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_quality.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'opensquilla.skills.creator.quality'`.

- [ ] **Step 3: Implement quality gate**

Create `src/opensquilla/skills/creator/quality.py`:

```python
"""Deterministic generation-quality gates for meta-skill creator candidates."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from opensquilla.skills.creator.patterns import PATTERN_SLOT_SCHEMA

_CREATOR_INTERNAL_SKILLS = {
    "meta-skill-creator",
    "skill-creator-linter",
    "skill-creator-proposals",
    "skill-creator-smoke-test",
}


def evaluate_generation_quality(pattern_id: str, slots_json: str) -> dict[str, Any]:
    """Validate slot-level generation quality before SKILL.md assembly gates."""
    failures: list[str] = []
    if pattern_id not in PATTERN_SLOT_SCHEMA:
        return {
            "required": True,
            "passed": False,
            "reason": "unknown_pattern",
            "failures": [f"unknown_pattern:{pattern_id}"],
            "summary": {},
        }
    schema = PATTERN_SLOT_SCHEMA[pattern_id]
    try:
        raw = json.loads(slots_json)
        slots = schema.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        return {
            "required": True,
            "passed": False,
            "reason": "invalid_slots",
            "failures": ["invalid_slots"],
            "diagnostics": str(exc)[:1000],
            "summary": {},
        }

    data = slots.model_dump()
    rationale = data.get("generation_rationale")
    if not isinstance(rationale, dict) or not rationale:
        failures.append("missing_generation_rationale")
        rationale = {}
    if rationale.get("selected_shape") != "metaskill":
        failures.append(f"wrong_shape:{rationale.get('selected_shape') or 'missing'}")
    if rationale.get("selected_pattern") != pattern_id:
        failures.append(f"wrong_pattern:{rationale.get('selected_pattern') or 'missing'}")
    if not rationale.get("rejected_alternatives"):
        failures.append("missing_rejected_alternatives")
    if not rationale.get("output_contract_summary"):
        failures.append("missing_output_contract_summary")

    for skill in _referenced_skills(data):
        if skill in _CREATOR_INTERNAL_SKILLS:
            failures.append(f"creator_internal_skill:{skill}")

    passed = not failures
    return {
        "required": True,
        "passed": passed,
        "reason": "ok" if passed else "generation_quality_failed",
        "failures": failures,
        "summary": {
            "name": data.get("name", ""),
            "selected_shape": rationale.get("selected_shape", ""),
            "selected_pattern": rationale.get("selected_pattern", ""),
            "rejected_alternatives": rationale.get("rejected_alternatives", []),
            "unresolved_assumptions": rationale.get("unresolved_assumptions", []),
        },
    }


def _referenced_skills(data: dict[str, Any]) -> list[str]:
    skills: list[str] = []
    for step in data.get("steps", []) or []:
        if isinstance(step, dict) and step.get("skill"):
            skills.append(str(step["skill"]))
    for branch in data.get("branches", []) or []:
        if isinstance(branch, dict) and branch.get("skill"):
            skills.append(str(branch["skill"]))
    merge = data.get("merge")
    if isinstance(merge, dict) and merge.get("skill"):
        skills.append(str(merge["skill"]))
    tail = data.get("tail")
    if isinstance(tail, dict) and tail.get("skill"):
        skills.append(str(tail["skill"]))
    return skills
```

- [ ] **Step 4: Run tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_quality.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/quality.py tests/test_skills/test_creator_quality.py
git commit -m "Add deterministic creator generation-quality gate" \
  -m "Constraint: P0 gate uses validated slot JSON and deterministic checks only." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_creator_quality.py -q"
```

### Task 4: Add Catalog-Aware Activation Gate

**Files:**
- Create: `src/opensquilla/skills/creator/activation.py`
- Create: `tests/test_skills/test_creator_activation.py`

- [ ] **Step 1: Write failing activation tests**

Create `tests/test_skills/test_creator_activation.py`:

```python
from __future__ import annotations

from opensquilla.skills.creator.activation import evaluate_candidate_activation


CANDIDATE_SKILL_MD = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
kind: meta
meta_priority: 50
triggers:
  - "alpha report"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""


def test_activation_gate_passes_positive_and_catalog_negative() -> None:
    result = evaluate_candidate_activation(
        CANDIDATE_SKILL_MD,
        positive_prompts=["please run the alpha report"],
        negative_prompts=["please run the beta digest"],
        catalog_negative_prompts=["please run the beta digest"],
        sample_count=3,
    )

    assert result["passed"] is True
    assert result["true_positive_rate"] == 1.0
    assert result["false_positive_rate"] == 0.0
    assert result["sample_count"] == 3
    assert result["catalog_negative_count"] == 1


def test_activation_gate_fails_false_positive() -> None:
    result = evaluate_candidate_activation(
        CANDIDATE_SKILL_MD,
        positive_prompts=["please run the alpha report"],
        negative_prompts=["alpha report but only explain the name"],
        catalog_negative_prompts=["alpha report but only explain the name"],
        sample_count=3,
    )

    assert result["passed"] is False
    assert result["false_positive_rate"] == 1.0
    assert "false_positive_rate_above_threshold" in result["failures"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_activation.py -q
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement activation gate**

Create `src/opensquilla/skills/creator/activation.py`:

```python
"""Activation checks for meta-skill creator candidates."""

from __future__ import annotations

from typing import Any

from opensquilla.skills.creator.proposer import simulate_meta_resolution


def evaluate_candidate_activation(
    skill_md: str,
    *,
    positive_prompts: list[str] | None = None,
    negative_prompts: list[str] | None = None,
    catalog_negative_prompts: list[str] | None = None,
    sample_count: int = 3,
    min_true_positive_rate: float = 0.85,
    max_false_positive_rate: float = 0.10,
) -> dict[str, Any]:
    """Evaluate deterministic trigger behavior for a candidate SKILL.md."""
    positives = [p for p in (positive_prompts or []) if str(p).strip()]
    negatives = [p for p in (negative_prompts or []) if str(p).strip()]
    catalog_negatives = [
        p for p in (catalog_negative_prompts or []) if str(p).strip()
    ]
    merged_negatives = _dedupe([*negatives, *catalog_negatives])
    if not positives:
        positives = ["please use this meta-skill"]
    if not merged_negatives:
        merged_negatives = ["tell me a joke unrelated to this workflow"]

    positive_hits = _count_matches(skill_md, positives, sample_count)
    negative_hits = _count_matches(skill_md, merged_negatives, sample_count)
    positive_total = len(positives) * sample_count
    negative_total = len(merged_negatives) * sample_count
    tpr = positive_hits / positive_total if positive_total else 1.0
    fpr = negative_hits / negative_total if negative_total else 0.0

    failures: list[str] = []
    if tpr < min_true_positive_rate:
        failures.append("true_positive_rate_below_threshold")
    if fpr > max_false_positive_rate:
        failures.append("false_positive_rate_above_threshold")
    if catalog_negatives and len(catalog_negatives) < max(1, len(merged_negatives) // 2):
        failures.append("catalog_negative_coverage_below_half")

    passed = not failures
    return {
        "required": True,
        "passed": passed,
        "reason": "ok" if passed else "activation_failed",
        "failures": failures,
        "true_positive_rate": tpr,
        "false_positive_rate": fpr,
        "sample_count": sample_count,
        "positive_count": len(positives),
        "negative_count": len(merged_negatives),
        "catalog_negative_count": len(catalog_negatives),
        "variance": 0.0,
        "budget": {
            "deterministic_checks": positive_total + negative_total,
        },
    }


def _count_matches(skill_md: str, prompts: list[str], sample_count: int) -> int:
    hits = 0
    for prompt in prompts:
        for _ in range(max(1, sample_count)):
            if simulate_meta_resolution(skill_md, prompt, "deterministic"):
                hits += 1
    return hits


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out
```

- [ ] **Step 4: Run activation tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_activation.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/activation.py tests/test_skills/test_creator_activation.py
git commit -m "Add deterministic creator activation gate" \
  -m "Constraint: P0 activation uses existing trigger matching, not learned routing." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_creator_activation.py -q"
```

### Task 5: Expose Quality and Activation Tools

**Files:**
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `tests/test_skills/test_creator_proposer.py`

- [ ] **Step 1: Write failing tool-registration test**

Append this test to `tests/test_skills/test_creator_proposer.py`:

```python
def test_creator_quality_and_activation_tools_registered() -> None:
    import importlib

    import opensquilla.skills.creator
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry

    names = set(get_default_registry().list_names())
    assert "meta_skill_generation_quality_run" in names
    assert "meta_skill_activation_eval_run" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_creator_quality_and_activation_tools_registered -q
```

Expected: FAIL because the two tools are not registered.

- [ ] **Step 3: Add sync functions and tool wrappers**

In `src/opensquilla/skills/creator/proposer.py`, import the new evaluators near the other local imports:

```python
from .activation import evaluate_candidate_activation
from .quality import evaluate_generation_quality
```

Add sync functions above the tool-decorated wrapper section:

```python
def meta_skill_generation_quality_run(pattern_id: str, slots_json: str) -> str:
    result = evaluate_generation_quality(pattern_id, slots_json)
    return json.dumps(result, ensure_ascii=False)


def meta_skill_activation_eval_run(
    skill_md: str,
    positive_prompts: str = "",
    negative_prompts: str = "",
    catalog_negative_prompts: str = "",
) -> str:
    result = evaluate_candidate_activation(
        skill_md,
        positive_prompts=_json_list_or_lines(positive_prompts),
        negative_prompts=_json_list_or_lines(negative_prompts),
        catalog_negative_prompts=_json_list_or_lines(catalog_negative_prompts),
    )
    return json.dumps(result, ensure_ascii=False)


def _json_list_or_lines(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [text]
```

Add tool wrappers:

```python
@tool(
    name="meta_skill_generation_quality_run",
    description="Run deterministic generation-quality checks on creator slot JSON.",
    params={
        "pattern_id": {"type": "string", "enum": _PATTERN_ENUM},
        "slots_json": {"type": "string"},
    },
    required=["pattern_id", "slots_json"],
    exposed_by_default=False,
)
async def meta_skill_generation_quality_run_tool(pattern_id: str, slots_json: str) -> str:
    import asyncio

    return await asyncio.to_thread(meta_skill_generation_quality_run, pattern_id, slots_json)


@tool(
    name="meta_skill_activation_eval_run",
    description="Run deterministic activation checks on a candidate SKILL.md.",
    params={
        "skill_md": {"type": "string"},
        "positive_prompts": {"type": "string"},
        "negative_prompts": {"type": "string"},
        "catalog_negative_prompts": {"type": "string"},
    },
    required=["skill_md"],
    exposed_by_default=False,
)
async def meta_skill_activation_eval_run_tool(
    skill_md: str,
    positive_prompts: str = "",
    negative_prompts: str = "",
    catalog_negative_prompts: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_activation_eval_run,
        skill_md,
        positive_prompts,
        negative_prompts,
        catalog_negative_prompts,
    )
```

- [ ] **Step 4: Run focused test**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_creator_quality_and_activation_tools_registered -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/opensquilla/skills/creator/proposer.py tests/test_skills/test_creator_proposer.py
git commit -m "Expose creator quality and activation gates as internal tools" \
  -m "Constraint: tools remain exposed_by_default=False for orchestrator-only dispatch." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_creator_proposer.py::test_creator_quality_and_activation_tools_registered -q"
```

### Task 6: Persist New Gates in Proposal Metadata

**Files:**
- Modify: `src/opensquilla/skills/proposals_lib.py`
- Modify: `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`
- Modify: `tests/test_skills/test_proposals_lib.py`

- [ ] **Step 1: Write failing proposal tests**

Append this test to `tests/test_skills/test_proposals_lib.py`:

```python
def test_write_proposal_persists_generation_and_activation_gates(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result={
            "required": True,
            "passed": True,
            "reason": "ok",
            "failures": [],
        },
        activation_result={
            "required": True,
            "passed": True,
            "reason": "ok",
            "true_positive_rate": 1.0,
            "false_positive_rate": 0.0,
        },
    )

    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["generation_quality"]["passed"] is True
    assert shown["gates"]["activation_eval"]["true_positive_rate"] == 1.0
```

Append this test too:

```python
def test_write_proposal_blocks_failed_activation_gate(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result={"required": True, "passed": True, "reason": "ok"},
        activation_result={
            "required": True,
            "passed": False,
            "reason": "activation_failed",
            "failures": ["false_positive_rate_above_threshold"],
        },
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["activation_eval"]["passed"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_proposals_lib.py::test_write_proposal_persists_generation_and_activation_gates tests/test_skills/test_proposals_lib.py::test_write_proposal_blocks_failed_activation_gate -q
```

Expected: FAIL because `write_proposal()` does not accept the new keyword args.

- [ ] **Step 3: Extend proposals library**

In `src/opensquilla/skills/proposals_lib.py`, add this helper near the other normalizers:

```python
def _normalise_gate_payload(value: object, *, required: bool, missing_reason: str) -> dict:
    if value is None or value == "":
        return {
            "required": required,
            "passed": not required,
            "reason": missing_reason if required else "not_required",
        }
    if isinstance(value, dict):
        payload = dict(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw": text}
        payload = dict(parsed) if isinstance(parsed, dict) else {"raw": text}
    else:
        payload = {"raw": str(value)}
    payload.setdefault("required", required)
    payload.setdefault("passed", bool(payload.get("passed", False)))
    payload.setdefault("reason", "ok" if payload.get("passed") else missing_reason)
    return payload
```

Extend `write_proposal()` signature:

```python
    generation_quality_result: object = None,
    activation_result: object = None,
```

Inside `write_proposal()`, compute:

```python
    mode = (creator_mode or "").strip().upper()
    creator_quality_required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL"}
    generation_quality_gate = _normalise_gate_payload(
        generation_quality_result,
        required=creator_quality_required,
        missing_reason="missing_generation_quality_result",
    )
    activation_gate = _normalise_gate_payload(
        activation_result,
        required=creator_quality_required,
        missing_reason="missing_activation_result",
    )
```

Include both in `eligible`:

```python
        and bool(generation_quality_gate.get("passed", False))
        and bool(activation_gate.get("passed", False))
```

Include both in `gates`:

```python
        "generation_quality": generation_quality_gate,
        "activation_eval": activation_gate,
```

- [ ] **Step 4: Extend CLI script args**

In `src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py`, extend `cmd_write_proposal` call:

```python
        generation_quality_result=args.generation_quality_result,
        activation_result=args.activation_result,
```

Add parser args:

```python
    p.add_argument("--generation-quality-result", default=None)
    p.add_argument("--activation-result", default=None)
```

- [ ] **Step 5: Run proposal tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_proposals_lib.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/skills/proposals_lib.py src/opensquilla/skills/bundled/skill-creator-proposals/scripts/proposals.py tests/test_skills/test_proposals_lib.py
git commit -m "Persist creator generation and activation gates" \
  -m "Constraint: missing new gates are required only for persisted or full-gated creator modes." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_proposals_lib.py -q"
```

### Task 7: Wire Gates into Meta-Skill Creator DAG

**Files:**
- Modify: `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`
- Modify: `src/opensquilla/skills/creator/proposer.py`
- Modify: `tests/test_skills/test_meta_skill_creator_e2e.py`

- [ ] **Step 1: Write failing DAG wiring test**

Append this test to `tests/test_skills/test_meta_skill_creator_e2e.py`:

```python
def test_creator_dag_runs_generation_quality_and_activation_before_persist(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    steps = {step.id: step for step in plan.steps}
    assert "generation_quality" in steps
    assert "activation_eval" in steps
    assert steps["generation_quality"].depends_on == ["fill_slots"]
    assert steps["activation_eval"].depends_on == ["assemble"]
    assert "generation_quality_result" in steps["persist"].tool_args
    assert "activation_result" in steps["persist"].tool_args
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist -q
```

Expected: FAIL because the DAG lacks `generation_quality` and `activation_eval`.

- [ ] **Step 3: Add persist function args**

In `src/opensquilla/skills/creator/proposer.py`, extend `meta_skill_persist_proposal()` signature:

```python
    generation_quality_result: str = "",
    activation_result: str = "",
```

Add CLI args:

```python
            "--generation-quality-result", generation_quality_result,
            "--activation-result", activation_result,
```

Extend the tool schema params:

```python
        "generation_quality_result": {"type": "string"},
        "activation_result": {"type": "string"},
```

Extend `meta_skill_persist_proposal_tool()` signature and `asyncio.to_thread()` call with the same two keyword arguments.

- [ ] **Step 4: Add DAG steps**

In `src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md`, insert after `assemble`:

```yaml
    - id: generation_quality
      label: "生成质量"
      label_en: "Generation quality"
      kind: tool_call
      depends_on: [fill_slots]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      tool: meta_skill_generation_quality_run
      tool_args:
        pattern_id: "{{ outputs.pick_pattern }}"
        slots_json: "{{ outputs.fill_slots }}"

    - id: activation_eval
      label: "触发评测"
      label_en: "Activation evaluation"
      kind: tool_call
      depends_on: [assemble]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      tool: meta_skill_activation_eval_run
      tool_args:
        skill_md: "{{ outputs.assemble }}"
        positive_prompts: ""
        negative_prompts: ""
        catalog_negative_prompts: ""
```

Change `collision_check.depends_on` from `[assemble]` to:

```yaml
      depends_on: [assemble, generation_quality, activation_eval]
```

In the preview task text, add:

```yaml
          Generation quality:
          {{ outputs.generation_quality | truncate(2000) }}

          Activation eval:
          {{ outputs.activation_eval | truncate(2000) }}
```

In the `persist.tool_args`, add:

```yaml
        generation_quality_result: "{{ outputs.generation_quality }}"
        activation_result: "{{ outputs.activation_eval }}"
```

- [ ] **Step 5: Run DAG wiring test**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/skills/creator/proposer.py src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md tests/test_skills/test_meta_skill_creator_e2e.py
git commit -m "Run creator quality and activation gates in meta-skill DAG" \
  -m "Constraint: new gates run before persistence and remain creator-internal tool calls." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: PYTHONPATH=src pytest tests/test_skills/test_meta_skill_creator_e2e.py::test_creator_dag_runs_generation_quality_and_activation_before_persist -q"
```

### Task 8: Verify Full P0 Slice

**Files:**
- No new files.
- Verify: changed files from Tasks 1-7.

- [ ] **Step 1: Run focused creator tests**

Run:

```bash
PYTHONPATH=src pytest \
  tests/test_skills/test_creator_proposer.py \
  tests/test_skills/test_creator_quality.py \
  tests/test_skills/test_creator_activation.py \
  tests/test_skills/test_proposals_lib.py \
  tests/test_skills/test_meta_skill_creator_e2e.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run meta-skill lint tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_skills/test_meta_skill_creator_lint.py tests/test_skills/test_meta_skill_linter.py -q
```

Expected: PASS.

- [ ] **Step 3: Run static diff checks**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Inspect proposal gate shape manually**

Run:

```bash
PYTHONPATH=src python - <<'PY'
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from opensquilla.skills import proposals_lib

skill_md = '''---
name: synth-plan-check
description: "Synthetic plan check."
kind: meta
meta_priority: 50
triggers:
  - "plan check"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
'''
with TemporaryDirectory() as tmp:
    home = Path(tmp) / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result={"required": True, "passed": True, "reason": "ok"},
        activation_result={"required": True, "passed": True, "reason": "ok"},
    )
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    gates = shown["gates"]
    print(json.dumps({
        "status": shown["status"],
        "generation_quality": gates["generation_quality"]["passed"],
        "activation_eval": gates["activation_eval"]["passed"],
    }, sort_keys=True))
PY
```

Expected:

```text
{"activation_eval": true, "generation_quality": true, "status": "ok"}
```

- [ ] **Step 5: Commit verification notes if code changed during verification**

If no files changed, do not commit. If a verification fix was needed:

```bash
git add <fixed-files>
git commit -m "Stabilize meta-skill creator P0 verification" \
  -m "Constraint: fix was discovered by focused P0 verification." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: focused creator tests; meta-skill lint tests; git diff --check"
```

---

## Self-Review Checklist

- Spec coverage:
  - Generation rationale: Task 1, Task 2, Task 3.
  - Generation-quality gates: Task 3, Task 5, Task 7.
  - Activation measurement: Task 4, Task 5, Task 7.
  - Proposal gate persistence: Task 6.
  - Meta-skill DAG wiring: Task 7.
  - P0-only scope: plan excludes P1 author-entry, bundles, memory feedback, dashboards, learned routers, and rollback.
- Placeholder scan:
  - No unresolved markers or deferred implementation steps are used.
- Type consistency:
  - `generation_rationale` lives in slot schemas.
  - `generation_quality` gate key is persisted as `gates["generation_quality"]`.
  - `activation_eval` gate key is persisted as `gates["activation_eval"]`.
  - Internal tool names are `meta_skill_generation_quality_run` and `meta_skill_activation_eval_run`.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-08-meta-skill-creator-p0-generation-activation-plan.md`. Two execution options:

1. Subagent-Driven (recommended) - Dispatch a fresh subagent per task, review between tasks, fast iteration.

2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints.
