"""Unit tests for opensquilla.skills.proposals_lib."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from opensquilla.skills import proposals_lib

SAMPLE_SKILL_MD = """---
name: synth-test-pipeline
description: "Sample synthetic pipeline for proposals_lib tests"
kind: meta
meta_priority: 50
triggers:
  - "synth test trigger"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: a
      skill: summarize
      with:
        task: "{{ inputs.user_message }}"
---
"""

GATES_PASSING = {
    "G1": {"passed": True}, "G2": {"passed": True},
}
SMOKE_PASSING = {
    "G3": {"passed": True}, "G4": {"passed": True},
}


def _seed_proposal(home: Path, *, eligible: bool = True) -> str:
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING if eligible else {"G1": {"passed": False}},
        SMOKE_PASSING,
    )
    assert result["status"] == "ok"
    return result["proposal_id"]


def _skill_md(name: str, trigger: str, description: str = "Synthetic proposal") -> str:
    return f"""---
name: {name}
description: "{description}"
kind: meta
meta_priority: 50
triggers:
  - "{trigger}"
composition:
  steps:
    - id: a
      skill: summarize
      with:
        task: "{{{{ inputs.user_message }}}}"
---
"""


def test_is_valid_proposal_id() -> None:
    assert proposals_lib.is_valid_proposal_id("abcd1234") is True
    assert proposals_lib.is_valid_proposal_id("ABCD1234") is False  # uppercase rejected
    assert proposals_lib.is_valid_proposal_id("abcd123") is False   # too short
    assert proposals_lib.is_valid_proposal_id("../etc/passwd") is False
    assert proposals_lib.is_valid_proposal_id("") is False
    assert proposals_lib.is_valid_proposal_id(None) is False


def test_write_then_list_then_pending_count(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid1 = _seed_proposal(home)
    pid2 = _seed_proposal(home)
    rows = proposals_lib.list_proposals(home)["proposals"]
    assert sorted(r["proposal_id"] for r in rows) == sorted([pid1, pid2])
    assert all(r["auto_enable_eligible"] for r in rows)
    assert proposals_lib.pending_count(home) == {"count": 2}


def test_audit_proposal_drift_reports_stale_collision_and_rollback_heavy(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    parent = proposals_lib.write_proposal(
        home,
        _skill_md("audit-alpha", "audit shared trigger", "Audit alpha proposal"),
        GATES_PASSING,
        SMOKE_PASSING,
    )
    assert parent["status"] == "ok"
    sibling = proposals_lib.write_proposal(
        home,
        _skill_md("audit-beta", "audit shared trigger", "Audit beta proposal"),
        GATES_PASSING,
        SMOKE_PASSING,
    )
    assert sibling["status"] == "ok"
    patched = proposals_lib.patch_proposal(
        home,
        parent["proposal_id"],
        {"set_description": "Audit alpha revised proposal."},
    )
    assert patched["status"] == "ok"

    managed = home / "skills" / "audit-alpha"
    managed.mkdir(parents=True)
    (managed / "SKILL.md").write_text(
        _skill_md("audit-alpha", "audit active trigger"),
        encoding="utf-8",
    )
    for idx in range(2):
        archived = home / "rollback" / "audit-alpha" / f"archive{idx}"
        archived.mkdir(parents=True)
        (archived / "SKILL.md").write_text(
            _skill_md("audit-alpha", f"audit archived trigger {idx}"),
            encoding="utf-8",
        )

    audit = proposals_lib.audit_proposal_drift(home)

    assert audit["status"] == "ok"
    issue_types = {issue["type"] for issue in audit["issues"]}
    assert "stale_proposal" in issue_types
    assert "pending_trigger_collision" in issue_types
    assert "rollback_heavy_skill" in issue_types
    stale = [
        issue for issue in audit["issues"]
        if issue["type"] == "stale_proposal"
    ][0]
    assert stale["proposal_id"] == patched["proposal_id"]
    collision = [
        issue for issue in audit["issues"]
        if issue["type"] == "pending_trigger_collision"
    ][0]
    assert collision["trigger"] == "audit shared trigger"
    assert sorted(collision["proposal_ids"]) == sorted([
        parent["proposal_id"],
        sibling["proposal_id"],
        patched["proposal_id"],
    ])
    rollback_heavy = [
        issue for issue in audit["issues"]
        if issue["type"] == "rollback_heavy_skill"
    ][0]
    assert rollback_heavy["skill_name"] == "audit-alpha"
    assert rollback_heavy["rollback_count"] == 2


def test_record_creator_learning_event_writes_sanitized_jsonl(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"

    accepted = proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "accepted",
            "proposal_id": "abcd1234",
            "skill_name": "learn-skill",
            "outcome": "accepted after review\nwith newline",
            "lessons": ["keep trigger narrow\nplease", "x" * 400],
            "secret": "must not be persisted",
        },
    )
    benchmarked = proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "benchmarked",
            "baseline_proposal_id": "abcd1234",
            "candidate_proposal_id": "deadbeef",
            "benchmark_id": "01234567",
            "passed": True,
            "lessons": ["candidate won on groundedness"],
        },
    )
    rolled_back = proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "rolled_back",
            "skill_name": "learn-skill",
            "restored_proposal_id": "deadbeef",
            "reason": "overbroad trigger",
        },
    )
    invalid = proposals_lib.record_creator_learning_event(
        home,
        {"event_type": "unknown", "proposal_id": "abcd1234"},
    )

    assert accepted["status"] == "ok"
    assert benchmarked["status"] == "ok"
    assert rolled_back["status"] == "ok"
    assert invalid == {"status": "refused", "reason": "invalid_event_type"}
    path = home / "creator-learning" / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["event_type"] for row in rows] == [
        "accepted",
        "benchmarked",
        "rolled_back",
    ]
    assert rows[0]["outcome"] == "accepted after review with newline"
    assert rows[0]["lessons"][0] == "keep trigger narrow please"
    assert len(rows[0]["lessons"][1]) == 300
    assert "secret" not in rows[0]
    assert isinstance(rows[0]["recorded_at_ms"], int)
    assert rows[1]["passed"] is True
    assert rows[2]["restored_proposal_id"] == "deadbeef"


def test_creator_learning_summary_compacts_recent_events(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"

    proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "accepted",
            "proposal_id": "abcd1234",
            "skill_name": "learn-skill",
            "outcome": "accepted",
            "lessons": ["keep trigger narrow", "do not expose secrets"],
            "secret": "must not leak",
        },
    )
    proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "benchmarked",
            "baseline_proposal_id": "abcd1234",
            "candidate_proposal_id": "deadbeef",
            "benchmark_id": "01234567",
            "passed": True,
            "lessons": ["candidate won on groundedness"],
        },
    )
    proposals_lib.record_creator_learning_event(
        home,
        {
            "event_type": "rolled_back",
            "skill_name": "learn-skill",
            "restored_proposal_id": "deadbeef",
            "reason": "overbroad trigger",
            "lessons": ["avoid broad activation"],
        },
    )

    summary = proposals_lib.creator_learning_summary(home)

    assert summary["status"] == "ok"
    assert summary["event_count"] == 3
    text = summary["summary"]
    assert "accepted=1" in text
    assert "benchmarked=1" in text
    assert "rolled_back=1" in text
    assert "learn-skill: overbroad trigger" in text
    assert "keep trigger narrow" in text
    assert "candidate won on groundedness" in text
    assert "must not leak" not in text


def test_pending_count_on_empty_home(tmp_path: Path) -> None:
    home = tmp_path / "empty"
    assert proposals_lib.pending_count(home) == {"count": 0}


def test_list_proposals_surfaces_provenance(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    # Patch gates.json with provenance
    gates_path = home / "proposals" / pid / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["provenance"] = {
        "triggered_by": "auto_cron",
        "chain_hash": "deadbeefcafebabe",
    }
    gates_path.write_text(json.dumps(gates))
    rows = proposals_lib.list_proposals(home)["proposals"]
    assert rows[0]["triggered_by"] == "auto_cron"
    assert rows[0]["chain_hash"] == "deadbeefcafebabe"


def test_list_proposals_surfaces_auto_enable_decision(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    gates_path = home / "proposals" / pid / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["auto_enable"] = {
        "status": "skipped",
        "reason": "risk_too_high",
        "risk_level": "high",
        "max_risk": "low",
    }
    gates_path.write_text(json.dumps(gates))
    rows = proposals_lib.list_proposals(home)["proposals"]
    assert rows[0]["auto_enable"] == {
        "status": "skipped",
        "reason": "risk_too_high",
        "risk_level": "high",
        "max_risk": "low",
        "validation_profile": "unknown",
        "skills": [],
        "tools": [],
        "reasons": [],
    }


def test_show_returns_payload(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    out = proposals_lib.show_proposal(home, pid)
    assert out["status"] == "ok"
    assert out["proposal_id"] == pid
    assert "synth-test-pipeline" in out["skill_md"]
    assert out["gates"]["auto_enable_eligible"] is True


def test_full_gated_requires_runtime_e2e_result(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "REASONS:\n"
                "- candidate has stricter gates\n"
                "REGRESSIONS:\n"
                "- none\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
    )

    assert result["status"] == "ok"
    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["runtime_e2e"]["required"] is True
    assert shown["gates"]["runtime_e2e"]["passed"] is False
    assert shown["gates"]["runtime_e2e"]["reason"] == "missing_runtime_e2e_result"

    accepted = proposals_lib.accept_proposal(home, result["proposal_id"])
    assert accepted["status"] == "refused"
    assert "gates not all passed" in accepted["reason"]


def test_full_gated_runtime_e2e_blocks_baseline_winner(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "REASONS:\n"
                "- candidate has stricter gates\n"
                "REGRESSIONS:\n"
                "- none\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result={
            "status": "ok",
            "passed": False,
            "winner": "baseline",
            "cases": [
                {
                    "prompt": "please use synth test trigger",
                    "winner": "baseline",
                    "regression": "meta answer missed the requested summary",
                },
            ],
        },
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["runtime_e2e"]["passed"] is False
    assert shown["gates"]["runtime_e2e"]["winner"] == "baseline"


def test_full_gated_acceptance_blocks_single_model_winner_even_when_runtime_passes(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: single-model\n"
                "REASONS:\n"
                "- baseline SKILL.md reads cleaner\n"
                "REGRESSIONS:\n"
                "- none\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result={
            "status": "ok",
            "passed": True,
            "winner": "meta",
            "cases": [
                {
                    "prompt": "please use synth test trigger",
                    "winner": "meta",
                    "regression": "",
                },
            ],
        },
        collision_result="PASS: no trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["acceptance_compare"]["passed"] is False
    assert shown["gates"]["acceptance_compare"]["winner"] == "single-model"
    assert shown["gates"]["runtime_e2e"]["passed"] is True


def test_full_gated_acceptance_blocks_low_weighted_quality_score(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "QUALITY_SCORE: 0.71\n"
                "REASONS:\n"
                "- candidate works but lacks output contracts\n"
                "REGRESSIONS:\n"
                "- weaker final artifact quality\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result={
            "status": "ok",
            "passed": True,
            "winner": "meta",
            "cases": [{"winner": "meta", "regression": ""}],
        },
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["acceptance_compare"]["passed"] is False
    assert shown["gates"]["acceptance_compare"]["quality_score"] == 0.71
    assert "quality score below 0.80" in shown["gates"]["acceptance_compare"]["diagnostics"]


def test_full_gated_blocks_collision_and_high_risk_results(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "QUALITY_SCORE: 0.93\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result={
            "status": "ok",
            "passed": True,
            "winner": "meta",
            "cases": [{"winner": "meta", "regression": ""}],
        },
        collision_result="REVISE_NEEDED: trigger overlaps with summarize",
        risk_result="RISK: high\nCAPABILITIES:\n- shell",
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["collision_check"]["passed"] is False
    assert shown["gates"]["risk_classify"]["passed"] is False


def test_full_gated_runtime_e2e_allows_meta_winner_when_acceptance_passes(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "REASONS:\n"
                "- candidate has stricter gates\n"
                "REGRESSIONS:\n"
                "- none\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result={
            "status": "ok",
            "passed": True,
            "winner": "meta",
            "cases": [
                {
                    "prompt": "please use synth test trigger",
                    "winner": "meta",
                    "regression": "",
                },
            ],
        },
        collision_result="PASS: no trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result={"required": True, "passed": True, "reason": "ok"},
        activation_result={"required": True, "passed": True, "reason": "ok"},
    )

    assert result["auto_enable_eligible"] is True
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["acceptance_compare"]["passed"] is True
    assert shown["gates"]["runtime_e2e"]["passed"] is True
    assert shown["gates"]["generation_quality"]["passed"] is True
    assert shown["gates"]["activation_eval"]["passed"] is True


def test_write_proposal_persists_generation_and_activation_gates(
    tmp_path: Path,
) -> None:
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
        activation_result=json.dumps({
            "required": True,
            "passed": True,
            "reason": "ok",
            "true_positive_rate": 1.0,
            "false_positive_rate": 0.0,
        }),
    )

    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["generation_quality"]["passed"] is True
    assert shown["gates"]["activation_eval"]["true_positive_rate"] == 1.0


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


def test_write_proposal_rejects_non_boolean_creator_gate_passed_values(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result=json.dumps({
            "required": True,
            "passed": "false",
            "reason": "ok",
        }),
        activation_result={
            "required": True,
            "passed": "true",
            "reason": "ok",
        },
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["generation_quality"]["passed"] is False
    assert shown["gates"]["generation_quality"]["reason"] == "invalid_gate_passed_type"
    assert shown["gates"]["generation_quality"]["passed_raw"] == "false"
    assert shown["gates"]["activation_eval"]["passed"] is False
    assert shown["gates"]["activation_eval"]["reason"] == "invalid_gate_passed_type"
    assert shown["gates"]["activation_eval"]["passed_raw"] == "true"


def test_write_proposal_rejects_truthy_string_lint_and_smoke_passed_values(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        {"G1": {"passed": "false"}, "G2": {"passed": True}},
        {"G3": {"passed": "false"}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result={"required": True, "passed": True, "reason": "ok"},
        activation_result={"required": True, "passed": True, "reason": "ok"},
    )

    assert result["auto_enable_eligible"] is False
    accepted = proposals_lib.accept_proposal(home, result["proposal_id"])
    assert accepted["status"] == "refused"


def test_write_proposal_forces_creator_gate_required_in_required_mode(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="PERSISTED_PROPOSAL",
        collision_result="PASS",
        risk_result="RISK: low",
        generation_quality_result={"required": False, "passed": True, "reason": "ok"},
        activation_result={"required": False, "passed": True, "reason": "ok"},
    )

    assert result["auto_enable_eligible"] is True
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["generation_quality"]["required"] is True
    assert shown["gates"]["activation_eval"]["required"] is True


def test_full_gated_runtime_e2e_rejects_non_boolean_passed_value(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={
            "raw": (
                "WINNER: orchestrated\n"
                "REASONS:\n"
                "- candidate has stricter gates\n"
                "REGRESSIONS:\n"
                "- none\n"
                "REQUIRED_IMPROVEMENTS:\n"
                "- none\n"
            ),
        },
        runtime_e2e_result=json.dumps({
            "status": "ok",
            "passed": "false",
            "winner": "meta",
            "cases": [
                {
                    "prompt": "please use synth test trigger",
                    "winner": "meta",
                    "regression": "",
                },
            ],
        }),
        collision_result="PASS: no trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result={"required": True, "passed": True, "reason": "ok"},
        activation_result={"required": True, "passed": True, "reason": "ok"},
    )

    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["runtime_e2e"]["passed"] is False
    assert shown["gates"]["runtime_e2e"]["reason"] == "invalid_runtime_e2e_passed_type"


def test_write_proposal_requires_generation_and_activation_in_creator_modes(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    for creator_mode in ("PERSISTED_PROPOSAL", "FULL_GATED"):
        result = proposals_lib.write_proposal(
            home,
            SAMPLE_SKILL_MD,
            GATES_PASSING,
            SMOKE_PASSING,
            creator_mode=creator_mode,
            collision_result="PASS",
            risk_result="RISK: low",
        )

        assert result["auto_enable_eligible"] is False
        shown = proposals_lib.show_proposal(home, result["proposal_id"])
        assert shown["gates"]["generation_quality"]["required"] is True
        assert shown["gates"]["generation_quality"]["passed"] is False
        assert (
            shown["gates"]["generation_quality"]["reason"]
            == "missing_generation_quality_result"
        )
        assert shown["gates"]["activation_eval"]["required"] is True
        assert shown["gates"]["activation_eval"]["passed"] is False
        assert shown["gates"]["activation_eval"]["reason"] == "missing_activation_result"


def test_show_rejects_invalid_id(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = proposals_lib.show_proposal(home, "../etc")
    assert out["status"] == "error"


def test_show_missing_proposal(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = proposals_lib.show_proposal(home, "deadbeef")
    assert out["status"] == "error"
    assert "not found" in out["reason"]


def test_accept_promotes_to_managed_skills(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    out = proposals_lib.accept_proposal(home, pid)
    assert out["status"] == "ok"
    assert out["name"] == "synth-test-pipeline"
    moved = home / "skills" / "synth-test-pipeline" / "SKILL.md"
    assert moved.is_file()
    # Source dir disappears
    assert not (home / "proposals" / pid).exists()


def test_list_and_disable_auto_enabled_skill(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    gates_path = home / "proposals" / pid / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["auto_enable"] = {
        "status": "enabled",
        "proposal_id": pid,
        "risk_level": "low",
        "max_risk": "low",
        "triggered_by": "manual",
        "enabled_at_ms": 123,
    }
    gates_path.write_text(json.dumps(gates))
    accepted = proposals_lib.accept_proposal(home, pid)
    assert accepted["status"] == "ok"

    rows = proposals_lib.list_auto_enabled_skills(home)["skills"]
    assert rows == [{
        "name": "synth-test-pipeline",
        "proposal_id": pid,
        "risk_level": "low",
        "max_risk": "low",
        "triggered_by": "manual",
        "enabled_at_ms": 123,
        "validation_profile": "unknown",
        "skills": [],
        "tools": [],
        "reasons": [],
    }]

    out = proposals_lib.disable_auto_enabled_skill(home, "synth-test-pipeline")
    assert out["status"] == "ok"
    assert out["proposal_id"] == pid
    assert not (home / "skills" / "synth-test-pipeline").exists()
    assert (home / "proposals" / pid / "SKILL.md").is_file()
    disabled_gates = json.loads((home / "proposals" / pid / "gates.json").read_text())
    assert disabled_gates["auto_enable"]["status"] == "disabled"
    assert disabled_gates["auto_enable"]["previous_status"] == "enabled"


def test_disable_auto_enabled_skill_refuses_manual_skill(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    accepted = proposals_lib.accept_proposal(home, pid)
    assert accepted["status"] == "ok"
    out = proposals_lib.disable_auto_enabled_skill(home, "synth-test-pipeline")
    assert out["status"] == "refused"
    assert "not auto-enabled" in out["reason"]


def test_accept_refuses_when_gates_fail_without_force(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home, eligible=False)
    out = proposals_lib.accept_proposal(home, pid)
    assert out["status"] == "refused"
    out2 = proposals_lib.accept_proposal(home, pid, force=True)
    assert out2["status"] == "ok"


def test_patch_proposal_creates_revision_and_stales_gates(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)
    parent_skill_path = home / "proposals" / parent_id / "SKILL.md"
    original_parent_skill_md = parent_skill_path.read_text(encoding="utf-8")
    patch = {
        "set_description": "Refined synthetic pipeline for proposal patch tests.",
        "add_triggers": ["refined synth trigger"],
        "remove_triggers": ["synth test trigger"],
        "merge_output_contract": {"required_sections": ["Summary", "Evidence"]},
        "append_eval_prompts": [{
            "name": "refined-positive",
            "prompt": "please use refined synth trigger",
            "rubric": ["Summary"],
        }],
        "merge_metadata_opensquilla": {"requires_tools": ["read_file"]},
        "append_body": (
            "## Revision Notes\n\n"
            "- Tightened trigger and output contract.\n"
        ),
        "owner": "unit-test",
    }

    result = proposals_lib.patch_proposal(home, parent_id, patch)

    assert result["status"] == "ok"
    child_id = result["proposal_id"]
    assert child_id != parent_id
    assert parent_skill_path.read_text(encoding="utf-8") == original_parent_skill_md

    child = proposals_lib.show_proposal(home, child_id)
    assert child["status"] == "ok"
    frontmatter_text = child["skill_md"].split("---", 2)[1]
    child_frontmatter = yaml.safe_load(frontmatter_text)
    assert child_frontmatter["description"] == patch["set_description"]
    assert child_frontmatter["triggers"] == ["refined synth trigger"]
    assert child_frontmatter["output_contract"] == {
        "required_sections": ["Summary", "Evidence"],
    }
    assert child_frontmatter["eval_prompts"] == patch["append_eval_prompts"]
    assert child_frontmatter["metadata"]["opensquilla"] == {
        "requires_tools": ["read_file"],
    }
    assert "## Revision Notes" in child["skill_md"]
    assert "- Tightened trigger and output contract." in child["skill_md"]

    child_gates = child["gates"]
    assert child_gates["creator_mode"] == "PATCH_PROPOSAL"
    assert child_gates["revision"] == {
        "parent_proposal_id": parent_id,
        "root_proposal_id": parent_id,
        "revision": 2,
        "owner": "unit-test",
        "applied_operations": [
            "set_description",
            "add_triggers",
            "remove_triggers",
            "merge_metadata_opensquilla",
            "merge_output_contract",
            "append_eval_prompts",
            "append_body",
        ],
    }
    assert child_gates["auto_enable_eligible"] is False
    for gate_name in (
        "smoke",
        "collision_check",
        "risk_classify",
        "generation_quality",
        "activation_eval",
        "runtime_e2e",
    ):
        gate = child_gates[gate_name]
        assert gate["passed"] is False
        assert gate["reason"] == "stale_after_patch"
        assert gate["stale"] is True

    accepted = proposals_lib.accept_proposal(home, child_id)
    assert accepted["status"] == "refused"
    assert "stale patch revision gates" in accepted["reason"]

    forced = proposals_lib.accept_proposal(home, child_id, force=True)
    assert forced["status"] == "refused"
    assert "stale patch revision gates" in forced["reason"]


def test_patch_proposal_refuses_unsupported_operations(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)

    result = proposals_lib.patch_proposal(home, parent_id, {"set_name": "evil"})

    assert result == {
        "status": "refused",
        "reason": "unsupported_patch_operations:set_name",
    }
    assert proposals_lib.pending_count(home) == {"count": 1}


def test_accept_refuses_patch_revision_with_stale_gates_even_if_marked_eligible(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)
    patched = proposals_lib.patch_proposal(
        home,
        parent_id,
        {"add_triggers": ["patched trigger"], "owner": "unit-test"},
    )
    child_id = patched["proposal_id"]
    gates_path = home / "proposals" / child_id / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["auto_enable_eligible"] = True
    gates_path.write_text(json.dumps(gates))

    accepted = proposals_lib.accept_proposal(home, child_id, force=True)

    assert accepted["status"] == "refused"
    assert "stale patch revision gates" in accepted["reason"]
    assert accepted["gates"]["smoke"]["stale"] is True
    assert accepted["gates"]["acceptance_compare"]["stale"] is True


def test_refresh_proposal_gates_clears_patch_stale_gates(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        creator_mode="FULL_GATED",
        acceptance_result={"winner": "orchestrated", "required_improvements": "none"},
        runtime_e2e_result={"passed": True, "winner": "meta", "cases": []},
        collision_result="PASS: no trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )
    child = proposals_lib.patch_proposal(
        home,
        parent["proposal_id"],
        {"set_description": "Refreshed patch proposal gate test."},
    )
    child_id = child["proposal_id"]
    child_gates_path = home / "proposals" / child_id / "gates.json"
    child_gates = json.loads(child_gates_path.read_text())
    child_gates["lint"] = GATES_PASSING
    child_gates_path.write_text(json.dumps(child_gates))

    refreshed = proposals_lib.refresh_proposal_gates(
        home,
        child_id,
        smoke_result=SMOKE_PASSING,
        collision_result="PASS: refreshed collision check",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        acceptance_result={"winner": "orchestrated", "required_improvements": "none"},
        runtime_e2e_result={"passed": True, "winner": "meta", "cases": []},
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )

    assert refreshed["status"] == "ok"
    assert refreshed["auto_enable_eligible"] is True
    assert sorted(refreshed["refreshed"]) == [
        "acceptance_compare",
        "activation_eval",
        "collision_check",
        "generation_quality",
        "risk_classify",
        "runtime_e2e",
        "smoke",
    ]
    shown = proposals_lib.show_proposal(home, child_id)
    assert all(
        not (isinstance(value, dict) and value.get("stale") is True)
        for value in shown["gates"].values()
    )
    accepted = proposals_lib.accept_proposal(home, child_id)
    assert accepted["status"] == "ok"


def test_refresh_proposal_gates_preserves_failed_gate_block(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)
    child = proposals_lib.patch_proposal(
        home,
        parent_id,
        {"set_description": "Failed refreshed gate should block accept."},
    )

    refreshed = proposals_lib.refresh_proposal_gates(
        home,
        child["proposal_id"],
        smoke_result=SMOKE_PASSING,
        collision_result="REVISE_NEEDED: trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )

    assert refreshed["status"] == "ok"
    assert refreshed["auto_enable_eligible"] is False
    out = proposals_lib.accept_proposal(home, child["proposal_id"])
    assert out["status"] == "refused"
    assert "gates not all passed" in out["reason"]
    assert out["gates"]["collision_check"]["passed"] is False
    assert out["gates"]["collision_check"].get("stale") is not True


def test_patch_proposal_refuses_malformed_parent_revision(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)
    gates_path = home / "proposals" / parent_id / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["revision"] = {"revision": [1], "root_proposal_id": parent_id}
    gates_path.write_text(json.dumps(gates))

    result = proposals_lib.patch_proposal(
        home,
        parent_id,
        {"add_triggers": ["patched trigger"]},
    )

    assert result == {"status": "refused", "reason": "invalid_parent_revision"}
    assert proposals_lib.pending_count(home) == {"count": 1}


def test_patch_proposal_records_latest_child_and_refuses_stale_expected_revision(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    parent_id = _seed_proposal(home)

    first = proposals_lib.patch_proposal(
        home,
        parent_id,
        {"add_triggers": ["first child"]},
        expected_parent_revision=1,
    )

    assert first["status"] == "ok"
    parent_gates = json.loads(
        (home / "proposals" / parent_id / "gates.json").read_text(),
    )
    assert parent_gates["latest_child_proposal_id"] == first["proposal_id"]
    assert parent_gates["latest_child_revision"] == 2

    second = proposals_lib.patch_proposal(
        home,
        parent_id,
        {"add_triggers": ["conflicting child"]},
        expected_parent_revision=1,
    )

    assert second["status"] == "refused"
    assert second["reason"] == "revision_conflict"
    assert second["latest_child_proposal_id"] == first["proposal_id"]


def test_benchmark_proposals_reuses_revision_eval_prompts_and_records_report(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    baseline_id = _seed_proposal(home)
    patched = proposals_lib.patch_proposal(
        home,
        baseline_id,
        {
            "append_eval_prompts": [{
                "name": "refined-positive",
                "prompt": "please use refined synth trigger",
                "rubric": ["Summary"],
            }],
            "owner": "unit-test",
        },
    )
    candidate_id = patched["proposal_id"]

    result = proposals_lib.benchmark_proposals(
        home,
        baseline_id,
        candidate_id,
        comparison_result={
            "status": "ok",
            "passed": True,
            "winner": "candidate",
            "quality_score": 0.91,
            "cases": [{
                "prompt": "please use refined synth trigger",
                "winner": "candidate",
                "regression": "",
            }],
        },
    )

    assert result["status"] == "ok"
    assert result["auto_enable_eligible"] is False
    benchmark_id = result["benchmark_id"]
    report_path = home / "proposal-benchmarks" / benchmark_id / "benchmark.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["creator_mode"] == "BENCHMARK"
    assert report["baseline_proposal_id"] == baseline_id
    assert report["candidate_proposal_id"] == candidate_id
    assert report["eval_prompts"] == [{
        "name": "refined-positive",
        "prompt": "please use refined synth trigger",
        "rubric": ["Summary"],
    }]
    assert report["gates"]["benchmark_compare"]["passed"] is True
    assert report["gates"]["benchmark_compare"]["winner"] == "candidate"
    assert report["gates"]["benchmark_compare"]["quality_score"] == 0.91
    assert (home / "proposals" / baseline_id / "SKILL.md").is_file()
    assert (home / "proposals" / candidate_id / "SKILL.md").is_file()


def test_benchmark_proposals_marks_missing_eval_prompts_unavailable(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    baseline_id = _seed_proposal(home)
    candidate_id = _seed_proposal(home)

    result = proposals_lib.benchmark_proposals(home, baseline_id, candidate_id)

    assert result == {
        "status": "unavailable",
        "reason": "benchmark_prompts_missing",
        "baseline_proposal_id": baseline_id,
        "candidate_proposal_id": candidate_id,
    }
    assert not (home / "proposal-benchmarks").exists()


def test_accept_refuses_stale_required_creator_proposal_missing_new_gates(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    gates_path = home / "proposals" / pid / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["auto_enable_eligible"] = True
    gates["collision_check"] = {"required": True, "passed": True, "reason": "ok"}
    gates["risk_classify"] = {"required": True, "passed": True, "reason": "ok"}
    gates.pop("generation_quality", None)
    gates.pop("activation_eval", None)
    gates_path.write_text(json.dumps(gates))

    out = proposals_lib.accept_proposal(home, pid)

    assert out["status"] == "refused"
    assert "gates not all passed" in out["reason"]
    assert out["gates"]["generation_quality"]["reason"] == (
        "missing_generation_quality_result"
    )
    assert out["gates"]["activation_eval"]["reason"] == "missing_activation_result"


def test_accept_requires_boolean_true_auto_enable_eligible(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    gates_path = home / "proposals" / pid / "gates.json"
    gates = json.loads(gates_path.read_text())
    gates["auto_enable_eligible"] = "false"
    gates_path.write_text(json.dumps(gates))

    out = proposals_lib.accept_proposal(home, pid)

    assert out["status"] == "refused"
    assert "gates not all passed" in out["reason"]


def test_accept_refuses_when_target_skill_exists(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid1 = _seed_proposal(home)
    proposals_lib.accept_proposal(home, pid1)
    pid2 = _seed_proposal(home)
    out = proposals_lib.accept_proposal(home, pid2)
    assert out["status"] == "refused"
    assert "already exists" in out["reason"]


def test_accept_replace_records_supersession_and_rollback_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    first_id = _seed_proposal(home)
    first = proposals_lib.accept_proposal(home, first_id)
    assert first["status"] == "ok"

    replacement_skill_md = SAMPLE_SKILL_MD.replace(
        "Sample synthetic pipeline for proposals_lib tests",
        "Replacement synthetic pipeline for proposals_lib tests",
    )
    replacement = proposals_lib.write_proposal(
        home,
        replacement_skill_md,
        GATES_PASSING,
        SMOKE_PASSING,
    )
    replacement_id = replacement["proposal_id"]

    out = proposals_lib.accept_proposal(
        home,
        replacement_id,
        replace=True,
        owner="unit-test",
    )

    assert out["status"] == "ok"
    assert out["name"] == "synth-test-pipeline"
    assert out["replaced"] is True
    rollback_target = out["rollback_target"]
    assert rollback_target["proposal_id"] == first_id
    assert rollback_target["skill_name"] == "synth-test-pipeline"
    archived = Path(rollback_target["path"])
    assert archived.is_dir()
    assert "Sample synthetic pipeline" in (archived / "SKILL.md").read_text(
        encoding="utf-8",
    )
    managed_gates = json.loads(
        (
            home
            / "skills"
            / "synth-test-pipeline"
            / "gates.json"
        ).read_text(encoding="utf-8"),
    )
    lifecycle = managed_gates["lifecycle"]
    assert lifecycle["status"] == "active"
    assert lifecycle["accepted_proposal_id"] == replacement_id
    assert lifecycle["supersedes"]["proposal_id"] == first_id
    assert lifecycle["rollback_target"]["proposal_id"] == first_id
    assert lifecycle["owner"] == "unit-test"
    assert (home / "proposals" / replacement_id).exists() is False


def test_accept_replace_records_deprecation_metadata(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    first_id = _seed_proposal(home)
    first = proposals_lib.accept_proposal(home, first_id)
    assert first["status"] == "ok"
    replacement = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD.replace(
            "Sample synthetic pipeline for proposals_lib tests",
            "Replacement with migration notes for proposals_lib tests",
        ),
        GATES_PASSING,
        SMOKE_PASSING,
    )

    out = proposals_lib.accept_proposal(
        home,
        replacement["proposal_id"],
        replace=True,
        owner="migration-owner",
        deprecates=["synth-test-pipeline", "legacy-synth-helper"],
        migration_notes="Use synth-test-pipeline v2 triggers and keep rollback path.",
    )

    assert out["status"] == "ok"
    managed_gates = json.loads(
        (home / "skills" / "synth-test-pipeline" / "gates.json").read_text(),
    )
    lifecycle = managed_gates["lifecycle"]
    assert lifecycle["deprecates"] == [
        "synth-test-pipeline",
        "legacy-synth-helper",
    ]
    assert lifecycle["migration_notes"] == (
        "Use synth-test-pipeline v2 triggers and keep rollback path."
    )
    archived_gates = json.loads(
        (Path(out["rollback_target"]["path"]) / "gates.json").read_text(),
    )
    assert archived_gates["lifecycle"]["status"] == "deprecated"
    assert archived_gates["lifecycle"]["deprecated_by"] == {
        "skill_name": "synth-test-pipeline",
        "proposal_id": replacement["proposal_id"],
        "owner": "migration-owner",
    }


def test_write_accept_and_patch_preserve_bundle_files(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    skill_md = (
        SAMPLE_SKILL_MD
        + "\nUses helper `scripts/render.py` and reference `references/eval.json`.\n"
    )
    result = proposals_lib.write_proposal(
        home,
        skill_md,
        GATES_PASSING,
        SMOKE_PASSING,
        bundle_files={
            "scripts/render.py": "print('render')\n",
            "references/eval.json": '{"cases": []}\n',
        },
    )
    assert result["status"] == "ok"
    proposal_id = result["proposal_id"]

    shown = proposals_lib.show_proposal(home, proposal_id)
    assert shown["bundle"]["kind"] == "skill_bundle"
    assert shown["bundle"]["entrypoint"] == "SKILL.md"
    assert shown["bundle"]["files"] == [
        "references/eval.json",
        "scripts/render.py",
    ]
    assert shown["bundle_files"]["scripts/render.py"] == "print('render')\n"

    patched = proposals_lib.patch_proposal(
        home,
        proposal_id,
        {"set_description": "Bundle revision keeps attached resources."},
    )
    assert patched["status"] == "ok"
    child_id = patched["proposal_id"]
    assert (
        home / "proposals" / child_id / "scripts" / "render.py"
    ).read_text(encoding="utf-8") == "print('render')\n"

    child_gates = json.loads(
        (home / "proposals" / child_id / "gates.json").read_text(encoding="utf-8"),
    )
    assert child_gates["bundle"]["files"] == [
        "references/eval.json",
        "scripts/render.py",
    ]

    accepted = proposals_lib.accept_proposal(home, proposal_id)
    assert accepted["status"] == "ok"
    managed = home / "skills" / "synth-test-pipeline"
    assert (managed / "scripts" / "render.py").read_text(encoding="utf-8") == (
        "print('render')\n"
    )


def test_write_proposal_refuses_unsafe_bundle_paths(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"

    out = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        bundle_files={"../escape.py": "bad"},
    )

    assert out["status"] == "refused"
    assert out["reason"] == "invalid_bundle_path:../escape.py"
    assert proposals_lib.pending_count(home) == {"count": 0}


def test_write_proposal_flags_missing_referenced_bundle_file(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    skill_md = SAMPLE_SKILL_MD + "\nUses helper `scripts/missing.py`.\n"

    result = proposals_lib.write_proposal(
        home,
        skill_md,
        GATES_PASSING,
        SMOKE_PASSING,
    )

    assert result["status"] == "ok"
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    gate = shown["gates"]["bundle_validation"]
    assert gate["passed"] is False
    assert gate["missing_references"] == ["scripts/missing.py"]
    assert shown["gates"]["auto_enable_eligible"] is False


def test_write_proposal_flags_unreferenced_script_bundle_file(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"

    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_PASSING,
        bundle_files={"scripts/render.py": "print('unused')\n"},
    )

    assert result["status"] == "ok"
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    gate = shown["gates"]["bundle_validation"]
    assert gate["passed"] is False
    assert gate["unreferenced_scripts"] == ["scripts/render.py"]
    assert shown["gates"]["auto_enable_eligible"] is False


def test_rollback_skill_restores_recorded_target_and_archives_current(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    first_id = _seed_proposal(home)
    accepted = proposals_lib.accept_proposal(home, first_id)
    assert accepted["status"] == "ok"
    replacement_skill_md = SAMPLE_SKILL_MD.replace(
        "Sample synthetic pipeline for proposals_lib tests",
        "Replacement synthetic pipeline for proposals_lib tests",
    )
    replacement = proposals_lib.write_proposal(
        home,
        replacement_skill_md,
        GATES_PASSING,
        SMOKE_PASSING,
    )
    replacement_id = replacement["proposal_id"]
    replaced = proposals_lib.accept_proposal(home, replacement_id, replace=True)
    assert replaced["status"] == "ok"

    out = proposals_lib.rollback_skill(home, "synth-test-pipeline")

    assert out["status"] == "ok"
    assert out["name"] == "synth-test-pipeline"
    assert out["restored_proposal_id"] == first_id
    managed = home / "skills" / "synth-test-pipeline"
    assert "Sample synthetic pipeline" in (managed / "SKILL.md").read_text(
        encoding="utf-8",
    )
    gates = json.loads((managed / "gates.json").read_text(encoding="utf-8"))
    assert gates["lifecycle"]["status"] == "rolled_back_active"
    assert gates["lifecycle"]["rolled_back_from"]["proposal_id"] == replacement_id
    archived_current = Path(out["archived_current"]["path"])
    assert archived_current.is_dir()
    assert "Replacement synthetic pipeline" in (
        archived_current / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_rollback_skill_refuses_without_target(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    accepted = proposals_lib.accept_proposal(home, pid)
    assert accepted["status"] == "ok"

    out = proposals_lib.rollback_skill(home, "synth-test-pipeline")

    assert out["status"] == "refused"
    assert "rollback target" in out["reason"]


def test_reject_removes_directory(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    pid = _seed_proposal(home)
    out = proposals_lib.reject_proposal(home, pid)
    assert out["status"] == "ok"
    assert not (home / "proposals" / pid).exists()


def test_reject_rejects_invalid_id(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = proposals_lib.reject_proposal(home, "../etc/passwd")
    assert out["status"] == "error"


def test_reject_missing_proposal(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = proposals_lib.reject_proposal(home, "deadbeef")
    assert out["status"] == "error"


def test_auto_propose_settings_round_trip(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    assert proposals_lib.read_auto_propose_settings(home) == {}
    proposals_lib.write_auto_propose_settings(
        home, {
            "enabled": True,
            "on_dream_complete": False,
            "auto_enable": True,
            "auto_enable_max_risk": "medium",
        },
    )
    out = proposals_lib.read_auto_propose_settings(home)
    assert out == {
        "enabled": True,
        "on_dream_complete": False,
        "auto_enable": True,
        "auto_enable_max_risk": "medium",
    }


def test_auto_propose_settings_drops_unknown_and_bad_types(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    # Unknown keys dropped at write time
    proposals_lib.write_auto_propose_settings(
        home, {
            "enabled": True,
            "auto_enable_max_risk": "dangerous",
            "bogus_key": True,
        },  # type: ignore[arg-type]
    )
    assert proposals_lib.read_auto_propose_settings(home) == {"enabled": True}
    # Bad-shape file → empty dict (no exception)
    proposals_lib.auto_propose_settings_path(home).write_text("[1,2,3]")
    assert proposals_lib.read_auto_propose_settings(home) == {}


def test_write_atomic_under_concurrent_writers(tmp_path: Path) -> None:
    """Writing N proposals should produce N distinct directories — the
    atomic-rename guarantees uniqueness even if proposal_ids collide."""
    home = tmp_path / ".opensquilla"
    ids = []
    for _ in range(5):
        out = proposals_lib.write_proposal(home, SAMPLE_SKILL_MD, GATES_PASSING, SMOKE_PASSING)
        assert out["status"] == "ok"
        ids.append(out["proposal_id"])
    assert len(set(ids)) == 5  # all distinct
    assert proposals_lib.pending_count(home)["count"] == 5


# ── D1: degraded smoke must not yield auto_enable_eligible ──

SMOKE_DEGRADED = {
    "G3": {"passed": True, "degraded": True},
    "G4": {"passed": True, "degraded": True},
    "degraded": True,
}


def test_degraded_smoke_blocks_auto_enable_eligible(tmp_path: Path) -> None:
    """D1: when the smoke runner has no fixture-generator LLM, it falls
    back to a deterministic stub and flags the result ``degraded: True``.
    G3/G4 still report ``passed: True`` because the deterministic
    fixtures self-match by construction, but the candidate has not
    been validated against a real model. The eligibility evaluator
    must observe ``degraded`` and refuse to auto-enable so an
    unattended creator pipeline cannot promote a never-validated
    proposal.

    The proposal itself still lands (``status == "ok"``) so an operator
    can inspect it; only ``auto_enable_eligible`` flips to False."""
    home = tmp_path / ".opensquilla"
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        SMOKE_DEGRADED,
    )
    assert result["status"] == "ok"
    assert result["auto_enable_eligible"] is False
    shown = proposals_lib.show_proposal(home, result["proposal_id"])
    assert shown["gates"]["auto_enable_eligible"] is False
    # Cross-check: the smoke record on disk retains ``degraded`` so an
    # auditor can grep for it without re-deriving from the eligibility
    # flag.
    assert shown["gates"]["smoke"].get("degraded") is True


def test_non_degraded_smoke_still_yields_auto_enable_eligible(
    tmp_path: Path,
) -> None:
    """D1 negative control: a smoke result that does NOT carry a
    ``degraded`` flag (or carries it as False) must still be eligible
    when all other gates pass. Without this regression the D1 change
    could silently mark every proposal ineligible."""
    home = tmp_path / ".opensquilla"
    smoke_clean = {
        "G3": {"passed": True, "degraded": False},
        "G4": {"passed": True, "degraded": False},
        "degraded": False,
    }
    result = proposals_lib.write_proposal(
        home,
        SAMPLE_SKILL_MD,
        GATES_PASSING,
        smoke_clean,
    )
    assert result["status"] == "ok"
    assert result["auto_enable_eligible"] is True
