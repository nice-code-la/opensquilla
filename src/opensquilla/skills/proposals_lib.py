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
import time
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

PROPOSAL_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
SKILL_NAME_PATTERN = re.compile(r"[\w\-]+")
RISK_LEVELS = frozenset({"low", "medium", "high"})
_CREATOR_LEARNING_EVENT_TYPES = frozenset({
    "accepted",
    "benchmarked",
    "failed",
    "rolled_back",
})
_CREATOR_LEARNING_STRING_FIELDS = (
    "creator_mode",
    "gate_name",
    "outcome",
    "passed_gate_names",
    "preferred_pattern",
    "reason",
    "review_signal",
    "selected_pattern",
    "skill_chain",
    "source",
    "stage",
    "target_stage",
    "task_class",
    "trigger_samples",
)
_CREATOR_LEARNING_PROPOSAL_FIELDS = (
    "proposal_id",
    "baseline_proposal_id",
    "candidate_proposal_id",
    "restored_proposal_id",
)
_CREATOR_LESSON_PATTERNS = (
    "overbroad_trigger",
    "missing_input_contract",
    "missing_output_contract",
    "weak_negative_cases",
    "bad_eval_prompts",
    "benchmark_regression",
    "rollback_after_accept",
)
_CREATOR_FEEDBACK_STAGES = (
    "intent_brief",
    "pattern_picker",
    "context_builder",
    "slot_generator",
    "gate_calibration",
    "review_ux",
)
_CREATOR_FEEDBACK_PATTERN_ORDER = (
    "stage_feedback",
    "reject_reason_feedback",
    "history_failure_path",
    "missing_input_contract",
    "wrong_pattern",
    "missed_existing_skill",
    "overbroad_trigger",
    "missing_output_contract",
    "weak_negative_cases",
    "bad_eval_prompts",
    "benchmark_regression",
    "rollback_after_accept",
    "review_summary_gap",
)
_LESSON_PATTERN_KEYWORDS = {
    "overbroad_trigger": (
        "overbroad",
        "broad trigger",
        "false positive",
        "collision",
    ),
    "missing_input_contract": (
        "missing input",
        "input contract",
        "required source",
    ),
    "missing_output_contract": (
        "missing output",
        "output contract",
        "final artifact",
    ),
    "weak_negative_cases": (
        "negative case",
        "skip prompt",
        "adjacent task",
    ),
    "bad_eval_prompts": (
        "bad eval",
        "self-referential",
        "eval prompt",
    ),
    "benchmark_regression": (
        "benchmark regression",
        "candidate lost",
        "regression",
    ),
}
_FEEDBACK_PATTERN_TO_STAGE = {
    "stage_feedback": "slot_generator",
    "reject_reason_feedback": "review_ux",
    "history_failure_path": "slot_generator",
    "missing_input_contract": "intent_brief",
    "wrong_pattern": "pattern_picker",
    "missed_existing_skill": "context_builder",
    "overbroad_trigger": "slot_generator",
    "missing_output_contract": "slot_generator",
    "weak_negative_cases": "slot_generator",
    "bad_eval_prompts": "slot_generator",
    "benchmark_regression": "gate_calibration",
    "rollback_after_accept": "slot_generator",
    "review_summary_gap": "review_ux",
}
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
_BUNDLE_MANIFEST = "bundle.json"
_RESERVED_PROPOSAL_FILES = frozenset({"SKILL.md", "gates.json", _BUNDLE_MANIFEST})
_BUNDLE_REFERENCE_PATTERN = re.compile(
    r"(?<![\w./-])((?:scripts|evals|references)/[A-Za-z0-9_.\-/]+)",
)


def proposals_dir(home: Path) -> Path:
    return home / "proposals"


def skills_dir(home: Path) -> Path:
    return home / "skills"


def benchmarks_dir(home: Path) -> Path:
    return home / "proposal-benchmarks"


def rollback_dir(home: Path) -> Path:
    return home / "rollback"


def creator_learning_dir(home: Path) -> Path:
    return home / "creator-learning"


def creator_learning_events_path(home: Path) -> Path:
    return creator_learning_dir(home) / "events.jsonl"


def _bundle_manifest(files: dict[str, str]) -> dict:
    return {
        "version": 1,
        "kind": "skill_bundle",
        "entrypoint": "SKILL.md",
        "files": sorted(files),
    }


def _normalise_bundle_files(bundle_files: object) -> dict[str, str]:
    if bundle_files in (None, ""):
        return {}
    if not isinstance(bundle_files, dict):
        raise ValueError("invalid_bundle_files")
    normalised: dict[str, str] = {}
    for raw_path, content in bundle_files.items():
        if not isinstance(raw_path, str):
            raise ValueError("invalid_bundle_path")
        rel = raw_path.strip()
        path = PurePosixPath(rel)
        if (
            not rel
            or "\\" in rel
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or str(path) in _RESERVED_PROPOSAL_FILES
        ):
            raise ValueError(f"invalid_bundle_path:{raw_path}")
        if not isinstance(content, str):
            raise ValueError(f"invalid_bundle_content:{raw_path}")
        normalised[str(path)] = content
    return normalised


def _write_bundle_files(root: Path, bundle_files: dict[str, str]) -> dict:
    if not bundle_files:
        return {}
    manifest = _bundle_manifest(bundle_files)
    for rel_path, content in bundle_files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / _BUNDLE_MANIFEST).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _read_bundle_manifest(root: Path) -> dict:
    path = root / _BUNDLE_MANIFEST
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    files = parsed.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        return {}
    return {
        "version": parsed.get("version", 1),
        "kind": parsed.get("kind", "skill_bundle"),
        "entrypoint": parsed.get("entrypoint", "SKILL.md"),
        "files": list(files),
    }


def _read_bundle_files(root: Path) -> dict[str, str]:
    manifest = _read_bundle_manifest(root)
    files = manifest.get("files")
    if not isinstance(files, list):
        return {}
    out: dict[str, str] = {}
    for rel_path in files:
        try:
            safe = _normalise_bundle_files({rel_path: ""})
        except ValueError:
            continue
        path = root / next(iter(safe))
        if path.is_file():
            out[rel_path] = path.read_text(encoding="utf-8")
    return out


def _extract_bundle_references(skill_md: str) -> list[str]:
    references: list[str] = []
    for match in _BUNDLE_REFERENCE_PATTERN.finditer(skill_md):
        ref = match.group(1).rstrip(".,;:)'\"`]")
        if ref and ref not in references:
            references.append(ref)
    return references


def _evaluate_bundle_validation(skill_md: str, bundle_files: dict[str, str]) -> dict:
    references = _extract_bundle_references(skill_md)
    bundle_paths = sorted(bundle_files)
    missing_references = [
        ref for ref in references
        if ref not in bundle_files
    ]
    unreferenced_scripts = [
        path for path in bundle_paths
        if path.startswith("scripts/") and path not in references
    ]
    required = bool(references or bundle_files)
    passed = not missing_references and not unreferenced_scripts
    return {
        "required": required,
        "passed": passed,
        "reason": "ok" if passed else "bundle_validation_failed",
        "file_count": len(bundle_paths),
        "references": references,
        "missing_references": missing_references,
        "unreferenced_scripts": unreferenced_scripts,
    }


def is_valid_proposal_id(proposal_id: str | None) -> bool:
    if not proposal_id:
        return False
    return bool(PROPOSAL_ID_PATTERN.fullmatch(proposal_id))


