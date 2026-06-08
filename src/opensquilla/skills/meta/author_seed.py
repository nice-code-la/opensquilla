"""Authoring helpers derived from persisted meta-skill runs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from opensquilla.persistence.meta_run_writer import RunRecord, summarize_run_record
from opensquilla.skills.meta.plan_serde import from_jsonable
from opensquilla.skills.meta.types import MetaPlan

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SECRET_LITERAL_RE = re.compile(
    r"(?i)\b(?:sk|pk|ghp|gho|ghu|ghs|ghr|xoxb|xoxp)[_-][A-Za-z0-9_\-]{8,}\b"
)
_FILE_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._\- ]+){2,}\.[A-Za-z0-9]{1,8}")
_DUPLICATE_THRESHOLD = 0.75
_SUCCESSFUL_STEP_STATUSES = {"ok", "substituted"}
_INCOHERENT_STEP_STATUSES = {"failed", "running"}


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
    existing_specs = list(existing_specs)
    inputs = _json_obj(record.inputs_json)
    plan = _plan_from_record(record)
    raw_user_message = str(inputs.get("user_message") or inputs.get("message") or "").strip()
    user_message, privacy_warnings = _scrub_text(raw_user_message)
    refusal_reason = _can_draft(record, user_message)
    if refusal_reason:
        return _cannot_draft(record, refusal_reason)
    trigger_candidates = _trigger_candidates(record.meta_skill_name, user_message)
    request_template, request_warnings = _scrub_value(
        dict(plan.request_template) if plan else {}
    )
    output_contract, output_warnings = _scrub_value(
        dict(plan.output_contract) if plan else {}
    )
    eval_prompts, eval_warnings = _scrub_value(_seed_eval_prompts(plan, user_message))
    request_template = request_template if isinstance(request_template, dict) else {}
    output_contract = output_contract if isinstance(output_contract, dict) else {}
    eval_prompts = eval_prompts if isinstance(eval_prompts, list) else []
    privacy_warnings = _merge_warnings(
        privacy_warnings,
        request_warnings,
        output_warnings,
        eval_warnings,
    )
    goal, goal_warnings = _seed_goal(record, request_template, user_message)
    privacy_warnings = _merge_warnings(privacy_warnings, goal_warnings)
    duplicate_detection = _detect_duplicate(
        goal,
        trigger_candidates,
        existing_specs=existing_specs,
    )
    normalized = _normalized_seed(
        record,
        plan=plan,
        goal=goal,
        candidate_triggers=trigger_candidates,
        request_template=request_template,
        output_contract=output_contract,
        eval_prompts=eval_prompts,
    )
    normalized["duplicate_detection"] = duplicate_detection
    creator_input = {
        "user_message": user_message or goal,
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
        **({"privacy_warnings": privacy_warnings} if privacy_warnings else {}),
        "creator_input": creator_input,
        "source_run": _source_run(record),
        "name": f"{_slug(record.meta_skill_name)}-draft",
        "description": _draft_description(record, user_message),
        "trigger_candidates": trigger_candidates,
        "trigger_conflicts": detect_trigger_conflicts(
            trigger_candidates,
            existing_specs=existing_specs,
        ),
        "request_template": request_template,
        "output_contract": output_contract,
        "eval_prompts": eval_prompts,
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


def _detect_duplicate(
    goal: str,
    trigger_candidates: Iterable[str],
    *,
    existing_specs: Iterable[Any],
) -> dict[str, Any]:
    source_parts = [goal, *trigger_candidates]
    source_tokens = _tokens_from_parts(source_parts)
    best_name: str | None = None
    best_score = 0.0
    for spec in sorted(existing_specs, key=_spec_sort_key):
        spec_parts = [
            str(getattr(spec, "name", "")),
            str(getattr(spec, "description", "")),
            *[str(trigger) for trigger in getattr(spec, "triggers", []) or []],
        ]
        score = max(
            _jaccard(source_tokens, _tokens_from_parts(spec_parts)),
            _exact_phrase_score(source_parts, spec_parts),
        )
        if score > best_score:
            best_score = score
            best_name = str(getattr(spec, "name", "")) or None
    rounded_score = round(best_score, 3)
    if best_name and best_score >= _DUPLICATE_THRESHOLD:
        return {
            "suggested_action": "patch_existing",
            "target": best_name,
            "score": rounded_score,
        }
    return {
        "suggested_action": "create_new",
        "target": None,
        "score": rounded_score,
    }


def _tokens_from_parts(parts: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        for token in _TOKEN_RE.findall(part.casefold()):
            tokens.add(token)
            if any(ord(char) > 127 for char in token):
                tokens.update(_char_ngrams(token))
    return tokens


def _char_ngrams(value: str, *, size: int = 2) -> set[str]:
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _spec_sort_key(spec: Any) -> str:
    return str(getattr(spec, "name", "")).casefold()


def _exact_phrase_score(left_parts: Iterable[str], right_parts: Iterable[str]) -> float:
    left = [_normalize_phrase(part) for part in left_parts]
    right = [_normalize_phrase(part) for part in right_parts]
    for left_part in left:
        if not _strong_phrase(left_part):
            continue
        for right_part in right:
            if _strong_phrase(right_part) and (
                left_part in right_part or right_part in left_part
            ):
                return 1.0
    return 0.0


def _normalize_phrase(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _strong_phrase(value: str) -> bool:
    if any(ord(char) > 127 for char in value):
        return len(value.replace(" ", "")) >= 4
    return len(value.split()) >= 2 and len(value) >= 8


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _plan_from_record(record: RunRecord) -> MetaPlan | None:
    try:
        payload = json.loads(record.plan_snapshot_json or "{}")
        return from_jsonable(payload)
    except Exception:  # noqa: BLE001 - authoring must fail open
        return None


def _can_draft(record: RunRecord, scrubbed_user_message: str) -> str | None:
    if record.status != "ok":
        return "run_not_successful"
    if not scrubbed_user_message and not (record.final_text or "").strip():
        return "missing_goal_or_output"
    if not record.steps:
        return "missing_observed_steps"
    if not any(step.status in _SUCCESSFUL_STEP_STATUSES for step in record.steps):
        return "missing_successful_observed_step"
    if any(step.status in _INCOHERENT_STEP_STATUSES for step in record.steps):
        return "incoherent_observed_steps"
    return None


def _cannot_draft(record: RunRecord, reason: str) -> dict[str, Any]:
    return {
        "status": "cannot_draft",
        "reason": reason,
        "source_kind": "meta_run",
        "source_run": _source_run(record),
    }


def _source_run(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "meta_skill_name": record.meta_skill_name,
        "status": record.status,
    }


def _scrub_text(value: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    scrubbed = _SECRET_LITERAL_RE.sub("[secret]", value)
    if scrubbed != value:
        warnings.append("secret_like_text_redacted")
    with_files = _FILE_PATH_RE.sub("[file]", scrubbed)
    if with_files != scrubbed:
        warnings.append("file_path_redacted")
    return with_files.strip(), warnings


def _scrub_value(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, Mapping):
        warnings: list[str] = []
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            scrubbed_key, key_warnings = _scrub_value(key)
            scrubbed_item, item_warnings = _scrub_value(item)
            scrubbed[scrubbed_key] = scrubbed_item
            warnings = _merge_warnings(warnings, key_warnings, item_warnings)
        return scrubbed, warnings
    if isinstance(value, list):
        warnings = []
        scrubbed_items = []
        for item in value:
            scrubbed_item, item_warnings = _scrub_value(item)
            scrubbed_items.append(scrubbed_item)
            warnings = _merge_warnings(warnings, item_warnings)
        return scrubbed_items, warnings
    if isinstance(value, tuple):
        warnings = []
        scrubbed_items = []
        for item in value:
            scrubbed_item, item_warnings = _scrub_value(item)
            scrubbed_items.append(scrubbed_item)
            warnings = _merge_warnings(warnings, item_warnings)
        return tuple(scrubbed_items), warnings
    return value, []


def _merge_warnings(*groups: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for warning in group:
            if warning not in seen:
                merged.append(warning)
                seen.add(warning)
    return merged


def _normalized_seed(
    record: RunRecord,
    *,
    plan: MetaPlan | None,
    goal: str,
    candidate_triggers: list[str],
    request_template: dict[str, Any],
    output_contract: dict[str, Any],
    eval_prompts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source_kind": "meta_run",
        "goal": goal,
        "observed_steps": _observed_steps(record, plan),
        "candidate_triggers": candidate_triggers,
        "inputs": _seed_inputs(request_template),
        "outputs": _seed_outputs(output_contract, eval_prompts),
        "constraints": _seed_constraints(request_template, output_contract),
        "negative_cases": _seed_negative_cases(goal),
        "evidence_refs": [record.run_id],
    }


def _seed_goal(
    record: RunRecord,
    request_template: dict[str, Any],
    user_message: str,
) -> tuple[str, list[str]]:
    if user_message:
        return user_message, []
    if request_template.get("outcome"):
        return str(request_template["outcome"]).strip(), []
    if record.final_text:
        return _scrub_text(record.final_text)
    return _draft_base_name(record.meta_skill_name).replace("-", " ").strip(), []


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


def _seed_outputs(
    output_contract: dict[str, Any],
    eval_prompts: list[dict[str, Any]],
) -> list[str]:
    outputs = _string_list(output_contract.get("required_sections"))
    if outputs:
        return _dedupe(outputs)
    from_eval: list[str] = []
    for prompt in eval_prompts:
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
