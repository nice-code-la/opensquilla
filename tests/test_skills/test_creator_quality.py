from __future__ import annotations

import json
from typing import Any

from opensquilla.skills.creator.quality import evaluate_generation_quality


def _valid_slots() -> dict[str, Any]:
    return {
        "name": "synth-quality-pipeline",
        "description": "Synthetic quality pipeline that processes then saves output.",
        "meta_priority": 50,
        "triggers": ["quality pipeline"],
        "steps": [
            {
                "id": "process",
                "skill": "summarize",
                "task": "Process the source",
                "with_keys": {},
            },
            {
                "id": "save",
                "skill": "memory",
                "task": "Save the result",
                "with_keys": {},
            },
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
            "rejected_alternatives": [
                "bundle: later step depends on earlier output",
            ],
            "output_contract_summary": (
                "Final answer reports processed output and save status."
            ),
        },
    }


def test_generation_quality_passes_valid_slots() -> None:
    result = evaluate_generation_quality("p1_sequential", json.dumps(_valid_slots()))

    json.dumps(result)
    assert result["required"] is True
    assert result["passed"] is True
    assert result["reason"] == "ok"
    assert result["issues"] == []
    assert result["failures"] == []
    assert result["summary"]["selected_shape"] == "metaskill"


def test_generation_quality_fails_missing_rationale() -> None:
    slots = _valid_slots()
    slots.pop("generation_rationale")

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "missing_generation_rationale" in result["issues"]


def test_generation_quality_fails_empty_rationale() -> None:
    slots = _valid_slots()
    slots["generation_rationale"] = {}

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "missing_generation_rationale" in result["issues"]


def test_generation_quality_fails_creator_internal_skill() -> None:
    slots = _valid_slots()
    slots["steps"][0]["skill"] = "skill-creator-proposals"

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "creator_internal_skill:skill-creator-proposals" in result["issues"]


def test_generation_quality_fails_creator_internal_generated_content() -> None:
    slots = _valid_slots()
    slots["generated_skill_content"] = (
        "composition:\n"
        "  steps:\n"
        "    - skill: meta-skill-creator\n"
    )

    result = evaluate_generation_quality("p1_sequential", json.dumps(slots))

    assert result["passed"] is False
    assert "creator_internal_skill:meta-skill-creator" in result["issues"]


def test_generation_quality_unknown_pattern_returns_structured_failure() -> None:
    result = evaluate_generation_quality("p0_missing", json.dumps(_valid_slots()))

    assert result["required"] is True
    assert result["passed"] is False
    assert result["reason"] == "unknown_pattern"
    assert "unknown_pattern:p0_missing" in result["issues"]


def test_generation_quality_invalid_json_returns_structured_failure() -> None:
    result = evaluate_generation_quality("p1_sequential", "{not valid")

    assert result["required"] is True
    assert result["passed"] is False
    assert result["reason"] == "invalid_slots"
    assert "invalid_json" in result["issues"]
    assert isinstance(result["diagnostics"], str)


def test_generation_quality_schema_failure_returns_structured_failure() -> None:
    result = evaluate_generation_quality("p1_sequential", json.dumps({"name": "x"}))

    assert result["required"] is True
    assert result["passed"] is False
    assert result["reason"] == "invalid_slots"
    assert "schema_validation_failed" in result["issues"]
    assert isinstance(result["diagnostics"], str)
