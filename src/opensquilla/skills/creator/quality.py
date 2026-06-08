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

_GENERATED_CONTENT_FIELDS = {
    "generated_skill_content",
    "generated_skill_md",
    "rendered_skill_md",
    "skill_content",
    "skill_markdown",
    "skill_md",
}


def evaluate_generation_quality(pattern_id: str, slots_json: str) -> dict[str, Any]:
    """Validate slot-level generation quality before SKILL.md assembly gates."""
    if pattern_id not in PATTERN_SLOT_SCHEMA:
        return _failure(
            reason="unknown_pattern",
            issues=[f"unknown_pattern:{pattern_id}"],
            summary={},
        )

    try:
        raw = json.loads(slots_json)
    except json.JSONDecodeError as exc:
        return _failure(
            reason="invalid_slots",
            issues=["invalid_json"],
            diagnostics=str(exc)[:1000],
            summary={},
        )

    if not isinstance(raw, dict):
        return _failure(
            reason="invalid_slots",
            issues=["schema_validation_failed"],
            diagnostics="slots_json must decode to a JSON object",
            summary={},
        )

    schema = PATTERN_SLOT_SCHEMA[pattern_id]
    validation_raw = dict(raw)
    raw_rationale = validation_raw.get("generation_rationale")
    missing_rationale = not isinstance(raw_rationale, dict) or not raw_rationale
    if missing_rationale:
        validation_raw.pop("generation_rationale", None)

    try:
        slots = schema.model_validate(validation_raw)
    except ValidationError as exc:
        return _failure(
            reason="invalid_slots",
            issues=["schema_validation_failed"],
            diagnostics=str(exc)[:1000],
            summary={},
        )

    data = slots.model_dump()
    issues: list[str] = []
    rationale = data.get("generation_rationale")
    if missing_rationale or not isinstance(rationale, dict) or not rationale:
        issues.append("missing_generation_rationale")
        rationale = {}

    if rationale.get("selected_shape") != "metaskill":
        issues.append(f"wrong_shape:{rationale.get('selected_shape') or 'missing'}")
    if rationale.get("selected_pattern") != pattern_id:
        issues.append(f"wrong_pattern:{rationale.get('selected_pattern') or 'missing'}")
    if not rationale.get("rejected_alternatives"):
        issues.append("missing_rejected_alternatives")
    if not rationale.get("output_contract_summary"):
        issues.append("missing_output_contract_summary")

    for skill in _referenced_creator_internal_skills(data, raw):
        issues.append(f"creator_internal_skill:{skill}")

    passed = not issues
    return {
        "required": True,
        "passed": passed,
        "reason": "ok" if passed else "generation_quality_failed",
        "issues": issues,
        "failures": issues,
        "summary": {
            "name": data.get("name", ""),
            "selected_shape": rationale.get("selected_shape", ""),
            "selected_pattern": rationale.get("selected_pattern", ""),
            "rejected_alternatives": rationale.get("rejected_alternatives", []),
            "unresolved_assumptions": rationale.get("unresolved_assumptions", []),
        },
    }


def _failure(
    *,
    reason: str,
    issues: list[str],
    summary: dict[str, Any],
    diagnostics: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "required": True,
        "passed": False,
        "reason": reason,
        "issues": issues,
        "failures": issues,
        "summary": summary,
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


def _referenced_creator_internal_skills(
    data: dict[str, Any],
    raw: dict[str, Any],
) -> list[str]:
    referenced = {
        skill
        for skill in _referenced_step_skills(data)
        if skill in _CREATOR_INTERNAL_SKILLS
    }
    for text in _generated_skill_content(raw):
        referenced.update(
            skill for skill in _CREATOR_INTERNAL_SKILLS if skill in text
        )
    return sorted(referenced)


def _referenced_step_skills(data: dict[str, Any]) -> list[str]:
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


def _generated_skill_content(raw: dict[str, Any]) -> list[str]:
    content: list[str] = []
    for key in _GENERATED_CONTENT_FIELDS:
        value = raw.get(key)
        if isinstance(value, str):
            content.append(value)
    return content
