"""Authoring helpers derived from persisted meta-skill runs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from opensquilla.persistence.meta_run_writer import RunRecord, summarize_run_record
from opensquilla.skills.meta.plan_serde import from_jsonable
from opensquilla.skills.meta.types import MetaPlan

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def draft_meta_skill_seed(
    record: RunRecord,
    *,
    existing_specs: Iterable[Any] = (),
) -> dict[str, Any]:
    """Build a lightweight meta-skill draft seed from one historical run.

    This is intentionally not a full SKILL.md generator. It returns a stable
    JSON payload for CLI/WebUI authoring surfaces: trigger candidates,
    description, composition skeleton, inherited contracts, and conflict hints.
    """
    inputs = _json_obj(record.inputs_json)
    plan = _plan_from_record(record)
    user_message = str(inputs.get("user_message") or inputs.get("message") or "").strip()
    goal = _seed_goal(record, plan, user_message)
    trigger_candidates = _trigger_candidates(record.meta_skill_name, user_message)
    request_template = dict(plan.request_template) if plan else {}
    output_contract = dict(plan.output_contract) if plan else {}
    normalized = _normalized_seed(
        record,
        plan=plan,
        goal=goal,
        candidate_triggers=trigger_candidates,
        request_template=request_template,
        output_contract=output_contract,
    )
    creator_input = {
        "user_message": user_message,
        "draft_seed_json": json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "recommended_mode": "PERSISTED_PROPOSAL",
    }
    return {
        "status": "ok",
        **normalized,
        "creator_input": creator_input,
        "source_run": {
            "run_id": record.run_id,
            "meta_skill_name": record.meta_skill_name,
            "status": record.status,
        },
        "name": f"{_slug(_draft_base_name(record.meta_skill_name))}-draft",
        "description": _draft_description(record, user_message),
        "trigger_candidates": trigger_candidates,
        "trigger_conflicts": detect_trigger_conflicts(
            trigger_candidates,
            existing_specs=existing_specs,
        ),
        "request_template": request_template,
        "output_contract": output_contract,
        "eval_prompts": _seed_eval_prompts(plan, user_message),
        "composition": {
            "steps": _seed_steps(plan, record),
        },
        "run_summary": summarize_run_record(record),
    }


def detect_trigger_conflicts(
    trigger_candidates: Iterable[str],
    *,
    existing_specs: Iterable[Any],
) -> list[dict[str, Any]]:
    """Return exact trigger collisions against loaded SkillSpec-like objects."""
    wanted = {str(t).strip().lower() for t in trigger_candidates if str(t).strip()}
    conflicts: list[dict[str, Any]] = []
    for spec in existing_specs:
        for trigger in getattr(spec, "triggers", []) or []:
            key = str(trigger).strip().lower()
            if key in wanted:
                conflicts.append({
                    "trigger": str(trigger),
                    "skill": str(getattr(spec, "name", "")),
                })
    return conflicts


def _plan_from_record(record: RunRecord) -> MetaPlan | None:
    try:
        payload = json.loads(record.plan_snapshot_json or "{}")
        return from_jsonable(payload)
    except Exception:  # noqa: BLE001 - authoring must fail open
        return None


def _normalized_seed(
    record: RunRecord,
    *,
    plan: MetaPlan | None,
    goal: str,
    candidate_triggers: list[str],
    request_template: dict[str, Any],
    output_contract: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_kind": "meta_run",
        "goal": goal,
        "observed_steps": _observed_steps(record, plan),
        "candidate_triggers": candidate_triggers,
        "inputs": _seed_inputs(request_template),
        "outputs": _seed_outputs(output_contract, plan),
        "constraints": _seed_constraints(request_template, output_contract),
        "negative_cases": _seed_negative_cases(goal),
        "evidence_refs": [record.run_id],
    }


def _seed_goal(record: RunRecord, plan: MetaPlan | None, user_message: str) -> str:
    if user_message:
        return user_message
    if plan and plan.request_template.get("outcome"):
        return str(plan.request_template["outcome"]).strip()
    if record.final_text:
        return record.final_text.strip()
    return _draft_base_name(record.meta_skill_name).replace("-", " ").strip()


def _json_obj(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _observed_steps(record: RunRecord, plan: MetaPlan | None) -> list[str]:
    observed = [
        step.effective_skill or step.declared_skill
        for step in record.steps
        if step.effective_skill or step.declared_skill
    ]
    if observed:
        return observed
    if plan is None:
        return []
    return [step.skill for step in plan.steps if step.skill]


def _seed_inputs(request_template: dict[str, Any]) -> list[str]:
    fields = request_template.get("fields", [])
    inputs: list[str] = []
    if isinstance(fields, list):
        for field in fields:
            name = field.get("name") if isinstance(field, dict) else field
            if name:
                inputs.append(str(name))
    return _dedupe(inputs)


def _seed_outputs(output_contract: dict[str, Any], plan: MetaPlan | None) -> list[str]:
    outputs = _string_list(output_contract.get("required_sections"))
    if outputs:
        return _dedupe(outputs)
    if plan is None:
        return []
    from_eval: list[str] = []
    for prompt in plan.eval_prompts:
        from_eval.extend(_string_list(prompt.get("rubric")))
    return _dedupe(from_eval)


def _seed_constraints(
    request_template: dict[str, Any],
    output_contract: dict[str, Any],
) -> list[str]:
    constraints: list[str] = []
    constraints.extend(_string_list(request_template.get("constraints")))
    constraints.extend(_string_list(output_contract.get("constraints")))
    return _dedupe(constraints)


def _seed_negative_cases(goal: str) -> list[str]:
    if goal:
        return [f"Requests unrelated to this goal: {goal[:80]}"]
    return ["Requests unrelated to the persisted meta-skill run."]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [str(key) for key in value if str(key).strip()]
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _trigger_candidates(meta_skill_name: str, user_message: str) -> list[str]:
    candidates: list[str] = []
    if user_message:
        candidates.append(user_message[:120])
    readable_name = meta_skill_name.replace("meta-", "").replace("-", " ").strip()
    if readable_name:
        candidates.append(readable_name)
    seen: set[str] = set()
    out: list[str] = []
    for item in candidates:
        key = item.lower()
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _draft_description(record: RunRecord, user_message: str) -> str:
    if user_message:
        return (
            f"Draft meta-skill based on run {record.run_id}: "
            f"{user_message[:100]}"
        )
    return f"Draft meta-skill based on run {record.run_id}."


def _seed_steps(plan: MetaPlan | None, record: RunRecord) -> list[dict[str, Any]]:
    status_by_step = {step.step_id: step.status for step in record.steps}
    if plan is None:
        return [
            {
                "id": step.step_id,
                "kind": step.step_kind,
                "skill": step.declared_skill,
                "status": step.status,
            }
            for step in record.steps
        ]
    steps: list[dict[str, Any]] = []
    for step in plan.steps:
        item = {
            "id": step.id,
            "label": step.label,
            "kind": step.kind,
            "skill": step.skill,
            "depends_on": list(step.depends_on),
            "status": status_by_step.get(step.id, "not_run"),
        }
        if step.on_failure:
            item["on_failure"] = step.on_failure
        steps.append(item)
    return steps


def _seed_eval_prompts(plan: MetaPlan | None, user_message: str) -> list[dict[str, Any]]:
    if plan and plan.eval_prompts:
        return [dict(item) for item in plan.eval_prompts]
    if not user_message:
        return []
    rubric = []
    if plan and plan.output_contract:
        rubric = list(plan.output_contract.get("required_sections", []) or [])
    return [{
        "name": "source-run-request",
        "prompt": user_message,
        "rubric": rubric,
    }]


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "meta-skill"


def _draft_base_name(meta_skill_name: str) -> str:
    if meta_skill_name.startswith("meta-"):
        return meta_skill_name.removeprefix("meta-")
    return meta_skill_name


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        stripped = item.strip()
        key = stripped.lower()
        if stripped and key not in seen:
            out.append(stripped)
            seen.add(key)
    return out
