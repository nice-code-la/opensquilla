"""Tests for skill-creator-proposals bundled skill (write/list/accept/reject)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_BUNDLED = REPO / "src" / "opensquilla" / "skills" / "bundled"
PROPOSALS = _BUNDLED / "skill-creator-proposals" / "scripts" / "proposals.py"


def _run(action: str, *args, home: Path, **kwargs) -> dict:
    cmd = [sys.executable, str(PROPOSALS), "--action", action,
           "--home", str(home), *args]
    for k, v in kwargs.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


SAMPLE_SKILL_MD = """---
name: synth-test-pipeline
description: "Sample synthetic pipeline for proposals tests"
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
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""


def test_write_proposal_creates_directory(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    assert out["status"] == "ok"
    proposal_id = out["proposal_id"]
    proposal_dir = home / "proposals" / proposal_id
    assert (proposal_dir / "SKILL.md").exists()
    assert (proposal_dir / "gates.json").exists()
    gates = json.loads((proposal_dir / "gates.json").read_text())
    assert gates["auto_enable_eligible"] is True


def test_write_proposal_accepts_bundle_files_json(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
        bundle_files_json=json.dumps({
            "scripts/render.py": "print('ok')\n",
            "evals/smoke.json": '{"prompt": "run synth"}\n',
        }),
    )

    proposal_dir = home / "proposals" / out["proposal_id"]
    assert (proposal_dir / "scripts" / "render.py").read_text(encoding="utf-8") == (
        "print('ok')\n"
    )
    manifest = json.loads((proposal_dir / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "skill_bundle"
    assert manifest["files"] == ["evals/smoke.json", "scripts/render.py"]


def test_write_proposal_marks_ineligible_on_g3_fail(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": False, "reason": "classifier missed"},
                                  "G4": {"passed": True}}),
    )
    gates = json.loads((home / "proposals" / out["proposal_id"] / "gates.json").read_text())
    assert gates["auto_enable_eligible"] is False


def test_write_proposal_marks_full_gated_ineligible_on_compare_loss(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
        creator_mode="FULL_GATED",
        acceptance_result=(
            "WINNER: orchestrated\n"
            "REASONS:\n"
            "- generated skill is clearer\n"
            "REGRESSIONS:\n"
            "- none\n"
            "REQUIRED_IMPROVEMENTS:\n"
            "- none\n"
        ),
        runtime_e2e_result=json.dumps({
            "status": "ok",
            "passed": False,
            "winner": "baseline",
            "cases": [{"prompt": "please use synth test trigger", "winner": "baseline"}],
        }),
    )

    gates = json.loads((home / "proposals" / out["proposal_id"] / "gates.json").read_text())
    assert out["auto_enable_eligible"] is False
    assert gates["runtime_e2e"]["passed"] is False
    assert gates["runtime_e2e"]["winner"] == "baseline"


def test_accept_rejects_path_traversal_proposal_id(tmp_path: Path) -> None:
    """I1 regression: cmd_accept must reject proposal IDs that aren't 8 hex chars."""
    home = tmp_path / ".opensquilla"
    home.mkdir()
    (home / "proposals").mkdir()

    for bad_id in ["../../etc", "../sibling", "abcd1234567890", "ABCDEF12", ""]:
        out = _run("accept", home=home, proposal_id=bad_id)
        assert out["status"] == "error", f"should reject {bad_id!r}, got: {out}"
        assert "invalid proposal_id" in out["reason"]


def test_proposals_cli_works_without_explicit_home(monkeypatch, tmp_path: Path) -> None:
    """N17: --home is optional; defaults to default_opensquilla_home()."""
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    # Run with NO --home; only --action and required action-specific args
    proc = subprocess.run(
        [sys.executable, str(PROPOSALS), "--action", "list"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"argparse should accept missing --home: {proc.stderr}"
    out = json.loads(proc.stdout)
    assert "proposals" in out  # empty list ok; just shouldn't crash


def test_patch_action_creates_revision(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    parent_id = parent["proposal_id"]

    out = _run(
        "patch",
        home=home,
        proposal_id=parent_id,
        expected_parent_revision=1,
        patch_json=json.dumps({
            "add_triggers": ["patched trigger"],
            "owner": "script-test",
        }),
    )

    assert out["status"] == "ok"
    child_id = out["proposal_id"]
    assert child_id != parent_id
    assert out["parent_proposal_id"] == parent_id
    gates = json.loads((home / "proposals" / child_id / "gates.json").read_text())
    assert gates["revision"]["parent_proposal_id"] == parent_id
    assert gates["revision"]["owner"] == "script-test"
    parent_gates = json.loads((home / "proposals" / parent_id / "gates.json").read_text())
    assert parent_gates["latest_child_proposal_id"] == child_id
    assert parent_gates["latest_child_revision"] == 2

    forced_accept = _run(
        "accept",
        "--force",
        home=home,
        proposal_id=child_id,
    )
    assert forced_accept["status"] == "refused"
    assert "stale patch revision gates" in forced_accept["reason"]
    assert (home / "proposals" / child_id / "SKILL.md").is_file()


def test_refresh_action_updates_patch_revision_gates(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    parent = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    patched = _run(
        "patch",
        home=home,
        proposal_id=parent["proposal_id"],
        patch_json=json.dumps({"add_triggers": ["refresh script trigger"]}),
    )

    out = _run(
        "refresh",
        home=home,
        proposal_id=patched["proposal_id"],
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
        collision_result="PASS: refreshed collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result=json.dumps({"passed": True}),
        activation_result=json.dumps({"passed": True}),
    )

    assert out["status"] == "ok"
    assert "smoke" in out["refreshed"]
    assert "collision_check" in out["refreshed"]
    gates = json.loads(
        (home / "proposals" / patched["proposal_id"] / "gates.json").read_text(),
    )
    assert gates["collision_check"]["passed"] is True
    assert gates["collision_check"].get("stale") is not True


def test_benchmark_action_records_report(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    baseline = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    baseline_id = baseline["proposal_id"]
    candidate = _run(
        "patch",
        home=home,
        proposal_id=baseline_id,
        patch_json=json.dumps({
            "append_eval_prompts": [{
                "name": "script-benchmark",
                "prompt": "please use script benchmark child",
                "rubric": ["Summary"],
            }],
        }),
    )
    candidate_id = candidate["proposal_id"]

    out = _run(
        "benchmark",
        home=home,
        proposal_id=baseline_id,
        candidate_proposal_id=candidate_id,
        comparison_result=json.dumps({
            "passed": True,
            "winner": "candidate",
            "cases": [{
                "prompt": "please use script benchmark child",
                "winner": "candidate",
                "regression": "",
            }],
        }),
    )

    assert out["status"] == "ok"
    assert out["baseline_proposal_id"] == baseline_id
    assert out["candidate_proposal_id"] == candidate_id
    report = json.loads(
        (
            home
            / "proposal-benchmarks"
            / out["benchmark_id"]
            / "benchmark.json"
        ).read_text(encoding="utf-8"),
    )
    assert report["creator_mode"] == "BENCHMARK"
    assert report["eval_prompts"][0]["name"] == "script-benchmark"
    assert report["gates"]["benchmark_compare"]["passed"] is True


def test_accept_replace_and_rollback_actions_round_trip(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    first = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    first_id = first["proposal_id"]
    accepted = _run("accept", home=home, proposal_id=first_id)
    assert accepted["status"] == "ok"

    replacement_md = SAMPLE_SKILL_MD.replace(
        "Sample synthetic pipeline for proposals tests",
        "Replacement synthetic pipeline for proposals tests",
    )
    replacement = _run(
        "write_proposal", home=home,
        skill_md_inline=replacement_md,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    replacement_id = replacement["proposal_id"]

    replaced = _run(
        "accept",
        "--replace",
        home=home,
        proposal_id=replacement_id,
        owner="script-test",
        deprecates="synth-test-pipeline,legacy-script-synth",
        migration_notes="Script migration keeps rollback metadata.",
    )

    assert replaced["status"] == "ok"
    assert replaced["replaced"] is True
    assert replaced["rollback_target"]["proposal_id"] == first_id
    gates = json.loads((home / "skills" / "synth-test-pipeline" / "gates.json").read_text())
    assert gates["lifecycle"]["deprecates"] == [
        "synth-test-pipeline",
        "legacy-script-synth",
    ]
    assert gates["lifecycle"]["migration_notes"] == (
        "Script migration keeps rollback metadata."
    )

    rolled_back = _run(
        "rollback",
        home=home,
        skill_name="synth-test-pipeline",
    )

    assert rolled_back["status"] == "ok"
    assert rolled_back["restored_proposal_id"] == first_id
    restored = home / "skills" / "synth-test-pipeline" / "SKILL.md"
    assert "Sample synthetic pipeline" in restored.read_text(encoding="utf-8")
