from __future__ import annotations

import importlib.util
from pathlib import Path

from opensquilla.skills import proposals_lib


def _load_script_module():
    path = Path("scripts/meta_skill_creator_ux_evidence.py")
    spec = importlib.util.spec_from_file_location("meta_skill_creator_ux_evidence", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _seed_blocked_proposal(home: Path) -> str:
    skill_md = """---
name: ux-history-recap
description: "Summarize recent operational decision history."
kind: meta
meta_priority: 50
triggers:
  - "recent decision history recap"
  - "operational recap"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: history
      skill: history-explorer
      with:
        query: "{{ inputs.user_message }}"
    - id: summary
      skill: summarize
      with:
        text: "{{ steps.history.output }}"
---
"""
    result = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="FULL_GATED",
        collision_result=(
            "REVISE_NEEDED\n"
            "- operational recap -> existing-recap: overlaps an existing trigger"
        ),
        acceptance_result={
            "passed": False,
            "winner": "single-model",
            "quality_score": 0.53,
            "required_improvements": "Add an Inputs section",
        },
        runtime_e2e_result={
            "passed": True,
            "winner": "meta",
            "baseline_model": "stub-live-model",
            "cases": [{"prompt": "recent decision history recap", "winner": "meta"}],
        },
        risk_result="RISK: low\nNo dangerous tools.",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )
    assert result["status"] == "ok"
    return str(result["proposal_id"])


def test_ux_evidence_summarizes_actionable_proposal_review_state(
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    home = tmp_path / "home"
    proposal_id = _seed_blocked_proposal(home)

    evidence = module.build_meta_skill_creator_ux_evidence(
        home=home,
        repo_root=Path.cwd(),
    )

    assert evidence["ok"] is True
    assert evidence["evidence_level"] == "state_and_static_ui"
    assert evidence["summary"]["pending_proposals"] == 1
    assert evidence["summary"]["actionable_proposals"] == 1

    proposal = evidence["proposals"][0]
    assert proposal["proposal_id"] == proposal_id
    assert proposal["skill_name"] == "ux-history-recap"
    assert proposal["dry_run_sample"] == "recent decision history recap"
    assert proposal["gate_blockers"] == [
        {
            "gate": "collision_check",
            "reason": "collision_check_failed",
            "detail": "operational recap -> existing-recap: overlaps an existing trigger",
        },
        {
            "gate": "acceptance_compare",
            "reason": "acceptance_compare_failed",
            "detail": "single-model score=0.53; Add an Inputs section",
        },
    ]
    assert proposal["next_actions"] == {
        "refresh": f"opensquilla skills meta proposals refresh {proposal_id}",
        "patch": f"opensquilla skills meta proposals patch {proposal_id}",
        "benchmark": f"opensquilla skills meta proposals benchmark {proposal_id}",
    }

    assert evidence["claims"] == {
        "persist_status_visible_in_final_response": True,
        "proposal_health_visible_in_webui": True,
        "dry_run_sample_visible_in_webui": True,
        "lifecycle_commands_visible_in_webui": True,
        "review_context_visible_in_approvals": True,
    }
    assert evidence["limitations"] == [
        (
            "This proves state/API/static-UI affordances, not browser pixel "
            "rendering or human task-time improvement."
        )
    ]


def test_collision_detail_prefers_numbered_revise_reason() -> None:
    module = _load_script_module()

    detail = module._first_collision_detail(
        '**REVISE_NEEDED**\n\n'
        '1. **Generic triggers that steal unrelated intent** – The trigger '
        '"summarize recent history" is overly broad.\n\n'
        '**Recommendation**\n'
        '- "decision log recap"\n'
    )

    assert detail == (
        'Generic triggers that steal unrelated intent - The trigger '
        '"summarize recent history" is overly broad.'
    )
