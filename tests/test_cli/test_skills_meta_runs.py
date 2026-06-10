"""CLI smoke tests for `opensquilla skills meta runs ...`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opensquilla.cli.main import app as cli_app
from opensquilla.persistence.meta_run_writer import open_meta_run_writer
from opensquilla.persistence.migrator import apply_pending
from opensquilla.skills import proposals_lib
from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

MIGRATIONS_DIR = Path(__file__).resolve().parents[1].parent / "migrations"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch):
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    w = open_meta_run_writer(db)

    plan_a = MetaPlan(
        name="alpha-skill", triggers=("t",), priority=10,
        steps=(MetaStep(id="s1", skill="x", kind="agent"),),
    )
    plan_b = MetaPlan(
        name="beta-skill", triggers=("t",), priority=10,
        steps=(MetaStep(id="s1", skill="y", kind="agent"),),
    )
    rid_ok = w.begin_run_sync(
        meta_skill_name="alpha-skill", meta_plan=plan_a,
        triggered_by="soft_meta_invoke", inputs={"user_message": "hi"},
        session_key="sess-1", turn_id="turn-1",
    )
    w.begin_step_sync(
        run_id=rid_ok, step=plan_a.steps[0], effective_skill="x",
        rendered_inputs={"a": 1},
    )
    w.finish_step_sync(
        run_id=rid_ok, step_id="s1", status="ok", output_text="alpha-out",
    )
    w.finish_run_sync(
        run_id=rid_ok, status="ok",
        result=MetaResult(ok=True, final_text="alpha-out"),
    )

    rid_fail = w.begin_run_sync(
        meta_skill_name="beta-skill", meta_plan=plan_b,
        triggered_by="hard_takeover", inputs={},
        session_key=None, turn_id=None,
    )
    w.finish_run_sync(
        run_id=rid_fail, status="failed",
        result=MetaResult(ok=False, error="boom", failed_step_id="s1"),
    )
    w.close()

    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", db)
    return {"db": db, "rid_ok": rid_ok, "rid_fail": rid_fail}


def test_runs_list(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2


def test_runs_list_filter_status(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app,
        ["skills", "meta", "runs", "list", "--status", "failed", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["status"] == "failed"


def test_runs_show(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "show", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["meta_skill_name"] == "alpha-skill"
    assert data["status"] == "ok"
    assert data["summary"]["step_count"] == 1
    assert data["summary"]["usage"]["available"] is False


def test_runs_steps(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "steps", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["step_id"] == "s1"
    assert data[0]["status"] == "ok"


def test_runs_failures(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "failures", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["status"] == "failed"


def test_runs_replay_dry_run(runner: CliRunner, seeded_db) -> None:
    """W8: --dry-run prints DAG in the spec'd format."""
    result = runner.invoke(
        cli_app,
        ["skills", "meta", "runs", "replay", seeded_db["rid_ok"], "--dry-run", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["meta_skill_name"] == "alpha-skill"
    assert data["plan_source"] == "historical_snapshot"
    assert len(data["steps"]) == 1


def test_runs_draft_json(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app, ["skills", "meta", "runs", "draft", seeded_db["rid_ok"], "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["source_run"]["run_id"] == seeded_db["rid_ok"]
    assert data["name"] == "alpha-skill-draft"
    assert data["composition"]["steps"][0]["id"] == "s1"
    assert data["trigger_candidates"]


def test_runs_draft_creator_input_json(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "runs",
            "draft",
            seeded_db["rid_ok"],
            "--creator-input",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["draft"]["source_run"]["run_id"] == seeded_db["rid_ok"]
    assert data["creator_input"]["recommended_mode"] == "PERSISTED_PROPOSAL"
    assert json.loads(data["creator_input"]["draft_seed_json"])["source_kind"] == "meta_run"


def test_runs_draft_creator_input_json_handles_cannot_draft(
    runner: CliRunner,
    seeded_db,
) -> None:
    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "runs",
            "draft",
            seeded_db["rid_fail"],
            "--creator-input",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["draft"]["status"] == "cannot_draft"
    assert data["creator_input"] is None


def test_runs_draft_non_json_handles_cannot_draft(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "draft", seeded_db["rid_fail"]])

    assert result.exit_code == 0, result.output
    assert "status:        cannot_draft" in result.output
    assert "reason:        run_not_successful" in result.output


def test_runs_show_bad_id(runner: CliRunner, seeded_db) -> None:
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "show", "BOGUS", "--json"])
    assert result.exit_code != 0


def test_runs_list_empty(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "empty.db")
    apply_pending(db, MIGRATIONS_DIR)
    monkeypatch.setenv("OPENSQUILLA_META_RUNS_DB", db)
    result = runner.invoke(cli_app, ["skills", "meta", "runs", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_proposals_accept_refuses_stale_required_creator_gates(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    proposal_id = "face1234"
    proposal_dir = tmp_path / "proposals" / proposal_id
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "SKILL.md").write_text(
        """---
name: stale-required-gates
description: "Stale proposal missing current creator gates."
kind: meta
triggers:
  - "stale required gates"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
""",
        encoding="utf-8",
    )
    (proposal_dir / "gates.json").write_text(
        json.dumps({
            "creator_mode": "PERSISTED_PROPOSAL",
            "auto_enable_eligible": True,
        }),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli_app,
        ["skills", "meta", "proposals", "accept", proposal_id],
    )

    assert result.exit_code == 1
    assert "missing_generation_quality_result" in result.output
    assert (proposal_dir / "SKILL.md").is_file()
    assert not (tmp_path / "skills" / "stale-required-gates").exists()


def test_proposals_patch_cli_creates_revision(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    parent_skill_md = """---
name: cli-patch-parent
description: "Original CLI proposal"
kind: meta
triggers:
  - "cli patch parent"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    parent = proposals_lib.write_proposal(
        tmp_path,
        parent_skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    proposal_id = parent["proposal_id"]

    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "patch",
            proposal_id,
            "--patch-json",
            json.dumps({"set_description": "Patched from CLI"}),
            "--owner",
            "cli-test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["parent_proposal_id"] == proposal_id
    child_id = data["proposal_id"]
    assert child_id != proposal_id

    child_skill_md = (tmp_path / "proposals" / child_id / "SKILL.md").read_text(
        encoding="utf-8",
    )
    child_frontmatter = yaml.safe_load(child_skill_md.split("---", 2)[1])
    assert child_frontmatter["description"] == "Patched from CLI"
    child_gates = json.loads((tmp_path / "proposals" / child_id / "gates.json").read_text())
    assert child_gates["revision"]["parent_proposal_id"] == proposal_id
    assert child_gates["revision"]["owner"] == "cli-test"
    assert (
        tmp_path / "proposals" / proposal_id / "SKILL.md"
    ).read_text(encoding="utf-8") == parent_skill_md

    forced_accept = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "accept",
            child_id,
            "--force",
        ],
    )

    assert forced_accept.exit_code == 1
    assert "stale patch revision gates" in forced_accept.output
    assert (tmp_path / "proposals" / child_id / "SKILL.md").is_file()


def test_proposals_refresh_cli_updates_patch_revision_gates(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    parent = proposals_lib.write_proposal(
        tmp_path,
        """---
name: cli-refresh-parent
description: "Refresh CLI proposal"
kind: meta
triggers:
  - "cli refresh parent"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
""",
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    patched = proposals_lib.patch_proposal(
        tmp_path,
        parent["proposal_id"],
        {"add_triggers": ["refresh cli trigger"]},
    )

    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "refresh",
            patched["proposal_id"],
            "--smoke-json",
            json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
            "--collision-result",
            "PASS: refreshed collision",
            "--risk-result",
            "RISK: low\nCAPABILITIES:\n- read-only",
            "--generation-quality-json",
            json.dumps({"passed": True}),
            "--activation-json",
            json.dumps({"passed": True}),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["proposal_id"] == patched["proposal_id"]
    assert "collision_check" in data["refreshed"]
    gates = json.loads(
        (tmp_path / "proposals" / patched["proposal_id"] / "gates.json").read_text(),
    )
    assert gates["collision_check"]["passed"] is True
    assert gates["collision_check"].get("stale") is not True


def test_proposals_benchmark_cli_records_report(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    parent = proposals_lib.write_proposal(
        tmp_path,
        """---
name: cli-benchmark-parent
description: "Original CLI benchmark proposal"
kind: meta
triggers:
  - "cli benchmark parent"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
""",
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    baseline_id = parent["proposal_id"]
    patched = proposals_lib.patch_proposal(
        tmp_path,
        baseline_id,
        {
            "append_eval_prompts": [{
                "name": "cli-benchmark",
                "prompt": "please use cli benchmark child",
                "rubric": ["Summary"],
            }],
        },
    )
    candidate_id = patched["proposal_id"]

    result = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "benchmark",
            baseline_id,
            "--candidate-id",
            candidate_id,
            "--comparison-json",
            json.dumps({
                "passed": True,
                "winner": "candidate",
                "cases": [{
                    "prompt": "please use cli benchmark child",
                    "winner": "candidate",
                    "regression": "",
                }],
            }),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["baseline_proposal_id"] == baseline_id
    assert data["candidate_proposal_id"] == candidate_id
    report = json.loads(
        (
            tmp_path
            / "proposal-benchmarks"
            / data["benchmark_id"]
            / "benchmark.json"
        ).read_text(encoding="utf-8"),
    )
    assert report["creator_mode"] == "BENCHMARK"
    assert report["gates"]["benchmark_compare"]["passed"] is True


def test_proposals_accept_replace_and_rollback_cli(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    original_md = """---
name: cli-replace-target
description: "Original CLI replacement target"
kind: meta
triggers:
  - "cli replace target"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    original = proposals_lib.write_proposal(
        tmp_path,
        original_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    accepted = runner.invoke(
        cli_app,
        ["skills", "meta", "proposals", "accept", original["proposal_id"], "--json"],
    )
    assert accepted.exit_code == 0, accepted.output

    replacement = proposals_lib.write_proposal(
        tmp_path,
        original_md.replace("Original CLI", "Replacement CLI"),
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    replaced = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "accept",
            replacement["proposal_id"],
            "--replace",
            "--owner",
            "cli-test",
            "--deprecates",
            "cli-replace-target,legacy-cli-helper",
            "--migration-notes",
            "CLI replacement migration notes.",
            "--json",
        ],
    )

    assert replaced.exit_code == 0, replaced.output
    # Human line is emitted first, JSON result last for historical CLI behavior.
    replaced_json = json.loads(replaced.output.strip().splitlines()[-1])
    assert replaced_json["replaced"] is True
    assert replaced_json["rollback_target"]["proposal_id"] == original["proposal_id"]
    gates = json.loads((tmp_path / "skills" / "cli-replace-target" / "gates.json").read_text())
    assert gates["lifecycle"]["deprecates"] == [
        "cli-replace-target",
        "legacy-cli-helper",
    ]
    assert gates["lifecycle"]["migration_notes"] == "CLI replacement migration notes."

    rolled_back = runner.invoke(
        cli_app,
        [
            "skills",
            "meta",
            "proposals",
            "rollback",
            "cli-replace-target",
            "--json",
        ],
    )

    assert rolled_back.exit_code == 0, rolled_back.output
    rolled_back_json = json.loads(rolled_back.output.strip().splitlines()[-1])
    assert rolled_back_json["restored_proposal_id"] == original["proposal_id"]
    assert "Original CLI replacement target" in (
        tmp_path / "skills" / "cli-replace-target" / "SKILL.md"
    ).read_text(encoding="utf-8")
