"""Library functions for the ``~/.opensquilla/proposals/`` directory.

Lifted out of ``skills/bundled/skill-creator-proposals/scripts/proposals.py``
so the gateway RPC layer (Path 3) can call them in-process — the
bundled script's hyphenated path is not importable.

The bundled script now delegates here so there's one source of truth.

Path layout::

    ~/.opensquilla/proposals/<8-hex>/SKILL.md
    ~/.opensquilla/proposals/<8-hex>/gates.json
    ~/.opensquilla/skills/<name>/                # MANAGED layer after accept
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

PROPOSAL_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
SKILL_NAME_PATTERN = re.compile(r"[\w\-]+")
RISK_LEVELS = frozenset({"low", "medium", "high"})
_NO_REQUIRED_IMPROVEMENTS = frozenset({"", "none", "no", "n/a", "not applicable"})
_CREATOR_QUALITY_REQUIRED_MODES = frozenset({
    "FULL_GATED",
    "PERSISTED_PROPOSAL",
    "PATCH_PROPOSAL",
})
_LINT_SCRIPT = (
    Path(__file__).resolve().parent
    / "bundled"
    / "skill-creator-linter"
    / "scripts"
    / "lint.py"
)
_PATCH_ALLOWED_OPERATIONS = frozenset({
    "set_description",
    "add_triggers",
    "remove_triggers",
    "merge_metadata_opensquilla",
    "merge_output_contract",
    "append_eval_prompts",
    "append_body",
    "owner",
})
_PATCH_APPLIED_OPERATION_ORDER = (
    "set_description",
    "add_triggers",
    "remove_triggers",
    "merge_metadata_opensquilla",
    "merge_output_contract",
    "append_eval_prompts",
    "append_body",
)
_PATCH_STALE_GATES = (
    "smoke",
    "collision_check",
    "risk_classify",
    "acceptance_compare",
    "generation_quality",
    "activation_eval",
    "runtime_e2e",
)


def proposals_dir(home: Path) -> Path:
    return home / "proposals"


def skills_dir(home: Path) -> Path:
    return home / "skills"


def benchmarks_dir(home: Path) -> Path:
    return home / "proposal-benchmarks"


def rollback_dir(home: Path) -> Path:
    return home / "rollback"


def is_valid_proposal_id(proposal_id: str | None) -> bool:
    if not proposal_id:
        return False
    return bool(PROPOSAL_ID_PATTERN.fullmatch(proposal_id))


def atomic_write_proposal(
    home: Path, skill_md: str, gates: dict,
) -> str:
    """Materialise a proposal directory atomically.

    Writes ``SKILL.md`` + ``gates.json`` under ``$home/.tmp/proposal-<id>``
    then renames into ``$home/proposals/<id>`` — readers never see a
    half-built dir. Returns the new 8-hex proposal_id.
    """
    proposals = proposals_dir(home)
    proposals.mkdir(parents=True, exist_ok=True)
    proposal_id = uuid.uuid4().hex[:8]

    tmp_parent = home / ".tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_parent / f"proposal-{proposal_id}"
    tmp_dir.mkdir()
    (tmp_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (tmp_dir / "gates.json").write_text(
        json.dumps(gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    final_dir = proposals / proposal_id
    tmp_dir.rename(final_dir)
    return proposal_id


def atomic_write_benchmark(home: Path, report: dict) -> str:
    """Materialise a proposal benchmark report atomically."""
    benchmarks = benchmarks_dir(home)
    benchmarks.mkdir(parents=True, exist_ok=True)
    benchmark_id = uuid.uuid4().hex[:8]

    tmp_parent = home / ".tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_parent / f"proposal-benchmark-{benchmark_id}"
    tmp_dir.mkdir()
    (tmp_dir / "benchmark.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    final_dir = benchmarks / benchmark_id
    tmp_dir.rename(final_dir)
    return benchmark_id


def _normalise_acceptance_result(acceptance_result: object) -> dict:
    if acceptance_result is None:
        return {}
    if isinstance(acceptance_result, dict):
        return dict(acceptance_result)
    if isinstance(acceptance_result, str):
        text = acceptance_result.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": text}
    return {"raw": str(acceptance_result)}


def _first_section_item(raw: str, section: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(section)}:\s*(.*?)(?=^[A-Z][A-Z _-]*:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(raw)
    if not match:
        return ""
    body = match.group(1).strip()
    if not body:
        return ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    return first[1:].strip() if first.startswith("-") else first


def _evaluate_acceptance_compare(
    creator_mode: str,
    acceptance_result: object,
) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode == "FULL_GATED"
    payload = _normalise_acceptance_result(acceptance_result)
    raw = str(payload.get("raw") or "").strip()
    winner = str(payload.get("winner") or "").strip().lower()
    quality_score_raw = payload.get("quality_score")
    required_improvements = str(
        payload.get("required_improvements") or payload.get("required_improvement") or ""
    ).strip()

    if raw:
        if not winner:
            match = re.search(r"^WINNER:\s*([^\n]+)", raw, re.MULTILINE | re.IGNORECASE)
            if match:
                winner = match.group(1).strip().lower()
        if not required_improvements:
            required_improvements = _first_section_item(raw, "REQUIRED_IMPROVEMENTS")
        if quality_score_raw is None:
            score_match = re.search(
                r"^QUALITY_SCORE:\s*([0-9]+(?:\.[0-9]+)?)",
                raw,
                re.MULTILINE | re.IGNORECASE,
            )
            if score_match:
                quality_score_raw = score_match.group(1)

    required_improvements_norm = required_improvements.strip().lower()
    has_required_improvements = required_improvements_norm not in _NO_REQUIRED_IMPROVEMENTS
    quality_score: float | None = None
    if quality_score_raw not in (None, ""):
        try:
            quality_score = float(str(quality_score_raw))
        except (TypeError, ValueError):
            quality_score = None
    quality_passed = quality_score is None or quality_score >= 0.80
    passed = (
        not required
        or (
            winner in {"orchestrated", "tie"}
            and not has_required_improvements
            and quality_passed
        )
    )
    diagnostics: list[str] = []
    if required and not winner:
        diagnostics.append("missing WINNER in acceptance comparison")
    if required and winner not in {"orchestrated", "tie"}:
        diagnostics.append(f"winner is not orchestrated/tie: {winner or 'missing'}")
    if required and has_required_improvements:
        diagnostics.append("required improvements are present")
    if required and not quality_passed:
        diagnostics.append("quality score below 0.80")

    return {
        "required": required,
        "passed": passed,
        "winner": winner,
        "quality_score": quality_score,
        "required_improvements": required_improvements,
        "diagnostics": diagnostics,
        "raw": raw,
    }


def _evaluate_collision_check(creator_mode: str, collision_result: object) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL"}
    raw = str(collision_result or "").strip()
    lowered = raw.lower()
    failed = "revise_needed" in lowered or "fail" in lowered
    return {
        "required": required,
        "passed": (not required) or (bool(raw) and not failed),
        "reason": "ok" if ((not required) or (bool(raw) and not failed)) else (
            "collision_check_failed" if raw else "missing_collision_check"
        ),
        "raw": raw,
    }


def _evaluate_risk_classify(creator_mode: str, risk_result: object) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL"}
    raw = str(risk_result or "").strip()
    match = re.search(r"^RISK:\s*(low|medium|high)\b", raw, re.MULTILINE | re.IGNORECASE)
    risk_level = match.group(1).lower() if match else ""
    passed = (not required) or (bool(raw) and risk_level in {"low", "medium"})
    return {
        "required": required,
        "passed": passed,
        "risk_level": risk_level,
        "reason": "ok" if passed else (
            "risk_too_high" if risk_level == "high" else "missing_risk_classification"
        ),
        "raw": raw,
    }


def _normalise_runtime_e2e_result(runtime_e2e_result: object) -> dict:
    if runtime_e2e_result is None:
        return {}
    if isinstance(runtime_e2e_result, dict):
        return dict(runtime_e2e_result)
    if isinstance(runtime_e2e_result, str):
        text = runtime_e2e_result.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": text}
    return {"raw": str(runtime_e2e_result)}


def _normalise_gate_payload(
    value: object, *, required: bool, missing_reason: str,
) -> dict:
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
    payload["required"] = bool(required or payload.get("required") is True)
    if "passed" in payload:
        passed = payload["passed"]
        if isinstance(passed, bool):
            payload["passed"] = passed
        else:
            payload["passed_raw"] = passed
            payload["passed"] = False
            payload["reason"] = "invalid_gate_passed_type"
    else:
        payload["passed"] = False
    payload.setdefault("reason", "ok" if payload["passed"] else missing_reason)
    return payload


def _creator_quality_gates_required_from_gates(gates: dict) -> bool:
    mode = str(gates.get("creator_mode") or "").strip().upper()
    if mode in _CREATOR_QUALITY_REQUIRED_MODES:
        return True
    required_gate_names = (
        "collision_check",
        "risk_classify",
        "acceptance_compare",
        "runtime_e2e",
    )
    for name in required_gate_names:
        gate = gates.get(name)
        if isinstance(gate, dict) and gate.get("required") is True:
            return True
    return False


def _enforce_required_creator_quality_gates(gates: dict) -> bool:
    if _contains_stale_gate(gates):
        return False
    if not _creator_quality_gates_required_from_gates(gates):
        return gates.get("auto_enable_eligible") is True
    generation_quality_gate = gates.get("generation_quality")
    if not isinstance(generation_quality_gate, dict):
        generation_quality_gate = _normalise_gate_payload(
            None,
            required=True,
            missing_reason="missing_generation_quality_result",
        )
        gates["generation_quality"] = generation_quality_gate
    else:
        generation_quality_gate = _normalise_gate_payload(
            generation_quality_gate,
            required=True,
            missing_reason="missing_generation_quality_result",
        )
        gates["generation_quality"] = generation_quality_gate
    activation_gate = gates.get("activation_eval")
    if not isinstance(activation_gate, dict):
        activation_gate = _normalise_gate_payload(
            None,
            required=True,
            missing_reason="missing_activation_result",
        )
        gates["activation_eval"] = activation_gate
    else:
        activation_gate = _normalise_gate_payload(
            activation_gate,
            required=True,
            missing_reason="missing_activation_result",
        )
        gates["activation_eval"] = activation_gate
    return (
        gates.get("auto_enable_eligible") is True
        and generation_quality_gate.get("passed") is True
        and activation_gate.get("passed") is True
    )


def _contains_stale_gate(gates: dict) -> bool:
    for value in gates.values():
        if isinstance(value, dict) and value.get("stale") is True:
            return True
    return False


def _evaluate_runtime_e2e(
    creator_mode: str,
    runtime_e2e_result: object,
) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode == "FULL_GATED"
    payload = _normalise_runtime_e2e_result(runtime_e2e_result)
    if not payload:
        return {
            "required": required,
            "passed": not required,
            "reason": "missing_runtime_e2e_result" if required else "not_required",
            "winner": "",
            "cases": [],
        }
    winner = str(payload.get("winner") or "").strip().lower()
    cases = payload.get("cases")
    if not isinstance(cases, list):
        cases = []
    case_blockers: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_winner = str(case.get("winner") or "").strip().lower()
        regression = str(case.get("regression") or "").strip()
        if case_winner not in {"meta", "tie"}:
            case_blockers.append(f"case_{index}_winner:{case_winner or 'missing'}")
        if regression:
            case_blockers.append(f"case_{index}_regression")
    passed_value = payload.get("passed", False)
    passed_is_bool = isinstance(passed_value, bool)
    passed = (
        (not required)
        or (
            passed_value is True
            and winner in {"meta", "tie"}
            and not case_blockers
        )
    )
    reason = str(payload.get("reason") or "runtime_e2e_failed")
    if required and "passed" in payload and not passed_is_bool:
        reason = "invalid_runtime_e2e_passed_type"
    return {
        "required": required,
        "passed": passed,
        "reason": "ok" if passed else reason,
        "winner": winner,
        "baseline_model": payload.get("baseline_model", ""),
        "cases": cases,
        "diagnostics": case_blockers,
        "raw": payload.get("raw", ""),
    }


def write_proposal(
    home: Path,
    skill_md: str,
    lint_result: dict,
    smoke_result: dict,
    *,
    creator_mode: str = "",
    acceptance_result: object = None,
    runtime_e2e_result: object = None,
    collision_result: object = None,
    risk_result: object = None,
    generation_quality_result: object = None,
    activation_result: object = None,
) -> dict:
    """Atomic write + return the standard ``{status, proposal_id, ...}`` shape."""
    mode = (creator_mode or "").strip().upper()
    acceptance_gate = _evaluate_acceptance_compare(creator_mode, acceptance_result)
    runtime_gate = _evaluate_runtime_e2e(creator_mode, runtime_e2e_result)
    collision_gate = _evaluate_collision_check(creator_mode, collision_result)
    risk_gate = _evaluate_risk_classify(creator_mode, risk_result)
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
    # D1: ``degraded`` smoke (no fixture LLM available → deterministic
    # stub fixtures) flags G3/G4 as ``passed: True`` even though no
    # cross-vendor verification actually happened. Treating it as
    # eligible would let an unattended creator pipeline auto-enable a
    # candidate that has never been validated against a real model.
    # Refuse eligibility whenever the smoke result is degraded;
    # ``acceptance/runtime_e2e`` may still proceed so the proposal
    # lands for human review.
    smoke_degraded = bool(smoke_result.get("degraded", False))
    gate_eligible = (
        lint_result.get("G1", {}).get("passed") is True
        and lint_result.get("G2", {}).get("passed") is True
        and smoke_result.get("G3", {}).get("passed") is True
        and smoke_result.get("G4", {}).get("passed") is True
        and not smoke_degraded
    )
    eligible = (
        gate_eligible
        and bool(collision_gate.get("passed", False))
        and bool(risk_gate.get("passed", False))
        and bool(acceptance_gate.get("passed", False))
        and bool(runtime_gate.get("passed", False))
        and generation_quality_gate.get("passed") is True
        and activation_gate.get("passed") is True
    )
    gates = {
        "creator_mode": mode,
        "lint": lint_result,
        "smoke": smoke_result,
        "collision_check": collision_gate,
        "risk_classify": risk_gate,
        "acceptance_compare": acceptance_gate,
        "runtime_e2e": runtime_gate,
        "generation_quality": generation_quality_gate,
        "activation_eval": activation_gate,
        "auto_enable_eligible": eligible,
    }
    proposal_id = atomic_write_proposal(home, skill_md, gates)
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "auto_enable_eligible": eligible,
    }


def auto_enable_audit_from_gates(gates: dict) -> dict[str, object]:
    """Return a compact, UI-ready auto-enable audit summary."""
    auto_enable = gates.get("auto_enable")
    if not isinstance(auto_enable, dict):
        return {}
    details = auto_enable.get("details")
    if not isinstance(details, dict):
        details = {}
    reason = auto_enable.get("reason") or details.get("reason") or ""
    skills = details.get("skills")
    tools = details.get("tools")
    reasons = details.get("reasons")
    return {
        "status": auto_enable.get("status", "unknown"),
        "reason": reason,
        "risk_level": auto_enable.get("risk_level", details.get("risk_level", "unknown")),
        "max_risk": auto_enable.get("max_risk", details.get("max_risk", "unknown")),
        "validation_profile": details.get("validation_profile", "unknown"),
        "skills": skills if isinstance(skills, list) else [],
        "tools": tools if isinstance(tools, list) else [],
        "reasons": reasons if isinstance(reasons, list) else [],
    }


def list_proposals(home: Path) -> dict:
    """Snapshot of pending proposals (id + eligibility + provenance digest)."""
    proposals = proposals_dir(home)
    if not proposals.is_dir():
        return {"proposals": []}
    rows: list[dict] = []
    for sub in sorted(proposals.iterdir()):
        if not (sub / "SKILL.md").is_file():
            continue
        gates_path = sub / "gates.json"
        gates: dict = {}
        if gates_path.is_file():
            try:
                gates = json.loads(gates_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                gates = {}
        provenance = gates.get("provenance") or {}
        auto_enable_digest = auto_enable_audit_from_gates(gates)
        rows.append({
            "proposal_id": sub.name,
            "auto_enable_eligible": bool(gates.get("auto_enable_eligible", False)),
            "triggered_by": provenance.get("triggered_by", "manual"),
            "chain_hash": provenance.get("chain_hash"),
            "auto_enable": auto_enable_digest,
        })
    return {"proposals": rows}


def pending_count(home: Path) -> dict:
    """Number of pending proposals — cheap badge backend for the WebUI."""
    proposals = proposals_dir(home)
    if not proposals.is_dir():
        return {"count": 0}
    count = 0
    for sub in proposals.iterdir():
        if sub.is_dir() and (sub / "SKILL.md").is_file():
            count += 1
    return {"count": count}


def show_proposal(home: Path, proposal_id: str) -> dict:
    """Full payload for one proposal: SKILL.md text + gates.json."""
    if not is_valid_proposal_id(proposal_id):
        return {"status": "error", "reason": "invalid proposal_id format"}
    sub = proposals_dir(home) / proposal_id
    skill_path = sub / "SKILL.md"
    gates_path = sub / "gates.json"
    if not skill_path.is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    skill_md = skill_path.read_text(encoding="utf-8")
    gates: dict = {}
    if gates_path.is_file():
        try:
            gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gates = {}
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "skill_md": skill_md,
        "gates": gates,
        "auto_enable_audit": auto_enable_audit_from_gates(gates),
    }


def _split_skill_markdown(skill_md: str) -> tuple[dict, str]:
    match = re.match(
        r"\A---\s*\n(?P<frontmatter>.*?)\n---(?:\n(?P<body>.*)|\Z)",
        skill_md,
        re.DOTALL,
    )
    if not match:
        raise ValueError("missing_skill_frontmatter")
    raw_frontmatter = yaml.safe_load(match.group("frontmatter")) or {}
    if not isinstance(raw_frontmatter, dict):
        raise ValueError("skill_frontmatter_not_mapping")
    return raw_frontmatter, match.group("body") or ""


def _render_skill_markdown(frontmatter: dict, body: str) -> str:
    rendered_frontmatter = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
    )
    if not rendered_frontmatter.endswith("\n"):
        rendered_frontmatter += "\n"
    return f"---\n{rendered_frontmatter}---\n{body}"


def _json_safe_value(value: object, operation: str) -> Any:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"invalid_patch_operation:{operation}")
        return value
    if isinstance(value, list):
        return [_json_safe_value(item, operation) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"invalid_patch_operation:{operation}")
        return {
            key: _json_safe_value(item, operation)
            for key, item in value.items()
        }
    raise ValueError(f"invalid_patch_operation:{operation}")


def _string_list(value: object, operation: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"invalid_patch_operation:{operation}")
    return list(value)


def _dict_value(value: object, operation: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"invalid_patch_operation:{operation}")
    return dict(_json_safe_value(value, operation))


def _list_of_dicts(value: object, operation: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"invalid_patch_operation:{operation}")
    return [dict(_json_safe_value(item, operation)) for item in value]


def _apply_patch_request(
    frontmatter: dict,
    body: str,
    patch_request: dict,
) -> tuple[dict, str, list[str]]:
    revised = deepcopy(frontmatter)
    revised_body = body
    applied_operations: list[str] = []

    if "set_description" in patch_request:
        description = patch_request["set_description"]
        if not isinstance(description, str):
            raise ValueError("invalid_patch_operation:set_description")
        revised["description"] = description
        applied_operations.append("set_description")

    if "add_triggers" in patch_request:
        additions = _string_list(patch_request["add_triggers"], "add_triggers")
        current = revised.get("triggers")
        triggers = list(current) if isinstance(current, list) else []
        for trigger in additions:
            if trigger not in triggers:
                triggers.append(trigger)
        revised["triggers"] = triggers
        applied_operations.append("add_triggers")

    if "remove_triggers" in patch_request:
        removals = set(_string_list(patch_request["remove_triggers"], "remove_triggers"))
        current = revised.get("triggers")
        triggers = list(current) if isinstance(current, list) else []
        revised["triggers"] = [
            trigger for trigger in triggers
            if not isinstance(trigger, str) or trigger not in removals
        ]
        applied_operations.append("remove_triggers")

    if "merge_metadata_opensquilla" in patch_request:
        patch_metadata = _dict_value(
            patch_request["merge_metadata_opensquilla"],
            "merge_metadata_opensquilla",
        )
        metadata = revised.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        opensquilla_metadata = metadata.get("opensquilla")
        if not isinstance(opensquilla_metadata, dict):
            opensquilla_metadata = {}
        metadata["opensquilla"] = {
            **opensquilla_metadata,
            **patch_metadata,
        }
        revised["metadata"] = metadata
        applied_operations.append("merge_metadata_opensquilla")

    if "merge_output_contract" in patch_request:
        patch_output_contract = _dict_value(
            patch_request["merge_output_contract"],
            "merge_output_contract",
        )
        output_contract = revised.get("output_contract")
        if not isinstance(output_contract, dict):
            output_contract = {}
        revised["output_contract"] = {
            **output_contract,
            **patch_output_contract,
        }
        applied_operations.append("merge_output_contract")

    if "append_eval_prompts" in patch_request:
        prompts = _list_of_dicts(
            patch_request["append_eval_prompts"],
            "append_eval_prompts",
        )
        current = revised.get("eval_prompts")
        eval_prompts = list(current) if isinstance(current, list) else []
        eval_prompts.extend(prompts)
        revised["eval_prompts"] = eval_prompts
        applied_operations.append("append_eval_prompts")

    if "append_body" in patch_request:
        append_body = patch_request["append_body"]
        if not isinstance(append_body, str):
            raise ValueError("invalid_patch_operation:append_body")
        if revised_body and not revised_body.endswith("\n"):
            revised_body += "\n"
        revised_body += append_body
        applied_operations.append("append_body")

    return (
        revised,
        revised_body,
        [
            operation for operation in _PATCH_APPLIED_OPERATION_ORDER
            if operation in applied_operations
        ],
    )


def _gate_previous_passed(previous: object) -> bool | None:
    if not isinstance(previous, dict):
        return None
    passed = previous.get("passed")
    if isinstance(passed, bool):
        return passed
    child_passed_values = [
        value.get("passed")
        for value in previous.values()
        if isinstance(value, dict) and isinstance(value.get("passed"), bool)
    ]
    if child_passed_values:
        return all(child_passed_values)
    return None


def _stale_gate(name: str, previous: object, required: bool = True) -> dict:
    if isinstance(previous, dict) and isinstance(previous.get("required"), bool):
        required = previous["required"]
    stale = {
        "required": required,
        "passed": False,
        "reason": "stale_after_patch",
        "stale": True,
    }
    previous_passed = _gate_previous_passed(previous)
    if previous_passed is not None:
        stale["previous_passed"] = previous_passed
    if isinstance(previous, dict):
        stale["previous"] = previous
    if name == "smoke" and "previous_passed" not in stale:
        stale["previous_passed"] = False
    return stale


def _next_revision(parent_gates: dict, parent_id: str, patch_request: dict) -> dict:
    parent_revision = parent_gates.get("revision")
    owner = patch_request.get("owner", "")
    if not isinstance(owner, str):
        raise ValueError("invalid_patch_operation:owner")
    if isinstance(parent_revision, dict):
        revision_number = _parent_revision_number(parent_revision.get("revision")) + 1
        root_proposal_id = str(parent_revision.get("root_proposal_id") or parent_id)
    else:
        revision_number = 2
        root_proposal_id = parent_id
    return {
        "parent_proposal_id": parent_id,
        "root_proposal_id": root_proposal_id,
        "revision": revision_number,
        "owner": owner,
    }


def _parent_revision_number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_parent_revision")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError("invalid_parent_revision")


def _lint_revised_skill(skill_md: str) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, str(_LINT_SCRIPT), "--skill-md-stdin", "--gates", "G1,G2"],
            input=skill_md,
            capture_output=True,
            text=True,
            check=False,
        )
        lint_raw = proc.stdout or json.dumps({
            "passed": False,
            "reason": "linter_subprocess_failed",
            "stderr": proc.stderr[:500],
            "returncode": proc.returncode,
        })
        lint_payload = json.loads(lint_raw)
    except Exception as exc:  # noqa: BLE001 - gate payload must capture any lint failure.
        return {
            "passed": False,
            "reason": "lint_failed",
            "error": str(exc)[:500],
        }
    if not isinstance(lint_payload, dict):
        return {
            "passed": False,
            "reason": "lint_output_not_mapping",
            "raw": lint_payload,
        }
    return lint_payload


def patch_proposal(home: Path, proposal_id: str, patch_request: dict) -> dict:
    """Create a revised pending proposal from an allowlisted structured patch."""
    if not is_valid_proposal_id(proposal_id):
        return {"status": "error", "reason": "invalid proposal_id format"}
    if not isinstance(patch_request, dict):
        return {"status": "refused", "reason": "invalid_patch_request"}
    unsupported = sorted(set(patch_request) - _PATCH_ALLOWED_OPERATIONS)
    if unsupported:
        return {
            "status": "refused",
            "reason": f"unsupported_patch_operations:{','.join(unsupported)}",
        }

    parent_dir = proposals_dir(home) / proposal_id
    skill_path = parent_dir / "SKILL.md"
    gates_path = parent_dir / "gates.json"
    if not skill_path.is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    parent_skill_md = skill_path.read_text(encoding="utf-8")
    parent_gates: dict = {}
    if gates_path.is_file():
        try:
            parent_gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            parent_gates = {}

    try:
        frontmatter, body = _split_skill_markdown(parent_skill_md)
        revised_frontmatter, revised_body, applied_operations = _apply_patch_request(
            frontmatter,
            body,
            patch_request,
        )
        revision = _next_revision(parent_gates, proposal_id, patch_request)
    except (ValueError, yaml.YAMLError) as exc:
        return {"status": "refused", "reason": str(exc)}

    try:
        revised_skill_md = _render_skill_markdown(revised_frontmatter, revised_body)
    except (TypeError, yaml.YAMLError) as exc:
        return {"status": "refused", "reason": f"invalid_patch_render:{str(exc)[:120]}"}
    revision["applied_operations"] = applied_operations
    gates = {
        "creator_mode": "PATCH_PROPOSAL",
        "revision": revision,
        "lint": _lint_revised_skill(revised_skill_md),
        "auto_enable_eligible": False,
    }
    for gate_name in _PATCH_STALE_GATES:
        gates[gate_name] = _stale_gate(
            gate_name,
            parent_gates.get(gate_name),
            required=(gate_name == "smoke"),
        )

    child_id = atomic_write_proposal(home, revised_skill_md, gates)
    return {
        "status": "ok",
        "proposal_id": child_id,
        "parent_proposal_id": proposal_id,
        "auto_enable_eligible": False,
    }


def _load_pending_skill(home: Path, proposal_id: str) -> tuple[str | None, dict | None]:
    if not is_valid_proposal_id(proposal_id):
        return None, {
            "status": "error",
            "reason": "invalid proposal_id format",
            "proposal_id": proposal_id,
        }
    proposal_dir = proposals_dir(home) / proposal_id
    skill_path = proposal_dir / "SKILL.md"
    if not skill_path.is_file():
        return None, {
            "status": "error",
            "reason": f"proposal {proposal_id} not found",
            "proposal_id": proposal_id,
        }
    return skill_path.read_text(encoding="utf-8"), None


def _normalise_eval_prompts(value: object) -> list[dict]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [{
                "name": "inline-prompt",
                "prompt": text,
            }]
        return _normalise_eval_prompts(parsed)
    if not isinstance(value, list):
        return []
    prompts: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            prompt = item.strip()
            if not prompt or prompt in seen:
                continue
            seen.add(prompt)
            prompts.append({
                "name": f"prompt-{len(prompts) + 1}",
                "prompt": prompt,
            })
            continue
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt or prompt in seen:
            continue
        seen.add(prompt)
        prompts.append(dict(_json_safe_value(item, "eval_prompts")))
    return prompts


def _extract_eval_prompts_from_skill(skill_md: str) -> list[dict]:
    try:
        frontmatter, _body = _split_skill_markdown(skill_md)
    except (ValueError, yaml.YAMLError):
        return []
    return _normalise_eval_prompts(frontmatter.get("eval_prompts"))


def _benchmark_prompts(
    baseline_skill_md: str,
    candidate_skill_md: str,
    eval_prompts: object,
) -> list[dict]:
    explicit = _normalise_eval_prompts(eval_prompts)
    if explicit:
        return explicit
    candidate_prompts = _extract_eval_prompts_from_skill(candidate_skill_md)
    if candidate_prompts:
        return candidate_prompts
    return _extract_eval_prompts_from_skill(baseline_skill_md)


def _normalise_benchmark_result(comparison_result: object) -> dict:
    if comparison_result is None:
        return {}
    if isinstance(comparison_result, dict):
        return dict(comparison_result)
    if isinstance(comparison_result, str):
        text = comparison_result.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": text}
    return {"raw": str(comparison_result)}


def _evaluate_benchmark_compare(
    comparison_result: object,
    prompt_count: int,
) -> dict:
    payload = _normalise_benchmark_result(comparison_result)
    if not payload:
        return {
            "required": True,
            "passed": False,
            "reason": "missing_benchmark_comparison",
            "winner": "",
            "cases": [],
            "prompt_count": prompt_count,
        }
    winner = str(payload.get("winner") or "").strip().lower()
    cases = payload.get("cases")
    if not isinstance(cases, list):
        cases = []
    blockers: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_winner = str(case.get("winner") or "").strip().lower()
        regression = str(case.get("regression") or "").strip()
        if case_winner not in {"candidate", "tie"}:
            blockers.append(f"case_{index}_winner:{case_winner or 'missing'}")
        if regression:
            blockers.append(f"case_{index}_regression")

    passed_value = payload.get("passed", False)
    passed_is_bool = isinstance(passed_value, bool)
    quality_score: float | None = None
    quality_score_raw = payload.get("quality_score")
    if quality_score_raw not in (None, ""):
        try:
            quality_score = float(str(quality_score_raw))
        except (TypeError, ValueError):
            quality_score = None
    quality_passed = quality_score is None or quality_score >= 0.80
    if not quality_passed:
        blockers.append("quality_score_below_0.80")

    passed = (
        passed_value is True
        and passed_is_bool
        and winner in {"candidate", "tie"}
        and not blockers
    )
    reason = str(payload.get("reason") or "benchmark_compare_failed")
    if "passed" in payload and not passed_is_bool:
        reason = "invalid_benchmark_passed_type"
    return {
        "required": True,
        "passed": passed,
        "reason": "ok" if passed else reason,
        "winner": winner,
        "quality_score": quality_score,
        "prompt_count": prompt_count,
        "cases": [
            dict(_json_safe_value(case, "benchmark_cases"))
            for case in cases
            if isinstance(case, dict)
        ],
        "diagnostics": blockers,
        "raw": payload.get("raw", ""),
    }


def benchmark_proposals(
    home: Path,
    baseline_proposal_id: str,
    candidate_proposal_id: str,
    *,
    eval_prompts: object = None,
    comparison_result: object = None,
) -> dict:
    """Record a non-promoting A/B benchmark for two pending proposals."""
    baseline_skill_md, baseline_error = _load_pending_skill(home, baseline_proposal_id)
    if baseline_error is not None:
        return baseline_error
    candidate_skill_md, candidate_error = _load_pending_skill(home, candidate_proposal_id)
    if candidate_error is not None:
        return candidate_error
    assert baseline_skill_md is not None
    assert candidate_skill_md is not None

    prompts = _benchmark_prompts(baseline_skill_md, candidate_skill_md, eval_prompts)
    if not prompts:
        return {
            "status": "unavailable",
            "reason": "benchmark_prompts_missing",
            "baseline_proposal_id": baseline_proposal_id,
            "candidate_proposal_id": candidate_proposal_id,
        }

    benchmark_gate = _evaluate_benchmark_compare(comparison_result, len(prompts))
    report = {
        "creator_mode": "BENCHMARK",
        "baseline_proposal_id": baseline_proposal_id,
        "candidate_proposal_id": candidate_proposal_id,
        "eval_prompts": prompts,
        "gates": {
            "benchmark_compare": benchmark_gate,
        },
        "auto_enable_eligible": False,
    }
    benchmark_id = atomic_write_benchmark(home, report)
    return {
        "status": "ok",
        "benchmark_id": benchmark_id,
        "baseline_proposal_id": baseline_proposal_id,
        "candidate_proposal_id": candidate_proposal_id,
        "auto_enable_eligible": False,
        "passed": benchmark_gate["passed"],
    }


def _read_gates(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _proposal_id_from_gates(gates: dict) -> str:
    lifecycle = gates.get("lifecycle")
    if isinstance(lifecycle, dict):
        proposal_id = str(lifecycle.get("accepted_proposal_id") or "")
        if is_valid_proposal_id(proposal_id):
            return proposal_id
    auto_enable = gates.get("auto_enable")
    if isinstance(auto_enable, dict):
        proposal_id = str(auto_enable.get("proposal_id") or "")
        if is_valid_proposal_id(proposal_id):
            return proposal_id
    return uuid.uuid4().hex[:8]


def _archive_managed_skill(
    home: Path,
    skill_name: str,
    src: Path,
    *,
    proposal_id: str,
    reason: str,
) -> dict:
    archive_id = uuid.uuid4().hex[:8]
    dst = rollback_dir(home) / skill_name / archive_id
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {
        "skill_name": skill_name,
        "proposal_id": proposal_id,
        "archive_id": archive_id,
        "reason": reason,
        "path": str(dst),
    }


def _merge_lifecycle(gates: dict, lifecycle_patch: dict) -> dict:
    lifecycle = gates.get("lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    gates["lifecycle"] = {
        **lifecycle,
        **lifecycle_patch,
    }
    return gates


def accept_proposal(
    home: Path,
    proposal_id: str,
    force: bool = False,
    *,
    replace: bool = False,
    owner: str = "",
) -> dict:
    """Promote a proposal to the MANAGED skills layer."""
    if not is_valid_proposal_id(proposal_id):
        return {
            "status": "error",
            "reason": (
                f"invalid proposal_id format (expected 8 hex chars): {proposal_id!r}"
            ),
        }
    src = proposals_dir(home) / proposal_id
    if not (src / "SKILL.md").is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    gates = _read_gates(src / "gates.json")
    if _contains_stale_gate(gates):
        return {
            "status": "refused",
            "reason": "stale patch revision gates must be refreshed before accept",
            "gates": gates,
        }
    gates_passed = _enforce_required_creator_quality_gates(gates)
    if not gates_passed and not force:
        return {
            "status": "refused",
            "reason": "gates not all passed; use --force to override",
            "gates": gates,
        }
    skill_md = (src / "SKILL.md").read_text(encoding="utf-8")
    # Accept both `name: foo` and `name: "foo"` (creator's tojson emits quoted).
    name_match = re.search(r'^name:\s*"?([\w\-]+)"?\s*$', skill_md, re.MULTILINE)
    if not name_match:
        return {"status": "error", "reason": "cannot parse skill name from SKILL.md"}
    name = name_match.group(1)

    dst = skills_dir(home) / name
    rollback_target: dict | None = None
    if dst.exists() and not replace:
        return {"status": "refused", "reason": f"skill {name} already exists at {dst}"}
    if dst.exists():
        existing_gates = _read_gates(dst / "gates.json")
        existing_proposal_id = _proposal_id_from_gates(existing_gates)
        rollback_target = _archive_managed_skill(
            home,
            name,
            dst,
            proposal_id=existing_proposal_id,
            reason="superseded",
        )
        gates = _merge_lifecycle(
            gates,
            {
                "status": "active",
                "accepted_proposal_id": proposal_id,
                "owner": owner,
                "supersedes": {
                    "skill_name": name,
                    "proposal_id": existing_proposal_id,
                    "path": rollback_target["path"],
                },
                "rollback_target": rollback_target,
            },
        )
        (src / "gates.json").write_text(
            json.dumps(gates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        gates = _merge_lifecycle(
            gates,
            {
                "status": "active",
                "accepted_proposal_id": proposal_id,
                "owner": owner,
            },
        )
        (src / "gates.json").write_text(
            json.dumps(gates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    result = {"status": "ok", "skill_path": str(dst), "name": name}
    if rollback_target is not None:
        result["replaced"] = True
        result["rollback_target"] = rollback_target
    return result


def list_auto_enabled_skills(home: Path) -> dict:
    """Return managed skills that were promoted by auto-enable."""
    managed = skills_dir(home)
    if not managed.is_dir():
        return {"skills": []}
    rows: list[dict] = []
    for sub in sorted(managed.iterdir()):
        if not (sub / "SKILL.md").is_file():
            continue
        gates_path = sub / "gates.json"
        if not gates_path.is_file():
            continue
        try:
            gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        auto_enable = gates.get("auto_enable")
        if not isinstance(auto_enable, dict):
            continue
        if auto_enable.get("status") != "enabled":
            continue
        audit = auto_enable_audit_from_gates(gates)
        rows.append({
            "name": sub.name,
            "proposal_id": auto_enable.get("proposal_id"),
            "risk_level": auto_enable.get("risk_level", "unknown"),
            "max_risk": auto_enable.get("max_risk", "unknown"),
            "triggered_by": auto_enable.get("triggered_by", "unknown"),
            "enabled_at_ms": auto_enable.get("enabled_at_ms"),
            "validation_profile": audit.get("validation_profile", "unknown"),
            "skills": audit.get("skills", []),
            "tools": audit.get("tools", []),
            "reasons": audit.get("reasons", []),
        })
    return {"skills": rows}


def disable_auto_enabled_skill(home: Path, name: str) -> dict:
    """Move an auto-enabled managed skill back to proposals for review."""
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
        return {"status": "error", "reason": "invalid skill name"}
    src = skills_dir(home) / name
    if not (src / "SKILL.md").is_file():
        return {"status": "error", "reason": f"skill {name} not found"}
    gates_path = src / "gates.json"
    gates: dict = {}
    if gates_path.is_file():
        try:
            parsed = json.loads(gates_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                gates = parsed
        except (json.JSONDecodeError, OSError):
            gates = {}
    auto_enable = gates.get("auto_enable")
    if not isinstance(auto_enable, dict) or auto_enable.get("status") != "enabled":
        return {"status": "refused", "reason": f"skill {name} is not auto-enabled"}

    proposal_id = str(auto_enable.get("proposal_id") or uuid.uuid4().hex[:8])
    if not is_valid_proposal_id(proposal_id) or (proposals_dir(home) / proposal_id).exists():
        proposal_id = uuid.uuid4().hex[:8]
    proposals_dir(home).mkdir(parents=True, exist_ok=True)
    dst = proposals_dir(home) / proposal_id

    disabled = dict(auto_enable)
    disabled["previous_status"] = auto_enable.get("status")
    disabled["status"] = "disabled"
    disabled["proposal_id"] = proposal_id
    gates["auto_enable"] = disabled
    gates_path.write_text(json.dumps(gates, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.move(str(src), str(dst))
    return {"status": "ok", "proposal_id": proposal_id, "name": name}


def rollback_skill(home: Path, name: str) -> dict:
    """Restore a managed skill from its recorded rollback target."""
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
        return {"status": "error", "reason": "invalid skill name"}
    current = skills_dir(home) / name
    if not (current / "SKILL.md").is_file():
        return {"status": "error", "reason": f"skill {name} not found"}
    current_gates = _read_gates(current / "gates.json")
    lifecycle = current_gates.get("lifecycle")
    rollback_target = (
        lifecycle.get("rollback_target")
        if isinstance(lifecycle, dict)
        else None
    )
    if not isinstance(rollback_target, dict):
        return {"status": "refused", "reason": f"skill {name} has no rollback target"}
    target_path = Path(str(rollback_target.get("path") or ""))
    target_proposal_id = str(rollback_target.get("proposal_id") or "")
    if not is_valid_proposal_id(target_proposal_id):
        return {"status": "refused", "reason": "rollback target proposal_id is invalid"}
    if not (target_path / "SKILL.md").is_file():
        return {"status": "refused", "reason": "rollback target is missing"}

    current_proposal_id = _proposal_id_from_gates(current_gates)
    archived_current = _archive_managed_skill(
        home,
        name,
        current,
        proposal_id=current_proposal_id,
        reason="rollback_current",
    )
    current.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target_path), str(current))

    restored_gates = _read_gates(current / "gates.json")
    restored_gates = _merge_lifecycle(
        restored_gates,
        {
            "status": "rolled_back_active",
            "accepted_proposal_id": target_proposal_id,
            "rolled_back_from": {
                "skill_name": name,
                "proposal_id": current_proposal_id,
                "path": archived_current["path"],
            },
            "rollback_target": archived_current,
        },
    )
    (current / "gates.json").write_text(
        json.dumps(restored_gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "name": name,
        "restored_proposal_id": target_proposal_id,
        "skill_path": str(current),
        "archived_current": archived_current,
    }


def reject_proposal(home: Path, proposal_id: str) -> dict:
    """Delete the proposal directory. Idempotent — re-deleting is fine."""
    if not is_valid_proposal_id(proposal_id):
        return {
            "status": "error",
            "reason": (
                f"invalid proposal_id format (expected 8 hex chars): {proposal_id!r}"
            ),
        }
    target = proposals_dir(home) / proposal_id
    if not target.is_dir():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    shutil.rmtree(target)
    return {"status": "ok", "proposal_id": proposal_id}


# ─── Auto-propose settings (Path 1/2 runtime toggle) ──────────────────

_AUTO_PROPOSE_BOOL_SETTINGS_KEYS = ("enabled", "on_dream_complete", "auto_enable")
_AUTO_PROPOSE_SETTINGS_KEYS = (*_AUTO_PROPOSE_BOOL_SETTINGS_KEYS, "auto_enable_max_risk")


def auto_propose_settings_path(home: Path) -> Path:
    """Path to the per-installation runtime overrides JSON."""
    return home / "state" / "auto_propose_settings.json"


def read_auto_propose_settings(home: Path) -> dict[str, object]:
    """Return the persisted runtime overrides, or {} when not present.

    The dict is keyed by ``enabled``, ``on_dream_complete``, and/or
    ``auto_enable``. Missing keys mean "no override" — the caller should fall
    back to the toml / pydantic-settings default.
    """
    path = auto_propose_settings_path(home)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, object] = {
        k: bool(v) for k, v in payload.items()
        if k in _AUTO_PROPOSE_BOOL_SETTINGS_KEYS and isinstance(v, bool)
    }
    risk = payload.get("auto_enable_max_risk")
    if isinstance(risk, str) and risk in RISK_LEVELS:
        out["auto_enable_max_risk"] = risk
    return out


def write_auto_propose_settings(home: Path, settings: dict[str, object]) -> None:
    """Persist the runtime overrides atomically. Unknown keys are dropped."""
    path = auto_propose_settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitised: dict[str, object] = {
        k: bool(settings.get(k))
        for k in _AUTO_PROPOSE_BOOL_SETTINGS_KEYS
        if k in settings
    }
    risk = settings.get("auto_enable_max_risk")
    if isinstance(risk, str) and risk in RISK_LEVELS:
        sanitised["auto_enable_max_risk"] = risk
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitised, indent=2), encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "PROPOSAL_ID_PATTERN",
    "atomic_write_proposal",
    "accept_proposal",
    "auto_enable_audit_from_gates",
    "auto_propose_settings_path",
    "disable_auto_enabled_skill",
    "is_valid_proposal_id",
    "list_auto_enabled_skills",
    "list_proposals",
    "patch_proposal",
    "pending_count",
    "proposals_dir",
    "read_auto_propose_settings",
    "reject_proposal",
    "rollback_dir",
    "rollback_skill",
    "show_proposal",
    "skills_dir",
    "write_auto_propose_settings",
    "write_proposal",
]
