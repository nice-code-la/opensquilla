"""Deterministic activation gates for meta-skill creator candidates."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import yaml

from opensquilla.engine.steps.meta_resolution import _trigger_matches

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---(?:\n|\Z)", re.DOTALL)


def evaluate_candidate_activation(
    skill_markdown: str,
    positive_prompts: Sequence[str],
    negative_prompts: Sequence[str],
    *,
    threshold: float = 0.8,
) -> dict[str, Any]:
    """Evaluate deterministic trigger behavior for a candidate SKILL.md."""
    metadata, metadata_issues = _parse_skill_metadata(skill_markdown)
    if metadata_issues:
        return _result(
            passed=False,
            reason="invalid_skill_metadata",
            true_positive_rate=0.0,
            false_positive_count=0,
            cases=[],
            issues=metadata_issues,
            metadata=metadata,
        )

    prompts_positive = _normalise_prompts(positive_prompts)
    prompts_negative = _normalise_prompts(negative_prompts)
    cases: list[dict[str, Any]] = []
    issues: list[str] = []

    if not prompts_positive:
        issues.append("missing_positive_prompts")

    for prompt in prompts_positive:
        cases.append(_evaluate_case("positive", prompt, metadata["triggers"]))
    for prompt in prompts_negative:
        case = _evaluate_case("negative", prompt, metadata["triggers"])
        cases.append(case)
        if case["matched"]:
            issues.append(f"false_positive:{prompt}")

    positive_cases = [case for case in cases if case["kind"] == "positive"]
    positive_hits = sum(1 for case in positive_cases if case["matched"])
    true_positive_rate = (
        positive_hits / len(positive_cases) if positive_cases else 0.0
    )
    false_positive_count = sum(
        1 for case in cases if case["kind"] == "negative" and case["matched"]
    )

    if true_positive_rate < threshold:
        issues.append(
            "true_positive_rate_below_threshold:"
            f"{true_positive_rate:.2f}<{threshold:.2f}"
        )

    passed = not issues
    return _result(
        passed=passed,
        reason="ok" if passed else "activation_failed",
        true_positive_rate=true_positive_rate,
        false_positive_count=false_positive_count,
        cases=cases,
        issues=issues,
        metadata=metadata,
    )


def _parse_skill_metadata(skill_markdown: str) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    match = _FRONTMATTER_RE.search(skill_markdown)
    if match is None:
        return {"name": "", "description": "", "triggers": []}, [
            "missing_frontmatter",
        ]

    try:
        raw = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        return {"name": "", "description": "", "triggers": []}, [
            f"invalid_frontmatter_yaml:{str(exc)[:200]}",
        ]

    if not isinstance(raw, dict):
        return {"name": "", "description": "", "triggers": []}, [
            "frontmatter_not_mapping",
        ]

    name = raw.get("name")
    description = raw.get("description")
    triggers_raw = raw.get("triggers")
    triggers = _normalise_triggers(triggers_raw)

    if not isinstance(name, str) or not name.strip():
        issues.append("missing_name")
    if not isinstance(description, str) or not description.strip():
        issues.append("missing_description")
    if not triggers:
        issues.append("missing_triggers")

    return {
        "name": name.strip() if isinstance(name, str) else "",
        "description": description.strip() if isinstance(description, str) else "",
        "triggers": triggers,
    }, issues


def _normalise_triggers(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _normalise_prompts(prompts: Sequence[str]) -> list[str]:
    return [str(prompt).strip() for prompt in prompts if str(prompt).strip()]


def _evaluate_case(
    kind: str,
    prompt: str,
    triggers: Sequence[str],
) -> dict[str, Any]:
    prompt_lower = prompt.lower()
    matched_trigger = next(
        (
            trigger
            for trigger in triggers
            if _trigger_matches(trigger, prompt_lower)
        ),
        "",
    )
    return {
        "kind": kind,
        "prompt": prompt,
        "matched": bool(matched_trigger),
        "matched_trigger": matched_trigger,
    }


def _result(
    *,
    passed: bool,
    reason: str,
    true_positive_rate: float,
    false_positive_count: int,
    cases: list[dict[str, Any]],
    issues: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "required": True,
        "passed": passed,
        "reason": reason,
        "true_positive_rate": true_positive_rate,
        "false_positive_count": false_positive_count,
        "cases": cases,
        "issues": issues,
        "metadata": metadata,
    }


__all__ = ["evaluate_candidate_activation"]
