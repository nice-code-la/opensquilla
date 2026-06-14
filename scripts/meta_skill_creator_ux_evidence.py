#!/usr/bin/env python3
"""Build reproducible UX evidence for meta-skill-creator proposals.

The script reads an OpenSquilla state directory after a deterministic or live
creator run and emits only structural evidence. It does not call an LLM and it
does not print secrets.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from opensquilla.skills import proposals_lib

CLAIM_NEEDLES = {
    "persist_status_visible_in_final_response": (
        "src/opensquilla/skills/bundled/meta-skill-creator/SKILL.md",
        ("Saved proposal status", "outputs.persist"),
    ),
    "proposal_health_visible_in_webui": (
        "src/opensquilla/gateway/static/js/views/skills.js",
        ("exec.proposals.audit", "Proposal Health"),
    ),
    "dry_run_sample_visible_in_webui": (
        "src/opensquilla/gateway/static/js/views/skills.js",
        ("Dry run sample", "_renderProposalDryRun"),
    ),
    "lifecycle_commands_visible_in_webui": (
        "src/opensquilla/gateway/static/js/views/skills.js",
        (
            "opensquilla skills meta proposals refresh",
            "opensquilla skills meta proposals patch",
            "opensquilla skills meta proposals benchmark",
        ),
    ),
    "repair_hints_visible_in_webui": (
        "src/opensquilla/gateway/static/js/views/skills.js",
        ("Recommended repairs", "_renderProposalRepairHints", "repair_hints"),
    ),
    "review_context_visible_in_approvals": (
        "src/opensquilla/gateway/static/js/views/approvals.js",
        ("_approvalContext", "reason", "risk", "source"),
    ),
}

ACTION_NAMES = ("refresh", "patch", "benchmark")
BLOCKING_GATES = (
    "collision_check",
    "risk_classify",
    "acceptance_compare",
    "runtime_e2e",
    "generation_quality",
    "activation_eval",
    "bundle_validation",
)


def build_meta_skill_creator_ux_evidence(
    *,
    home: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Return JSON-ready evidence for user-visible proposal review affordances."""
    home = home.expanduser().resolve()
    root = (repo_root or Path.cwd()).resolve()
    rows = proposals_lib.list_proposals(home).get("proposals", [])
    proposals = [_proposal_evidence(home, row) for row in rows]
    claims = _static_claims(root)
    actionable = [
        proposal for proposal in proposals
        if proposal["gate_blockers"] or proposal["dry_run_sample"]
    ]
    return {
        "ok": True,
        "evidence_level": "state_and_static_ui",
        "home": str(home),
        "summary": {
            "pending_proposals": len(proposals),
            "actionable_proposals": len(actionable),
            "claims_backed_by_static_ui": sum(1 for value in claims.values() if value),
        },
        "claims": claims,
        "proposals": proposals,
        "limitations": [
            (
                "This proves state/API/static-UI affordances, not browser pixel "
                "rendering or human task-time improvement."
            )
        ],
    }


def _static_claims(repo_root: Path) -> dict[str, bool]:
    claims: dict[str, bool] = {}
    for claim, (relative, needles) in CLAIM_NEEDLES.items():
        path = repo_root / relative
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            claims[claim] = False
            continue
        claims[claim] = all(needle in source for needle in needles)
    return claims