def atomic_write_proposal(
    home: Path,
    skill_md: str,
    gates: dict,
    *,
    bundle_files: dict[str, str] | None = None,
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
    _write_bundle_files(tmp_dir, bundle_files or {})

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
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL", "PATCH_PROPOSAL"}
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
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL", "PATCH_PROPOSAL"}
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


def _clear_optional_stale_gates(gates: dict) -> None:
    for name in _PATCH_STALE_GATES:
        gate = gates.get(name)
        if (
            isinstance(gate, dict)
            and gate.get("stale") is True
            and gate.get("required") is False
        ):
            gates[name] = {
                "required": False,
                "passed": True,
                "reason": "not_required",
                "previous_passed": gate.get("previous_passed", False),
            }


def _proposal_gate_passed(gates: dict, name: str) -> bool:
    gate = gates.get(name)
    if name == "smoke":
        return _gate_previous_passed(gate) is True and not (
            isinstance(gate, dict) and gate.get("degraded") is True
        )
    return isinstance(gate, dict) and gate.get("passed") is True


def _recompute_auto_enable_eligible(gates: dict) -> bool:
    lint = gates.get("lint")
    lint_passed = (
        isinstance(lint, dict)
        and lint.get("G1", {}).get("passed") is True
        and lint.get("G2", {}).get("passed") is True
    )
    gate_names = (
        "smoke",
        "collision_check",
        "risk_classify",
        "acceptance_compare",
        "runtime_e2e",
        "generation_quality",
        "activation_eval",
        "bundle_validation",
    )
    return (
        lint_passed
        and not _contains_stale_gate(gates)
        and all(_proposal_gate_passed(gates, name) for name in gate_names)
    )


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


def _gate_problem(gate: dict) -> str:
    for key in ("required_improvements", "reason", "diagnostics", "raw"):
        value = gate.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            return text[:500]
    return "gate did not pass"


def _repair_hint_for_gate(gate_name: str, gate: dict) -> dict[str, object]:
    problem = _gate_problem(gate)
    if gate_name == "collision_check":
        return {
            "gate": gate_name,
            "problem": problem,
            "recommended_action": (
                "Patch the proposal with narrower trigger phrases and an explicit "
                "negative trigger boundary before accepting."
            ),
            "patch_operations": ["remove_triggers", "add_triggers"],
        }
    if gate_name == "acceptance_compare":
        return {
            "gate": gate_name,
            "problem": problem,
            "recommended_action": (
                "Patch the generated SKILL.md to close the required improvements, "
                "especially input parameters, output contract, and missing workflow "
                "steps called out by the reviewer."
            ),
            "patch_operations": ["merge_output_contract", "append_body"],
        }
    if gate_name == "generation_quality":
        return {
            "gate": gate_name,
            "problem": problem,
            "recommended_action": (
                "Regenerate or patch the candidate so every step has a concrete "
                "purpose, bounded inputs, and a checkable final deliverable."
            ),
            "patch_operations": ["set_description", "append_body"],
        }
    if gate_name == "activation_eval":
        return {
            "gate": gate_name,
            "problem": problem,
            "recommended_action": (
                "Patch triggers and negative examples so positive prompts route "
                "to this skill while unrelated catalog prompts do not."
            ),
            "patch_operations": ["remove_triggers", "add_triggers", "append_eval_prompts"],
        }
    if gate_name == "runtime_e2e":
        return {
            "gate": gate_name,
            "problem": problem,
            "recommended_action": (
                "Refresh runtime gates after patching the candidate workflow; if "
                "cases still fail, simplify the step graph or add explicit inputs."
            ),
            "patch_operations": ["append_body"],
        }
    return {
        "gate": gate_name,
        "problem": problem,
        "recommended_action": "Patch the proposal, then refresh gates before accepting.",
        "patch_operations": ["append_body"],
    }


def _build_repair_hints(gates: dict) -> list[dict[str, object]]:
    hints: list[dict[str, object]] = []
    for gate_name in (
        "collision_check",
        "acceptance_compare",
        "generation_quality",
        "activation_eval",
        "runtime_e2e",
        "risk_classify",
        "bundle_validation",
    ):
        gate = gates.get(gate_name)
        if not isinstance(gate, dict):
            continue
        if gate.get("required") is True and gate.get("passed") is not True:
            hints.append(_repair_hint_for_gate(gate_name, gate))
    return hints


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
    bundle_files: object = None,
) -> dict:
    """Atomic write + return the standard ``{status, proposal_id, ...}`` shape."""
    try:
        normalised_bundle_files = _normalise_bundle_files(bundle_files)
    except ValueError as exc:
        return {"status": "refused", "reason": str(exc)}
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
    bundle_validation_gate = _evaluate_bundle_validation(
        skill_md,
        normalised_bundle_files,
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
        and bundle_validation_gate.get("passed") is True
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
        "bundle_validation": bundle_validation_gate,
        "auto_enable_eligible": eligible,
    }
    repair_hints = _build_repair_hints(gates)
    if repair_hints:
        gates["repair_hints"] = repair_hints
    if normalised_bundle_files:
        gates["bundle"] = _bundle_manifest(normalised_bundle_files)
    proposal_id = atomic_write_proposal(
        home,
        skill_md,
        gates,
        bundle_files=normalised_bundle_files,
    )
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "auto_enable_eligible": eligible,
        "bundle": gates.get("bundle", {}),
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
        bundle = _read_bundle_manifest(sub)
        rows.append({
            "proposal_id": sub.name,
            "auto_enable_eligible": bool(gates.get("auto_enable_eligible", False)),
            "triggered_by": provenance.get("triggered_by", "manual"),
            "chain_hash": provenance.get("chain_hash"),
            "auto_enable": auto_enable_digest,
            "bundle": bundle,
            "bundle_file_count": len(bundle.get("files", [])),
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


def _proposal_triggers(skill_md: str) -> tuple[str, list[str]]:
    try:
        frontmatter, _body = _split_skill_markdown(skill_md)
    except (ValueError, yaml.YAMLError):
        return ("", [])
    name = str(frontmatter.get("name") or "").strip()
    raw_triggers = frontmatter.get("triggers")
    if isinstance(raw_triggers, list):
        triggers = [str(item).strip() for item in raw_triggers if str(item).strip()]
    elif isinstance(raw_triggers, str) and raw_triggers.strip():
        triggers = [raw_triggers.strip()]
    else:
        triggers = []
    return (name, triggers)


def _normalise_trigger_key(trigger: str) -> str:
    return " ".join(trigger.casefold().split())


def _stale_gate_names(gates: dict) -> list[str]:
    return sorted(
        name for name, value in gates.items()
        if isinstance(value, dict) and value.get("stale") is True
    )


def audit_proposal_drift(
    home: Path,
    *,
    rollback_heavy_threshold: int = 2,
    long_pending_days: int = 14,
    now_ms: int | None = None,
) -> dict:
    """Return a deterministic read-only drift report for proposal maintenance."""
    issues: list[dict] = []
    proposal_records: list[dict] = []
    now_seconds = (now_ms / 1000) if now_ms is not None else time.time()
    proposals = proposals_dir(home)
    if proposals.is_dir():
        for sub in sorted(proposals.iterdir()):
            skill_path = sub / "SKILL.md"
            if not skill_path.is_file():
                continue
            try:
                skill_mtime = skill_path.stat().st_mtime
            except OSError:
                skill_mtime = now_seconds
            gates = _read_gates(sub / "gates.json")
            skill_md = skill_path.read_text(encoding="utf-8")
            skill_name, triggers = _proposal_triggers(skill_md)
            proposal_records.append({
                "proposal_id": sub.name,
                "skill_name": skill_name,
                "triggers": triggers,
                "gates": gates,
                "mtime": skill_mtime,
            })
            days_pending = max(0, math.floor((now_seconds - skill_mtime) / 86400))
            if days_pending >= long_pending_days:
                issues.append({
                    "type": "long_pending_proposal",
                    "severity": "warning",
                    "proposal_id": sub.name,
                    "skill_name": skill_name,
                    "days_pending": days_pending,
                    "threshold_days": long_pending_days,
                })
            stale_gates = _stale_gate_names(gates)
            if stale_gates:
                issues.append({
                    "type": "stale_proposal",
                    "severity": "warning",
                    "proposal_id": sub.name,
                    "skill_name": skill_name,
                    "stale_gates": stale_gates,
                })

    trigger_map: dict[str, dict] = {}
    for record in proposal_records:
        for trigger in record["triggers"]:
            key = _normalise_trigger_key(trigger)
            if not key:
                continue
            bucket = trigger_map.setdefault(
                key,
                {
                    "trigger": trigger,
                    "proposal_ids": [],
                    "skill_names": [],
                },
            )
            bucket["proposal_ids"].append(record["proposal_id"])
            if record["skill_name"]:
                bucket["skill_names"].append(record["skill_name"])
    for bucket in trigger_map.values():
        proposal_ids = sorted(set(bucket["proposal_ids"]))
        if len(proposal_ids) < 2:
            continue
        issues.append({
            "type": "pending_trigger_collision",
            "severity": "warning",
            "trigger": bucket["trigger"],
            "proposal_ids": proposal_ids,
            "skill_names": sorted(set(bucket["skill_names"])),
        })

    managed = skills_dir(home)
    rollbacks = rollback_dir(home)
    if managed.is_dir():
        for sub in sorted(managed.iterdir()):
            if not (sub / "SKILL.md").is_file():
                continue
            history_root = rollbacks / sub.name
            rollback_count = 0
            if history_root.is_dir():
                rollback_count = sum(
                    1 for archive in history_root.iterdir()
                    if (archive / "SKILL.md").is_file()
                )
            if rollback_count >= rollback_heavy_threshold:
                issues.append({
                    "type": "rollback_heavy_skill",
                    "severity": "warning",
                    "skill_name": sub.name,
                    "rollback_count": rollback_count,
                    "threshold": rollback_heavy_threshold,
                })

    learning_buckets: dict[tuple[str, str, str], dict] = {}
    for event in _read_creator_learning_events(home):
        signal = _learning_text(event.get("event_type"), max_chars=80)
        if signal not in {"rolled_back", "failed"}:
            continue
        skill_name = _learning_text(event.get("skill_name"), max_chars=80)
        reason = _learning_text(event.get("reason"))
        if not skill_name and not reason:
            continue
        key = (signal, skill_name, reason)
        bucket = learning_buckets.setdefault(
            key,
            {
                "signal": signal,
                "skill_name": skill_name,
                "reason": reason,
                "event_count": 0,
                "proposal_ids": [],
            },
        )
        bucket["event_count"] += 1
        proposal_id = _learning_text(event.get("proposal_id"), max_chars=32)
        if is_valid_proposal_id(proposal_id):
            bucket["proposal_ids"].append(proposal_id)
    for bucket in learning_buckets.values():
        if bucket["event_count"] < 2:
            continue
        issues.append({
            "type": "repeated_learning_signal",
            "severity": "warning",
            "signal": bucket["signal"],
            "skill_name": bucket["skill_name"],
            "reason": bucket["reason"],
            "event_count": bucket["event_count"],
            "proposal_ids": sorted(set(bucket["proposal_ids"])),
        })

    counts = {
        "pending_proposals": len(proposal_records),
        "stale_proposals": sum(1 for issue in issues if issue["type"] == "stale_proposal"),
        "long_pending_proposals": sum(
            1 for issue in issues
            if issue["type"] == "long_pending_proposal"
        ),
        "pending_trigger_collisions": sum(
            1 for issue in issues
            if issue["type"] == "pending_trigger_collision"
        ),
        "rollback_heavy_skills": sum(
            1 for issue in issues
            if issue["type"] == "rollback_heavy_skill"
        ),
        "repeated_learning_signals": sum(
            1 for issue in issues
            if issue["type"] == "repeated_learning_signal"
        ),
    }
    return {
        "status": "ok",
        "counts": counts,
        "issues": sorted(
            issues,
            key=lambda issue: (
                str(issue.get("type") or ""),
                str(issue.get("proposal_id") or issue.get("skill_name") or ""),
                str(issue.get("trigger") or ""),
            ),
        ),
    }


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
    bundle = _read_bundle_manifest(sub)
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "skill_md": skill_md,
        "gates": gates,
        "bundle": bundle,
        "bundle_files": _read_bundle_files(sub),
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


def _current_proposal_revision(gates: dict) -> int:
    revision = gates.get("revision")
    if not isinstance(revision, dict):
        return 1
    return _parent_revision_number(revision.get("revision"))


def _check_patch_revision_conflict(
    parent_gates: dict,
    expected_parent_revision: int | None,
) -> dict | None:
    if expected_parent_revision is None:
        return None
    current_revision = _current_proposal_revision(parent_gates)
    latest_child_revision = parent_gates.get("latest_child_revision")
    latest_child_id = str(parent_gates.get("latest_child_proposal_id") or "")
    if expected_parent_revision != current_revision:
        return {
            "status": "refused",
            "reason": "revision_conflict",
            "current_revision": current_revision,
            "expected_parent_revision": expected_parent_revision,
            "latest_child_proposal_id": latest_child_id,
            "latest_child_revision": latest_child_revision,
        }
    if isinstance(latest_child_revision, int) and latest_child_revision > current_revision:
        return {
            "status": "refused",
            "reason": "revision_conflict",
            "current_revision": current_revision,
            "expected_parent_revision": expected_parent_revision,
            "latest_child_proposal_id": latest_child_id,
            "latest_child_revision": latest_child_revision,
        }
    return None


def _record_latest_child_revision(
    parent_dir: Path,
    parent_gates: dict,
    *,
    child_id: str,
    revision_number: int,
) -> None:
    parent_gates["latest_child_proposal_id"] = child_id
    parent_gates["latest_child_revision"] = revision_number
    (parent_dir / "gates.json").write_text(
        json.dumps(parent_gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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


def patch_proposal(
    home: Path,
    proposal_id: str,
    patch_request: dict,
    *,
    expected_parent_revision: int | None = None,
) -> dict:
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
    bundle_files = _read_bundle_files(parent_dir)
    try:
        conflict = _check_patch_revision_conflict(
            parent_gates,
            expected_parent_revision,
        )
    except ValueError as exc:
        return {"status": "refused", "reason": str(exc)}
    if conflict is not None:
        return conflict

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
        "bundle_validation": _evaluate_bundle_validation(
            revised_skill_md,
            bundle_files,
        ),
        "auto_enable_eligible": False,
    }
    if bundle_files:
        gates["bundle"] = _bundle_manifest(bundle_files)
    for gate_name in _PATCH_STALE_GATES:
        gates[gate_name] = _stale_gate(
            gate_name,
            parent_gates.get(gate_name),
            required=(gate_name == "smoke"),
        )

    child_id = atomic_write_proposal(
        home,
        revised_skill_md,
        gates,
        bundle_files=bundle_files,
    )
    _record_latest_child_revision(
        parent_dir,
        parent_gates,
        child_id=child_id,
        revision_number=revision["revision"],
    )
    return {
        "status": "ok",
        "proposal_id": child_id,
        "parent_proposal_id": proposal_id,
        "auto_enable_eligible": False,
    }


def refresh_proposal_gates(
    home: Path,
    proposal_id: str,
    *,
    smoke_result: object = None,
    collision_result: object = None,
    risk_result: object = None,
    acceptance_result: object = None,
    runtime_e2e_result: object = None,
    generation_quality_result: object = None,
    activation_result: object = None,
) -> dict:
    """Refresh stale gates on a pending proposal revision."""
    if not is_valid_proposal_id(proposal_id):
        return {"status": "error", "reason": "invalid proposal_id format"}
    proposal_dir = proposals_dir(home) / proposal_id
    if not (proposal_dir / "SKILL.md").is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    gates = _read_gates(proposal_dir / "gates.json")
    creator_mode = str(gates.get("creator_mode") or "PATCH_PROPOSAL")
    refreshed: list[str] = []

    if smoke_result is not None:
        smoke_gate = _normalise_gate_payload(
            smoke_result,
            required=True,
            missing_reason="missing_smoke_result",
        )
        if isinstance(smoke_result, dict) and any(
            isinstance(value, dict) and "passed" in value
            for value in smoke_result.values()
        ):
            smoke_gate = dict(smoke_result)
        gates["smoke"] = smoke_gate
        refreshed.append("smoke")
    if collision_result is not None:
        gates["collision_check"] = _evaluate_collision_check(
            "PATCH_PROPOSAL",
            collision_result,
        )
        refreshed.append("collision_check")
    if risk_result is not None:
        gates["risk_classify"] = _evaluate_risk_classify("PATCH_PROPOSAL", risk_result)
        refreshed.append("risk_classify")
    if acceptance_result is not None:
        existing = gates.get("acceptance_compare")
        mode = "FULL_GATED" if (
            isinstance(existing, dict) and existing.get("required") is True
        ) else creator_mode
        gates["acceptance_compare"] = _evaluate_acceptance_compare(
            mode,
            acceptance_result,
        )
        refreshed.append("acceptance_compare")
    if runtime_e2e_result is not None:
        existing = gates.get("runtime_e2e")
        mode = "FULL_GATED" if (
            isinstance(existing, dict) and existing.get("required") is True
        ) else creator_mode
        gates["runtime_e2e"] = _evaluate_runtime_e2e(mode, runtime_e2e_result)
        refreshed.append("runtime_e2e")
    if generation_quality_result is not None:
        gates["generation_quality"] = _normalise_gate_payload(
            generation_quality_result,
            required=True,
            missing_reason="missing_generation_quality_result",
        )
        refreshed.append("generation_quality")
    if activation_result is not None:
        gates["activation_eval"] = _normalise_gate_payload(
            activation_result,
            required=True,
            missing_reason="missing_activation_result",
        )
        refreshed.append("activation_eval")

    _clear_optional_stale_gates(gates)
    gates["auto_enable_eligible"] = _recompute_auto_enable_eligible(gates)
    (proposal_dir / "gates.json").write_text(
        json.dumps(gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "auto_enable_eligible": gates["auto_enable_eligible"],
        "refreshed": refreshed,
        "gates": gates,
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
    _safe_record_creator_learning_event(
        home,
        {
            "event_type": "benchmarked",
            "benchmark_id": benchmark_id,
            "baseline_proposal_id": baseline_proposal_id,
            "candidate_proposal_id": candidate_proposal_id,
            "passed": benchmark_gate["passed"],
            "reason": benchmark_gate.get("reason", ""),
        },
    )
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


def _normalise_deprecates(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raise ValueError("invalid_deprecates")
    names: list[str] = []
    for item in raw_items:
        if not item:
            continue
        if not SKILL_NAME_PATTERN.fullmatch(item):
            raise ValueError(f"invalid_deprecates:{item}")
        if item not in names:
            names.append(item)
    return names


def _record_archived_deprecation(
    archive_path: Path,
    *,
    skill_name: str,
    proposal_id: str,
    owner: str,
) -> None:
    gates = _read_gates(archive_path / "gates.json")
    gates = _merge_lifecycle(
        gates,
        {
            "status": "deprecated",
            "deprecated_by": {
                "skill_name": skill_name,
                "proposal_id": proposal_id,
                "owner": owner,
            },
        },
    )
    (archive_path / "gates.json").write_text(
        json.dumps(gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _creator_success_context_from_skill(skill_md: str, gates: dict) -> dict[str, str]:
    """Extract compact, low-risk success context from an accepted skill."""
    context: dict[str, str] = {}
    try:
        frontmatter, _body = _split_skill_markdown(skill_md)
    except (ValueError, yaml.YAMLError):
        frontmatter = {}
    raw_triggers = frontmatter.get("triggers")
    if isinstance(raw_triggers, list):
        triggers = [_learning_text(item, max_chars=120) for item in raw_triggers]
    elif isinstance(raw_triggers, str):
        triggers = [_learning_text(raw_triggers, max_chars=120)]
    else:
        triggers = []
    triggers = [trigger for trigger in triggers if trigger]
    if triggers:
        context["trigger_samples"] = " | ".join(triggers[:3])
    raw_steps = (
        frontmatter.get("composition", {}).get("steps")
        if isinstance(frontmatter.get("composition"), dict)
        else []
    )
    if isinstance(raw_steps, list):
        chain = []
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            skill = _learning_text(step.get("skill"), max_chars=80)
            if skill:
                chain.append(skill)
            if len(chain) >= 6:
                break
        if chain:
            context["skill_chain"] = " -> ".join(chain)
    metadata = frontmatter.get("metadata")
    opensquilla_meta = (
        metadata.get("opensquilla")
        if isinstance(metadata, dict) and isinstance(metadata.get("opensquilla"), dict)
        else {}
    )
    creator_pattern = _learning_text(
        opensquilla_meta.get("creator_pattern")
        or opensquilla_meta.get("selected_pattern"),
        max_chars=80,
    )
    if creator_pattern:
        context["selected_pattern"] = creator_pattern
    passed_gate_names = [
        name
        for name, gate in gates.items()
        if isinstance(gate, dict) and gate.get("passed") is True
    ]
    if passed_gate_names:
        context["passed_gate_names"] = ", ".join(sorted(passed_gate_names))
    creator_mode = _learning_text(gates.get("creator_mode"), max_chars=80)
    if creator_mode:
        context["creator_mode"] = creator_mode
    return context


def accept_proposal(
    home: Path,
    proposal_id: str,
    force: bool = False,
    *,
    replace: bool = False,
    owner: str = "",
    deprecates: object = None,
    migration_notes: str = "",
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
    try:
        deprecated_names = _normalise_deprecates(deprecates)
    except ValueError as exc:
        return {"status": "refused", "reason": str(exc)}
    migration_notes_text = migration_notes.strip() if isinstance(migration_notes, str) else ""
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
        _record_archived_deprecation(
            Path(rollback_target["path"]),
            skill_name=name,
            proposal_id=proposal_id,
            owner=owner,
        )
        lifecycle_patch = {
            "status": "active",
            "accepted_proposal_id": proposal_id,
            "owner": owner,
            "supersedes": {
                "skill_name": name,
                "proposal_id": existing_proposal_id,
                "path": rollback_target["path"],
            },
            "rollback_target": rollback_target,
        }
        if deprecated_names:
            lifecycle_patch["deprecates"] = deprecated_names
        if migration_notes_text:
            lifecycle_patch["migration_notes"] = migration_notes_text
        gates = _merge_lifecycle(
            gates,
            lifecycle_patch,
        )
        (src / "gates.json").write_text(
            json.dumps(gates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        lifecycle_patch = {
            "status": "active",
            "accepted_proposal_id": proposal_id,
            "owner": owner,
        }
        if deprecated_names:
            lifecycle_patch["deprecates"] = deprecated_names
        if migration_notes_text:
            lifecycle_patch["migration_notes"] = migration_notes_text
        gates = _merge_lifecycle(
            gates,
            lifecycle_patch,
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
    if deprecated_names:
        result["deprecates"] = deprecated_names
    if migration_notes_text:
        result["migration_notes"] = migration_notes_text
    _safe_record_creator_learning_event(
        home,
        {
            "event_type": "accepted",
            "proposal_id": proposal_id,
            "skill_name": name,
            "outcome": "accepted",
            "reason": "replace" if replace else "accept",
            **_creator_success_context_from_skill(skill_md, gates),
        },
    )
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
    result = {
        "status": "ok",
        "name": name,
        "restored_proposal_id": target_proposal_id,
        "skill_path": str(current),
        "archived_current": archived_current,
    }
    _safe_record_creator_learning_event(
        home,
        {
            "event_type": "rolled_back",
            "skill_name": name,
            "restored_proposal_id": target_proposal_id,
            "proposal_id": current_proposal_id,
            "reason": "rollback",
        },
    )
    return result


def reject_proposal(
    home: Path,
    proposal_id: str,
    *,
    reason: str = "",
    stage: str = "",
    task_class: str = "",
    selected_pattern: str = "",
    preferred_pattern: str = "",
    review_signal: str = "",
) -> dict:
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
    skill_name = ""
    skill_path = target / "SKILL.md"
    if skill_path.is_file():
        try:
            skill_md = skill_path.read_text(encoding="utf-8")
        except OSError:
            skill_md = ""
        name_match = re.search(r'^name:\s*"?([\w\-]+)"?\s*$', skill_md, re.MULTILINE)
        if name_match:
            skill_name = name_match.group(1)
    shutil.rmtree(target)
    if any((
        reason,
        stage,
        task_class,
        selected_pattern,
        preferred_pattern,
        review_signal,
    )):
        event: dict[str, object] = {
            "event_type": "failed",
            "proposal_id": proposal_id,
            "source": "reject_proposal",
            "reason": reason or "rejected proposal",
        }
        if skill_name:
            event["skill_name"] = skill_name
        if stage:
            event["stage"] = stage
        if task_class:
            event["task_class"] = task_class
        if selected_pattern:
            event["selected_pattern"] = selected_pattern
        if preferred_pattern:
            event["preferred_pattern"] = preferred_pattern
        if review_signal:
            event["review_signal"] = review_signal
        _safe_record_creator_learning_event(home, event)
    return {"status": "ok", "proposal_id": proposal_id}


def record_creator_history_failure_feedback(home: Path, failure_path: object) -> dict:
    """Record a compact historical failure path as creator feedback."""
    if not isinstance(failure_path, dict):
        return {"status": "error", "reason": "invalid_failure_path"}
    stage = _learning_text(failure_path.get("failed_stage"), max_chars=80)
    if stage not in _CREATOR_FEEDBACK_STAGES:
        stage = "slot_generator"
    task_class = _learning_text(failure_path.get("task_class_hint"), max_chars=120)
    failed_step = _learning_text(failure_path.get("failed_step_id"), max_chars=80)
    failed_skill = _learning_text(failure_path.get("failed_skill"), max_chars=120)
    error_family = _learning_text(failure_path.get("error_family"), max_chars=80)
    sample_reason = _learning_text(failure_path.get("sample_reason"))
    had_fallback = bool(failure_path.get("had_fallback"))
    fallback_clause = (
        "had fallback"
        if had_fallback
        else "without fallback"
    )
    reason_parts = [
        "historical failed step",
        failed_step,
        f"skill {failed_skill}" if failed_skill else "",
        f"error family {error_family}" if error_family else "",
        fallback_clause,
        sample_reason,
    ]
    event = {
        "event_type": "failed",
        "source": "history_failure_path",
        "stage": stage,
        "task_class": task_class or "global",
        "reason": "; ".join(part for part in reason_parts if part),
    }
    return record_creator_learning_event(home, event)


def _learning_text(value: object, *, max_chars: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def _learning_lessons(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    lessons: list[str] = []
    for item in raw_items:
        text = _learning_text(item)
        if text and text not in lessons:
            lessons.append(text)
        if len(lessons) >= 5:
            break
    return lessons


def _read_creator_learning_events(home: Path, *, max_events: int = 200) -> list[dict]:
    path = creator_learning_events_path(home)
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-max_events:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _lesson_pattern_for_event(event: dict) -> str | None:
    event_type = str(event.get("event_type") or "")
    reason = _learning_text(event.get("reason")).lower()
    outcome = _learning_text(event.get("outcome")).lower()
    haystack = f"{reason} {outcome}"
    if event_type == "rolled_back":
        if (
            "overbroad" in haystack
            or "trigger" in haystack
            or "collision" in haystack
        ):
            return "overbroad_trigger"
        return "rollback_after_accept"
    if event_type == "benchmarked" and event.get("passed") is False:
        return "benchmark_regression"
    for pattern, keywords in _LESSON_PATTERN_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return pattern
    return None


def _lesson_recommendation(pattern: str) -> tuple[str, str, dict]:
    if pattern == "overbroad_trigger":
        return (
            "Generated trigger caught adjacent or generic requests.",
            (
                "Require action/domain nouns in triggers and add "
                "catalog-adjacent skip eval prompts."
            ),
            {
                "append_eval_prompts": [{
                    "name": "negative_adjacent_generic_request",
                    "prompt": "summarize this generic note",
                    "expect": "skip",
                }],
            },
        )
    if pattern == "missing_input_contract":
        return (
            "Generated candidate did not preserve required inputs.",
            (
                "Preserve INPUT_CONTRACT in description, step tasks, "
                "rationale, and eval prompts."
            ),
            {"merge_output_contract": {"inputs_required": ["source material"]}},
        )
    if pattern == "missing_output_contract":
        return (
            "Generated candidate did not preserve the expected final artifact.",
            (
                "Preserve OUTPUT_CONTRACT with concrete final sections and "
                "completion evidence."
            ),
            {"merge_output_contract": {"required_sections": ["Result", "Evidence"]}},
        )
    if pattern == "weak_negative_cases":
        return (
            "Generated candidate lacks useful negative-space examples.",
            "Mirror NEGATIVE_CASES into skip eval prompts before activation gates.",
            {
                "append_eval_prompts": [{
                    "name": "negative_nearby_non_target",
                    "prompt": "handle a nearby request that should not use this workflow",
                    "expect": "skip",
                }],
            },
        )
    if pattern == "bad_eval_prompts":
        return (
            "Eval prompts are not realistic future-user requests.",
            (
                "Rewrite eval prompts as natural user requests with activate "
                "or skip expectations."
            ),
            {
                "append_eval_prompts": [{
                    "name": "negative_self_reference",
                    "prompt": "explain how this skill works",
                    "expect": "skip",
                }],
            },
        )
    if pattern == "benchmark_regression":
        return (
            "A candidate revision regressed against the benchmark baseline.",
            "Turn the losing benchmark case into a fixed eval prompt before retrying.",
            {
                "append_eval_prompts": [{
                    "name": "negative_regression_replay",
                    "prompt": "replay the benchmark regression case",
                    "expect": "skip",
                }],
            },
        )
    return (
        "Accepted skill was rolled back after promotion.",
        "Treat the rolled-back proposal as negative evidence before generating siblings.",
        {},
    )


def _creator_feedback_pattern_for_event(event: dict) -> str | None:
    event_type = str(event.get("event_type") or "")
    source = _learning_text(event.get("source"), max_chars=80)
    explicit_stage = _learning_text(
        event.get("target_stage") or event.get("stage"),
        max_chars=80,
    )
    reason = _learning_text(event.get("reason")).lower()
    outcome = _learning_text(event.get("outcome")).lower()
    review_signal = _learning_text(event.get("review_signal"), max_chars=80)
    lessons = " ".join(_learning_lessons(event.get("lessons"))).lower()
    haystack = f"{reason} {outcome} {lessons}"
    if source == "history_failure_path":
        return "history_failure_path"
    if explicit_stage in _CREATOR_FEEDBACK_STAGES and not any(
        keyword in haystack
        for keyword in (
            "wrong pattern",
            "should use fan-out",
            "should use fan out",
            "preferred pattern",
            "existing skill",
            "skill overlap",
            "catalog context",
            "duplicate sibling",
            "not what the user meant",
            "intent boundary",
            "missing intent",
            "wrong task",
            "gate missed",
            "eval corpus",
            "overbroad",
            "trigger",
            "input contract",
            "output contract",
            "negative case",
            "eval prompt",
            "regression",
            "rollback",
        )
    ):
        return "stage_feedback"
    if (
        "wrong pattern" in haystack
        or "should use fan-out" in haystack
        or "should use fan out" in haystack
        or "preferred pattern" in haystack
    ):
        return "wrong_pattern"
    if (
        "existing skill" in haystack
        or "skill overlap" in haystack
        or "catalog context" in haystack
        or "duplicate sibling" in haystack
    ):
        return "missed_existing_skill"
    if "review" in haystack and (
        "summary" in haystack
        or "could not judge" in haystack
        or "hard to judge" in haystack
    ):
        return "review_summary_gap"
    if (
        "not what the user meant" in haystack
        or "intent boundary" in haystack
        or "missing intent" in haystack
        or "wrong task" in haystack
    ):
        return "missing_input_contract"
    if (
        event_type == "benchmarked"
        and event.get("passed") is False
        or "gate missed" in haystack
        or "eval corpus" in haystack
    ):
        return "benchmark_regression"
    if review_signal == "reject_reason":
        return "reject_reason_feedback"
    return _lesson_pattern_for_event(event)


def _creator_feedback_stage_for_pattern(pattern: str) -> str:
    return _FEEDBACK_PATTERN_TO_STAGE.get(pattern, "slot_generator")


def _creator_feedback_stage_for_event(event: dict, pattern: str) -> str:
    explicit = _learning_text(
        event.get("target_stage") or event.get("stage"),
        max_chars=80,
    )
    if explicit in _CREATOR_FEEDBACK_STAGES:
        return explicit
    return _creator_feedback_stage_for_pattern(pattern)


def _creator_feedback_stage_recommendation(stage: str) -> tuple[str, str]:
    if stage == "intent_brief":
        return (
            "Intent understanding failed before generation.",
            (
                "Clarify TASK_CLASS, INPUT_CONTRACT, OUTPUT_CONTRACT, and "
                "boundary cases before selecting a pattern."
            ),
        )
    if stage == "pattern_picker":
        return (
            "Pattern selection likely chose the wrong workflow shape.",
            (
                "Use this as preferred pattern evidence before fill-slots; "
                "compare sequential, fan-out, and gated modes explicitly."
            ),
        )
    if stage == "context_builder":
        return (
            "Creator missed relevant catalog or history context.",
            (
                "Include nearby existing skills and overlap evidence before "
                "pattern selection and slot generation."
            ),
        )
    if stage == "gate_calibration":
        return (
            "A gate or benchmark failed to catch a meaningful regression early enough.",
            (
                "Turn the failed benchmark or missed gate case into fixed eval "
                "corpus coverage before retrying."
            ),
        )
    if stage == "review_ux":
        return (
            "Human review did not provide enough decision context.",
            (
                "Show task class, trigger boundary, negative cases, gate "
                "evidence, and summary before raw SKILL.md."
            ),
        )
    return (
        "Generated slots need stronger capability boundaries.",
        (
            "Revise triggers, input/output contracts, negative cases, eval "
            "prompts, and rationale before assembly."
        ),
    )


def _creator_feedback_stage_hint(stage: str) -> str:
    if stage == "intent_brief":
        return "Feed into clarify_intent / intent brief before pattern selection."
    if stage == "pattern_picker":
        return "Feed into pattern picker as advisory preferred pattern evidence."
    if stage == "context_builder":
        return "Feed into context builder so nearby skills and history are visible."
    if stage == "gate_calibration":
        return "Feed into gate calibration and benchmark replay, not direct skill edits."
    if stage == "review_ux":
        return "Feed into proposal review summary and decision UI."
    return "Feed into fill-slots and slot critic as generation-quality guidance."


def _feedback_patterns_sorted(patterns: set[str]) -> list[str]:
    order = {pattern: index for index, pattern in enumerate(_CREATOR_FEEDBACK_PATTERN_ORDER)}
    return sorted(patterns, key=lambda pattern: (order.get(pattern, 999), pattern))


def _creator_feedback_next_run_controls(bucket: dict) -> dict:
    stage = str(bucket["stage"])
    patterns = set(bucket["source_patterns"])
    reasons = " ".join(str(reason).lower() for reason in bucket["reasons"])
    controls: dict[str, object] = {}
    if stage == "intent_brief":
        controls["require_intent_brief"] = True
        controls["require_task_class"] = True
        controls["require_input_contract"] = True
        controls["require_output_contract"] = True
    elif stage == "pattern_picker":
        preferred_patterns = sorted(bucket["preferred_patterns"])
        selected_patterns = sorted(bucket["selected_patterns"])
        if preferred_patterns:
            controls["preferred_pattern"] = preferred_patterns[0]
        if selected_patterns:
            controls["avoid_patterns"] = selected_patterns
        controls["compare_patterns_explicitly"] = True
    elif stage == "context_builder":
        controls["include_nearby_existing_skills"] = True
        controls["require_overlap_rationale"] = True
        if "history_failure_path" in patterns or "failed step" in reasons:
            controls["require_tool_preconditions"] = True
        if "without fallback" in reasons or "had no fallback" in reasons:
            controls["require_fallback_plan"] = True
    elif stage == "gate_calibration":
        controls["replay_benchmark_cases"] = True
        controls["strengthen_eval_corpus"] = True
    elif stage == "review_ux":
        controls["show_review_summary_first"] = True
        controls["include_gate_evidence_summary"] = True
    else:
        if "missing_input_contract" in patterns or "input contract" in reasons:
            controls["require_input_contract"] = True
        if "missing_output_contract" in patterns or "output contract" in reasons:
            controls["require_output_contract"] = True
        if "weak_negative_cases" in patterns or "negative case" in reasons:
            controls["require_negative_cases"] = True
        if "overbroad_trigger" in patterns or "trigger" in reasons:
            controls["require_narrow_triggers"] = True
            controls["require_negative_eval_prompts"] = True
    return controls


def creator_feedback_cards(home: Path, *, max_events: int = 200, limit: int = 8) -> dict:
    """Return workflow-stage feedback cards derived from creator events."""
    events = _read_creator_learning_events(home, max_events=max_events)
    buckets: dict[tuple[str, str], dict] = {}
    for event in events:
        pattern = _creator_feedback_pattern_for_event(event)
        if pattern is None:
            continue
        stage = _creator_feedback_stage_for_event(event, pattern)
        task_class = _learning_text(event.get("task_class"), max_chars=120)
        key = (stage, task_class)
        bucket = buckets.setdefault(
            key,
            {
                "stage": stage,
                "task_class": task_class,
                "source_patterns": set(),
                "sources": set(),
                "proposal_ids": set(),
                "selected_patterns": set(),
                "preferred_patterns": set(),
                "gate_names": set(),
                "reasons": [],
                "evidence_count": 0,
            },
        )
        bucket["evidence_count"] += 1
        bucket["source_patterns"].add(pattern)
        source = _learning_text(event.get("source"), max_chars=80)
        bucket["sources"].add(source or str(event.get("event_type") or ""))
        reason = _learning_text(event.get("reason"))
        if reason and reason not in bucket["reasons"]:
            bucket["reasons"].append(reason)
        selected_pattern = _learning_text(event.get("selected_pattern"), max_chars=80)
        if selected_pattern:
            bucket["selected_patterns"].add(selected_pattern)
        preferred_pattern = _learning_text(event.get("preferred_pattern"), max_chars=80)
        if preferred_pattern:
            bucket["preferred_patterns"].add(preferred_pattern)
        gate_name = _learning_text(event.get("gate_name"), max_chars=80)
        if gate_name:
            bucket["gate_names"].add(gate_name)
        for field in _CREATOR_LEARNING_PROPOSAL_FIELDS:
            value = _learning_text(event.get(field), max_chars=32)
            if is_valid_proposal_id(value):
                bucket["proposal_ids"].add(value)

    cards: list[dict] = []
    for bucket in buckets.values():
        stage = str(bucket["stage"])
        problem, recommendation = _creator_feedback_stage_recommendation(stage)
        evidence_count = int(bucket["evidence_count"])
        confidence = (
            "high"
            if evidence_count >= 3
            else "medium"
            if evidence_count >= 2
            else "low"
        )
        task_class = str(bucket["task_class"] or "global")
        cards.append({
            "feedback_id": f"{stage}:{task_class}:{evidence_count}",
            "stage": stage,
            "applies_to_task_class": task_class,
            "confidence": confidence,
            "evidence_count": evidence_count,
            "source_patterns": _feedback_patterns_sorted(bucket["source_patterns"]),
            "sources": sorted(source for source in bucket["sources"] if source),
            "proposal_ids": sorted(bucket["proposal_ids"]),
            "selected_patterns": sorted(bucket["selected_patterns"]),
            "preferred_patterns": sorted(bucket["preferred_patterns"]),
            "gate_names": sorted(bucket["gate_names"]),
            "problem": problem,
            "recommendation": recommendation,
            "stage_hint": _creator_feedback_stage_hint(stage),
            "next_run_controls": _creator_feedback_next_run_controls(bucket),
            "blocked_actions": [
                "direct_installed_skill_edit",
                "bypass_proposal_gates",
            ],
            "sample_reasons": bucket["reasons"][:3],
        })
    stage_order = {stage: index for index, stage in enumerate(_CREATOR_FEEDBACK_STAGES)}
    cards.sort(
        key=lambda item: (
            stage_order.get(str(item["stage"]), 999),
            -int(item["evidence_count"]),
            str(item["stage"]),
        ),
    )
    return {"status": "ok", "feedback_cards": cards[:limit], "event_count": len(events)}


def _creator_success_pattern_for_event(event: dict) -> str | None:
    event_type = str(event.get("event_type") or "")
    if event_type == "accepted":
        return "accepted_skill"
    if event_type == "benchmarked" and event.get("passed") is True:
        return "benchmark_win"
    return None


def _split_learning_samples(value: object, *, max_items: int = 5) -> list[str]:
    text = _learning_text(value)
    if not text:
        return []
    parts = [
        _learning_text(part, max_chars=120)
        for part in re.split(r"\s*(?:\||,)\s*", text)
    ]
    return [part for part in parts if part][:max_items]


def _creator_success_next_run_controls(bucket: dict) -> dict:
    controls: dict[str, object] = {}
    selected_patterns = sorted(bucket["selected_patterns"])
    if selected_patterns:
        controls["prefer_successful_pattern"] = selected_patterns[0]
    if bucket["trigger_samples"]:
        controls["reuse_successful_trigger_style"] = True
    if bucket["skill_chains"]:
        controls["reuse_successful_skill_chain"] = True
    if bucket["passed_gate_names"]:
        controls["preserve_successful_gate_coverage"] = True
    return controls


def creator_success_pattern_cards(
    home: Path,
    *,
    max_events: int = 200,
    limit: int = 8,
) -> dict:
    """Return reusable positive recipes from accepted and winning events."""
    events = _read_creator_learning_events(home, max_events=max_events)
    buckets: dict[tuple[str, str], dict] = {}
    for event in events:
        pattern = _creator_success_pattern_for_event(event)
        if pattern is None:
            continue
        task_class = _learning_text(event.get("task_class"), max_chars=120)
        selected_pattern = _learning_text(event.get("selected_pattern"), max_chars=80)
        key = (task_class, selected_pattern)
        bucket = buckets.setdefault(
            key,
            {
                "task_class": task_class,
                "source_patterns": set(),
                "sources": set(),
                "proposal_ids": set(),
                "skill_names": set(),
                "selected_patterns": set(),
                "trigger_samples": set(),
                "skill_chains": set(),
                "passed_gate_names": set(),
                "reasons": [],
                "evidence_count": 0,
            },
        )
        bucket["evidence_count"] += 1
        bucket["source_patterns"].add(pattern)
        bucket["sources"].add(str(event.get("event_type") or ""))
        skill_name = _learning_text(event.get("skill_name"), max_chars=80)
        if skill_name:
            bucket["skill_names"].add(skill_name)
        if selected_pattern:
            bucket["selected_patterns"].add(selected_pattern)
        for sample in _split_learning_samples(event.get("trigger_samples")):
            bucket["trigger_samples"].add(sample)
        skill_chain = _learning_text(event.get("skill_chain"), max_chars=240)
        if skill_chain:
            bucket["skill_chains"].add(skill_chain)
        for gate_name in _split_learning_samples(event.get("passed_gate_names")):
            bucket["passed_gate_names"].add(gate_name)
        reason = _learning_text(event.get("reason"))
        if reason and reason not in bucket["reasons"]:
            bucket["reasons"].append(reason)
        for field in _CREATOR_LEARNING_PROPOSAL_FIELDS:
            value = _learning_text(event.get(field), max_chars=32)
            if is_valid_proposal_id(value):
                bucket["proposal_ids"].add(value)

    cards: list[dict] = []
    for bucket in buckets.values():
        evidence_count = int(bucket["evidence_count"])
        confidence = (
            "high"
            if evidence_count >= 3
            else "medium"
            if evidence_count >= 2
            else "low"
        )
        task_class = str(bucket["task_class"] or "global")
        source_patterns = _feedback_patterns_sorted(bucket["source_patterns"])
        cards.append({
            "success_id": f"{task_class}:{evidence_count}",
            "applies_to_task_class": task_class,
            "confidence": confidence,
            "evidence_count": evidence_count,
            "source_patterns": source_patterns,
            "sources": sorted(source for source in bucket["sources"] if source),
            "proposal_ids": sorted(bucket["proposal_ids"]),
            "skill_names": sorted(bucket["skill_names"]),
            "selected_patterns": sorted(bucket["selected_patterns"]),
            "trigger_samples": sorted(bucket["trigger_samples"]),
            "skill_chains": sorted(bucket["skill_chains"]),
            "passed_gate_names": sorted(bucket["passed_gate_names"]),
            "positive_signal": (
                "Accepted or benchmark-winning meta-skill pattern matched "
                "this task class."
            ),
            "recommendation": (
                "Reuse this as a positive recipe only when the current "
                "TASK_CLASS and contracts match."
            ),
            "prompt_hint": (
                "Apply as a positive recipe: preserve the successful trigger "
                "style, workflow shape, skill chain, and gate coverage when "
                "they fit the current request."
            ),
            "next_run_controls": _creator_success_next_run_controls(bucket),
            "sample_reasons": bucket["reasons"][:3],
        })
    cards.sort(
        key=lambda item: (
            -int(item["evidence_count"]),
            str(item["applies_to_task_class"]),
            str(item["selected_patterns"]),
        ),
    )
    return {
        "status": "ok",
        "success_pattern_cards": cards[:limit],
        "event_count": len(events),
    }


def creator_lesson_cards(home: Path, *, max_events: int = 200, limit: int = 8) -> dict:
    """Return ranked lesson cards derived from sanitized creator events."""
    events = _read_creator_learning_events(home, max_events=max_events)
    buckets: dict[tuple[str, str, str], dict] = {}
    for event in events:
        pattern = _lesson_pattern_for_event(event)
        if pattern is None:
            continue
        skill_name = _learning_text(event.get("skill_name"), max_chars=80)
        reason = _learning_text(event.get("reason"))
        key = (pattern, skill_name, reason)
        bucket = buckets.setdefault(
            key,
            {
                "pattern": pattern,
                "skill_name": skill_name,
                "reason": reason,
                "sources": set(),
                "proposal_ids": set(),
                "evidence_count": 0,
            },
        )
        bucket["evidence_count"] += 1
        bucket["sources"].add(str(event.get("event_type") or ""))
        for field in _CREATOR_LEARNING_PROPOSAL_FIELDS:
            value = _learning_text(event.get(field), max_chars=32)
            if is_valid_proposal_id(value):
                bucket["proposal_ids"].add(value)

    cards: list[dict] = []
    for bucket in buckets.values():
        problem, recommendation, patch_hint = _lesson_recommendation(bucket["pattern"])
        evidence_count = int(bucket["evidence_count"])
        confidence = (
            "high"
            if evidence_count >= 3
            else "medium"
            if evidence_count >= 2
            else "low"
        )
        identity = bucket["skill_name"] or bucket["reason"] or "global"
        cards.append({
            "lesson_id": f"{bucket['pattern']}:{identity}:{evidence_count}",
            "pattern": bucket["pattern"],
            "confidence": confidence,
            "evidence_count": evidence_count,
            "sources": sorted(source for source in bucket["sources"] if source),
            "proposal_ids": sorted(bucket["proposal_ids"]),
            "skill_name": bucket["skill_name"],
            "problem": problem,
            "recommendation": recommendation,
            "prompt_hint": recommendation,
            "patch_hint": patch_hint,
        })
    cards.sort(
        key=lambda item: (
            -int(item["evidence_count"]),
            item["pattern"],
            item["skill_name"],
        ),
    )
    return {"status": "ok", "lesson_cards": cards[:limit], "event_count": len(events)}


def creator_learning_summary(home: Path, *, max_events: int = 200) -> dict:
    """Return compact advisory memory from sanitized creator learning events."""
    events = _read_creator_learning_events(home, max_events=max_events)
    cards_result = creator_lesson_cards(home, max_events=max_events)
    feedback_result = creator_feedback_cards(home, max_events=max_events)
    success_result = creator_success_pattern_cards(home, max_events=max_events)
    feedback_by_stage: dict[str, list[dict]] = {}
    for card in feedback_result["feedback_cards"]:
        stage = str(card.get("stage") or "slot_generator")
        feedback_by_stage.setdefault(stage, []).append(card)
    counts = {event_type: 0 for event_type in sorted(_CREATOR_LEARNING_EVENT_TYPES)}
    lessons: list[str] = []
    rollback_reasons: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type in counts:
            counts[event_type] += 1
        if event_type == "rolled_back":
            skill_name = _learning_text(event.get("skill_name"), max_chars=80)
            reason = _learning_text(event.get("reason"))
            if skill_name and reason:
                text = f"{skill_name}: {reason}"
            else:
                text = reason or skill_name
            if text and text not in rollback_reasons:
                rollback_reasons.append(text)
        for lesson in _learning_lessons(event.get("lessons")):
            if lesson not in lessons:
                lessons.append(lesson)
        if len(lessons) >= 8 and len(rollback_reasons) >= 5:
            continue

    count_text = " ".join(
        f"{event_type}={counts[event_type]}" for event_type in sorted(counts)
    )
    parts = [f"Creator learning counts: {count_text}."]
    if rollback_reasons:
        parts.append(
            "Rollback warnings: "
            + "; ".join(rollback_reasons[:5])
            + "."
        )
    if lessons:
        parts.append("Lessons: " + "; ".join(lessons[:8]) + ".")
    if len(parts) == 1:
        parts.append("No lessons or rollback warnings recorded yet.")
    return {
        "status": "ok",
        "path": str(creator_learning_events_path(home)),
        "event_count": len(events),
        "counts": counts,
        "lesson_cards": cards_result["lesson_cards"],
        "feedback_cards": feedback_result["feedback_cards"],
        "feedback_by_stage": feedback_by_stage,
        "success_pattern_cards": success_result["success_pattern_cards"],
        "summary": " ".join(parts),
    }


def _sanitize_creator_learning_event(event: object) -> dict | None:
    if not isinstance(event, dict):
        return None
    event_type = _learning_text(event.get("event_type") or event.get("type"), max_chars=80)
    if event_type not in _CREATOR_LEARNING_EVENT_TYPES:
        return None
    out: dict[str, object] = {
        "event_type": event_type,
        "recorded_at_ms": int(time.time() * 1000),
    }
    for field in _CREATOR_LEARNING_PROPOSAL_FIELDS:
        value = _learning_text(event.get(field), max_chars=32)
        if is_valid_proposal_id(value):
            out[field] = value
    benchmark_id = _learning_text(event.get("benchmark_id"), max_chars=32)
    if is_valid_proposal_id(benchmark_id):
        out["benchmark_id"] = benchmark_id
    skill_name = _learning_text(event.get("skill_name"), max_chars=80)
    if skill_name and SKILL_NAME_PATTERN.fullmatch(skill_name):
        out["skill_name"] = skill_name
    for field in _CREATOR_LEARNING_STRING_FIELDS:
        value = _learning_text(event.get(field))
        if value:
            out[field] = value
    if isinstance(event.get("passed"), bool):
        out["passed"] = bool(event["passed"])
    lessons = _learning_lessons(event.get("lessons"))
    if lessons:
        out["lessons"] = lessons
    return out


def record_creator_learning_event(home: Path, event: object) -> dict:
    """Append a compact sanitized creator learning event to JSONL memory."""
    sanitized = _sanitize_creator_learning_event(event)
    if sanitized is None:
        return {"status": "refused", "reason": "invalid_event_type"}
    path = creator_learning_events_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
    return {
        "status": "ok",
        "path": str(path),
        "event": sanitized,
    }


def _safe_record_creator_learning_event(home: Path, event: object) -> None:
    try:
        record_creator_learning_event(home, event)
    except OSError:
        return


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
    "audit_proposal_drift",
    "auto_enable_audit_from_gates",
    "auto_propose_settings_path",
    "creator_feedback_cards",
    "creator_learning_dir",
    "creator_learning_events_path",
    "creator_lesson_cards",
    "creator_learning_summary",
    "creator_success_pattern_cards",
    "disable_auto_enabled_skill",
    "is_valid_proposal_id",
    "list_auto_enabled_skills",
    "list_proposals",
    "patch_proposal",
    "pending_count",
    "proposals_dir",
    "read_auto_propose_settings",
    "record_creator_history_failure_feedback",
    "record_creator_learning_event",
    "refresh_proposal_gates",
    "reject_proposal",
    "rollback_dir",
    "rollback_skill",
    "show_proposal",
    "skills_dir",
    "write_auto_propose_settings",
    "write_proposal",
]