def _proposal_evidence(home: Path, row: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(row.get("proposal_id") or "")
    shown = proposals_lib.show_proposal(home, proposal_id)
    skill_md = str(shown.get("skill_md") or "")
    gates = shown.get("gates") if isinstance(shown.get("gates"), dict) else {}
    skill_name, triggers = _skill_identity(skill_md)
    dry_run_sample = triggers[0] if triggers else ""
    return {
        "proposal_id": proposal_id,
        "skill_name": skill_name,
        "triggered_by": row.get("triggered_by", "manual"),
        "auto_enable_eligible": bool(row.get("auto_enable_eligible")),
        "dry_run_sample": dry_run_sample,
        "gate_blockers": _gate_blockers(gates),
        "repair_hints": _repair_hints(gates),
        "next_actions": {
            action: f"opensquilla skills meta proposals {action} {proposal_id}"
            for action in ACTION_NAMES
        },
    }


def _skill_identity(skill_md: str) -> tuple[str, list[str]]:
    try:
        frontmatter = _frontmatter(skill_md)
    except ValueError:
        return ("", [])
    name = str(frontmatter.get("name") or "").strip()
    raw = frontmatter.get("triggers")
    if isinstance(raw, list):
        triggers = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str) and raw.strip():
        triggers = [raw.strip()]
    else:
        triggers = []
    return name, triggers


def _frontmatter(skill_md: str) -> dict[str, Any]:
    match = re.match(r"\A---\s*\n(?P<frontmatter>.*?)\n---", skill_md, re.DOTALL)
    if not match:
        raise ValueError("missing_frontmatter")
    parsed = yaml.safe_load(match.group("frontmatter")) or {}
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter_not_mapping")
    return parsed


def _gate_blockers(gates: dict[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for gate_name in BLOCKING_GATES:
        gate = gates.get(gate_name)
        if not isinstance(gate, dict):
            continue
        if gate.get("required") is False:
            continue
        if gate.get("passed") is True:
            continue
        reason = str(gate.get("reason") or f"{gate_name}_failed")
        blockers.append({
            "gate": gate_name,
            "reason": reason,
            "detail": _gate_detail(gate_name, gate),
        })
    return blockers


def _repair_hints(gates: dict[str, Any]) -> list[dict[str, Any]]:
    hints = gates.get("repair_hints")
    if not isinstance(hints, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in hints:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "gate": str(item.get("gate") or ""),
            "problem": str(item.get("problem") or ""),
            "recommended_action": str(item.get("recommended_action") or ""),
            "patch_operations": [
                str(op) for op in item.get("patch_operations", [])
                if str(op).strip()
            ] if isinstance(item.get("patch_operations"), list) else [],
        })
    return cleaned


def _gate_detail(gate_name: str, gate: dict[str, Any]) -> str:
    if gate_name == "collision_check":
        return _first_collision_detail(str(gate.get("raw") or "")) or str(
            gate.get("raw") or ""
        )
    if gate_name == "acceptance_compare":
        pieces = []
        winner = str(gate.get("winner") or "").strip()
        score = gate.get("quality_score")
        if winner or score is not None:
            pieces.append(
                f"{winner or 'unknown'}"
                + (f" score={score}" if score is not None else "")
            )
        improvements = str(gate.get("required_improvements") or "").strip()
        if improvements:
            pieces.append(improvements)
        diagnostics = gate.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics and not improvements:
            pieces.append("; ".join(str(item) for item in diagnostics))
        return "; ".join(pieces)
    if gate_name == "runtime_e2e":
        winner = str(gate.get("winner") or "").strip()
        diagnostics = gate.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics:
            return f"{winner or 'unknown'}; " + "; ".join(str(item) for item in diagnostics)
        return winner or str(gate.get("raw") or "")
    if gate_name == "risk_classify":
        return str(gate.get("risk_level") or gate.get("raw") or "")
    return str(gate.get("raw") or gate.get("reason") or "")


def _first_collision_detail(raw: str) -> str:
    for line in raw.splitlines():
        stripped = line.strip()
        numbered = re.match(r"^\d+\.\s+(?P<reason>.+)$", stripped)
        if numbered:
            return _clean_markdown_reason(numbered.group("reason"))
        if stripped.startswith("-"):
            return _clean_markdown_reason(stripped[1:].strip())
    return ""


def _clean_markdown_reason(text: str) -> str:
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", text.strip())
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = build_meta_skill_creator_ux_evidence(
        home=args.home,
        repo_root=args.repo_root,
    )
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
