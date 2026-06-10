"""End-to-end: creator pipeline with stubbed LLMs produces a valid proposal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# creator_fixtures is on sys.path via tests/test_skills/conftest.py
from creator_fixtures import INTENT_PDF_DIGEST, INTENT_TRIP_PLANNER, synth_decision_log

from opensquilla.engine.steps.meta_resolution import meta_resolution
from opensquilla.engine.types import TextDeltaEvent
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch, MetaResult

REPO = Path(__file__).resolve().parents[2]
_BUNDLED_BASE = REPO / "src" / "opensquilla" / "skills" / "bundled"
PROPOSALS = _BUNDLED_BASE / "skill-creator-proposals" / "scripts" / "proposals.py"
LINT = _BUNDLED_BASE / "skill-creator-linter" / "scripts" / "lint.py"
BUNDLED = _BUNDLED_BASE


def test_creator_catalog_excludes_outer_creator_helper_skills() -> None:
    """The generated candidate DAG must not be allowed to call creator gates.

    Gate, judge, and proposal persistence steps belong to meta-skill-creator
    itself. If those helper skills leak into the slot-filling catalog, the LLM
    can put proposal persistence inside the candidate meta-skill and runtime
    E2E will correctly fail.
    """
    from opensquilla.skills.creator import proposer

    catalog = proposer._build_catalog_summary()

    assert "history-explorer" in catalog
    assert "summarize" in catalog
    assert "skill-creator-proposals" not in catalog
    assert "skill-creator-linter" not in catalog
    assert "skill-creator-smoke-test" not in catalog
    assert "meta-skill-creator" not in catalog


def test_e2e_p1_proposal_lint_pass(tmp_path, monkeypatch) -> None:
    """Stub each LLM step + run the full pipeline; verify proposal is
    auto_enable_eligible."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_PDF_DIGEST["co_occurrence_seed"])

    from opensquilla.skills.creator import proposer

    canned_slots = {
        "name": "synth-pdf-digest-pipeline",
        "description": "Synthetic PDF digest: extract then summarize then memorize.",
        "meta_priority": 50,
        "triggers": ["synth pdf digest"],
        "steps": [
            {"id": "extract", "skill": "pdf-toolkit", "task": "extract", "with_keys": {}},
            {"id": "digest", "skill": "summarize", "task": "summarize", "with_keys": {}},
            {"id": "save", "skill": "memory", "task": "persist", "with_keys": {}},
        ],
    }
    monkeypatch.setattr(
        proposer, "_call_llm_for_slots", lambda prompt, **_: json.dumps(canned_slots),
    )

    skill_md = proposer.meta_skill_assemble("p1_sequential", json.dumps(canned_slots))
    assert "synth-pdf-digest-pipeline" in skill_md

    proc = subprocess.run(
        [sys.executable, str(LINT), "--skill-md-stdin", "--gates", "G1,G2"],
        input=skill_md, capture_output=True, text=True, check=True,
    )
    lint_result = json.loads(proc.stdout)
    assert lint_result["G1"]["passed"]
    assert lint_result["G2"]["passed"]

    smoke_result = proposer.run_smoke_gates(
        skill_md=skill_md,
        fixture_gen_fn=lambda md, kind: {
            "positive": "please use synth pdf digest now",
            "negative": "tell me a joke unrelated",
        }[kind],
        classifier_model="stub",
    )
    assert smoke_result["G3"]["passed"]
    assert smoke_result["G4"]["passed"]
    # ``classifier_model="stub"`` makes ``run_smoke_gates`` flag the
    # result as degraded — no cross-vendor classification actually ran,
    # so G3/G4 pass by stub-fixture construction only.
    assert smoke_result.get("degraded") is True

    out = subprocess.run(
        [sys.executable, str(PROPOSALS),
         "--action", "write_proposal", "--home", str(home),
         "--skill-md-inline", skill_md,
         "--lint-result", json.dumps(lint_result),
         "--smoke-result", json.dumps(smoke_result)],
        capture_output=True, text=True, check=True,
    )
    persist = json.loads(out.stdout)
    # D1: degraded smoke must NOT yield ``auto_enable_eligible``. The
    # proposal still persists (operators can review it on disk), but
    # the unattended creator pipeline cannot promote a candidate that
    # was never validated against a real classifier model.
    assert persist["auto_enable_eligible"] is False

    proposal_dir = home / "proposals" / persist["proposal_id"]
    assert (proposal_dir / "SKILL.md").is_file()
    assert (proposal_dir / "gates.json").is_file()
    gates_payload = json.loads((proposal_dir / "gates.json").read_text())
    assert gates_payload["smoke"].get("degraded") is True
    assert gates_payload["auto_enable_eligible"] is False


def test_creator_preserves_required_triggers_and_prior_step_context(monkeypatch) -> None:
    """Creator output must keep explicit trigger requirements and complete
    the evidence chain for sequential templates."""
    from opensquilla.skills.creator import proposer

    canned_slots = {
        "name": "traceback-debug-orchestrator",
        "description": (
            "Diagnose traceback root causes by chaining history, diff, and summary."
        ),
        "meta_priority": 55,
        "triggers": [
            "diagnose this traceback",
            "debug this stack trace with history and diff",
        ],
        "steps": [
            {
                "id": "history_scan",
                "skill": "history-explorer",
                "task": "Find related traceback history",
                "with_keys": {},
            },
            {
                "id": "diff_capture",
                "skill": "git-diff",
                "task": "Capture current diff",
                "with_keys": {},
            },
            {
                "id": "synthesize_report",
                "skill": "summarize",
                "task": "Produce a Chinese root-cause report from all evidence",
                "with_keys": {},
            },
        ],
    }
    monkeypatch.setattr(
        proposer,
        "_call_llm_for_slots",
        lambda prompt, **_: json.dumps(canned_slots),
    )

    slots_json = proposer.meta_skill_fill_slots(
        "p1_sequential",
        history_summary="history-explorer -> git-diff -> summarize freq=5",
        user_intent=(
            "请创建中文 traceback 根因诊断 meta-skill。"
            "触发短语要包含：诊断 traceback、traceback 根因、stack trace root cause。"
        ),
    )

    slots = json.loads(slots_json)
    assert slots["triggers"][:3] == [
        "诊断 traceback",
        "traceback 根因",
        "stack trace root cause",
    ]

    skill_md = proposer.meta_skill_assemble("p1_sequential", slots_json)
    assert "kind: skill_exec\n      skill: \"history-explorer\"" in skill_md
    assert "kind: skill_exec\n      skill: \"git-diff\"" in skill_md
    assert "kind: llm_chat\n      skill: \"summarize\"" in skill_md
    assert "outputs.history_scan" in skill_md
    assert "outputs.diff_capture" in skill_md


def test_creator_dag_passes_raw_user_request_to_slot_filling(tmp_path) -> None:
    """Slot filling must see raw user requirements, not only clarification
    summaries, so hard constraints compete fairly with the baseline gate."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    fill_slots = {step.id: step for step in plan.steps}["fill_slots"]
    user_intent = str(fill_slots.tool_args["user_intent"])
    assert "inputs.user_message" in user_intent
    assert "outputs.clarify_intent" in user_intent


def test_creator_runtime_e2e_uses_candidate_trigger_prompt(tmp_path) -> None:
    """Runtime E2E should exercise the candidate skill, not the outer creator
    request that produced it."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    runtime_e2e = {step.id: step for step in plan.steps}["runtime_e2e"]
    assert runtime_e2e.tool_args["skill_md"] == "{{ outputs.assemble }}"
    assert runtime_e2e.tool_args["eval_prompts"] == ""


def test_creator_dag_runs_generation_quality_and_activation_before_persist(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    steps = {step.id: step for step in plan.steps}
    assert "generation_quality" in steps
    assert "activation_eval" in steps

    generation_quality = steps["generation_quality"]
    assert list(generation_quality.depends_on) == ["fill_slots"]
    assert generation_quality.tool == "meta_skill_generation_quality_run"
    assert generation_quality.tool_args == {
        "pattern_id": "{{ outputs.pick_pattern }}",
        "slots_json": "{{ outputs.fill_slots }}",
    }

    activation_eval = steps["activation_eval"]
    assert list(activation_eval.depends_on) == ["assemble"]
    assert activation_eval.tool == "meta_skill_activation_eval_run"
    assert activation_eval.tool_args == {
        "skill_md": "{{ outputs.assemble }}",
        "positive_prompts": "auto",
        "catalog_negative_prompts": "auto",
    }

    persist = steps["persist"]
    assert list(persist.depends_on) == [
        "preview",
        "assemble",
        "generation_quality",
        "activation_eval",
        "bundle_assets",
    ]
    assert persist.tool_args["generation_quality_result"] == "{{ outputs.generation_quality }}"
    assert persist.tool_args["activation_result"] == "{{ outputs.activation_eval }}"

    preview_task = str(steps["preview"].with_args["task"])
    assert "Generation quality:" in preview_task
    assert "Activation eval:" in preview_task

    acceptance_compare = steps["acceptance_compare"]
    assert list(acceptance_compare.depends_on) == [
        "assemble",
        "single_model_baseline",
        "generation_quality",
        "activation_eval",
    ]
    acceptance_task = str(acceptance_compare.with_args["task"])
    assert "Generation quality:" in acceptance_task
    assert "Activation eval:" in acceptance_task


def test_creator_dag_forwards_optional_draft_seed_to_fill_slots() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    seed_prompt_expr = '{{ inputs.draft_seed_json | default("") | xml_escape | truncate(2000) }}'
    clarify_task = str(steps["clarify_intent"].with_args["task"])
    assert "Optional draft seed JSON" in clarify_task
    assert seed_prompt_expr in clarify_task
    assert "unless the seed explicitly refuses drafting" in clarify_task

    pick_pattern_user_intent = str(steps["pick_pattern"].with_args["user_intent"])
    assert "Draft seed JSON, if supplied" in pick_pattern_user_intent
    assert seed_prompt_expr in pick_pattern_user_intent

    fill_slots = steps["fill_slots"]
    assert fill_slots.tool_args["draft_seed_json"] == "{{ inputs.draft_seed_json | default('') }}"
    assert list(steps["generation_quality"].depends_on) == ["fill_slots"]
    assert "generation_quality_result" in steps["persist"].tool_args
    assert "activation_result" in steps["persist"].tool_args


def test_creator_dag_forwards_conditional_visibility_guidance_to_fill_slots() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    fill_slots_intent = str(steps["fill_slots"].tool_args["user_intent"])
    assert "requires_toolsets" in fill_slots_intent
    assert "fallback_for_tools" in fill_slots_intent
    assert "platforms" in fill_slots_intent
    assert "config_keys" in fill_slots_intent
    assert "Do not invent conditional visibility fields" in fill_slots_intent


def test_creator_dag_forwards_optional_learning_summary_to_fill_slots() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    learning_summary = steps["learning_summary"]
    assert learning_summary.kind == "tool_call"
    assert learning_summary.tool == "meta_skill_creator_learning_summary"
    assert learning_summary.depends_on == ("creator_mode",)

    assert "learning_summary" in steps["fill_slots"].depends_on
    fill_slots_intent = str(steps["fill_slots"].tool_args["user_intent"])
    assert "Creator learning summary" in fill_slots_intent
    expected_summary_template = (
        '{{ outputs.learning_summary | default(inputs.creator_learning_summary | '
        'default("")) | xml_escape | truncate(2000) }}'
    )
    assert (
        expected_summary_template in fill_slots_intent
    )
    assert "advisory memory" in fill_slots_intent


def test_creator_dag_generates_optional_bundle_assets_before_persist() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    bundle_assets = steps["bundle_assets"]
    assert bundle_assets.kind == "llm_chat"
    assert bundle_assets.depends_on == ("assemble", "generation_quality")
    assert "outputs.creator_mode != 'PATCH_PROPOSAL'" in bundle_assets.when
    assert "Return only a JSON object" in str(bundle_assets.with_args["task"])
    assert "scripts/" in str(bundle_assets.with_args["task"])
    assert "evals/" in str(bundle_assets.with_args["task"])

    persist = steps["persist"]
    assert "bundle_assets" in persist.depends_on
    assert persist.tool_args["bundle_files_json"] == "{{ outputs.bundle_assets }}"


def test_creator_final_response_includes_persist_status() -> None:
    from opensquilla.skills.loader import SkillLoader

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    final_response = steps["final_response"]
    assert "persist" in final_response.depends_on
    final_text = str(final_response.tool_args["text"])
    assert "outputs.persist" in final_text
    assert "Saved proposal status" in final_text


def test_creator_route_guards_accept_json_clarify_output() -> None:
    """Live models sometimes obey the schema as JSON; keep the DAG moving."""
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.templating import evaluate_when

    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("meta-skill-creator")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    json_clarify = (
        '{\n'
        '  "ROUTE": "meta-skill",\n'
        '  "WORKFLOW_GOAL": "Create a bundled meta-skill",\n'
        '  "NEEDS_CLARIFICATION": "no"\n'
        '}'
    )

    assert evaluate_when(
        steps["creator_mode"].when,
        inputs={},
        outputs={"clarify_intent": json_clarify},
    ) is True


def test_creator_dag_routes_patch_proposal_without_persist(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    assert "PATCH_PROPOSAL" in steps["creator_mode"].output_choices
    assert steps["build_patch_request"].kind == "llm_chat"
    assert steps["build_patch_request"].depends_on == ("creator_mode",)
    assert "outputs.creator_mode == 'PATCH_PROPOSAL'" in steps["build_patch_request"].when
    assert "Allowed keys:" in str(steps["build_patch_request"].with_args["task"])

    extract = steps["extract_patch_target"]
    assert extract.kind == "tool_call"
    assert extract.tool == "meta_skill_extract_proposal_id"
    assert extract.depends_on == ("creator_mode",)
    assert "outputs.creator_mode == 'PATCH_PROPOSAL'" in extract.when
    assert "inputs.user_message" in str(extract.tool_args["text"])
    assert "inputs.target_proposal_id" in str(extract.tool_args["fallback"])

    patch = steps["patch_proposal"]
    assert patch.kind == "tool_call"
    assert patch.tool == "meta_skill_patch_proposal"
    assert patch.depends_on == ("extract_patch_target", "build_patch_request")
    assert patch.tool_args == {
        "proposal_id": "{{ outputs.extract_patch_target }}",
        "patch_json": "{{ outputs.build_patch_request }}",
        "home": "{{ inputs.home | default('') }}",
    }

    for step_id in (
        "harvest",
        "pick_pattern",
        "fill_slots",
        "assemble",
        "generation_quality",
        "activation_eval",
        "collision_check",
        "lint",
        "risk_classify",
        "smoke",
        "preview",
        "persist",
    ):
        assert "outputs.creator_mode != 'PATCH_PROPOSAL'" in steps[step_id].when

    assert "patch_proposal" in steps["final_response"].depends_on
    assert "outputs.patch_proposal" in str(steps["final_response"].tool_args["text"])


def test_creator_dag_routes_benchmark_without_persist(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}

    assert "BENCHMARK" in steps["creator_mode"].output_choices

    extract = steps["extract_benchmark_targets"]
    assert extract.kind == "tool_call"
    assert extract.tool == "meta_skill_extract_benchmark_proposal_ids"
    assert extract.depends_on == ("creator_mode",)
    assert "outputs.creator_mode == 'BENCHMARK'" in extract.when
    assert "inputs.baseline_proposal_id" in str(extract.tool_args["baseline_fallback"])
    assert "inputs.candidate_proposal_id" in str(extract.tool_args["candidate_fallback"])

    benchmark = steps["benchmark_proposals"]
    assert benchmark.kind == "tool_call"
    assert benchmark.tool == "meta_skill_benchmark_proposals"
    assert benchmark.depends_on == ("extract_benchmark_targets",)
    assert benchmark.tool_args == {
        "target_json": "{{ outputs.extract_benchmark_targets }}",
        "comparison_json": "{{ inputs.comparison_json | default('') }}",
        "eval_prompts_json": "{{ inputs.eval_prompts_json | default('') }}",
        "home": "{{ inputs.home | default('') }}",
    }

    for step_id in (
        "harvest",
        "pick_pattern",
        "fill_slots",
        "assemble",
        "generation_quality",
        "activation_eval",
        "collision_check",
        "lint",
        "risk_classify",
        "smoke",
        "preview",
        "persist",
    ):
        assert "outputs.creator_mode != 'BENCHMARK'" in steps[step_id].when

    assert "benchmark_proposals" in steps["final_response"].depends_on
    assert "outputs.benchmark_proposals" in str(steps["final_response"].tool_args["text"])


async def test_meta_resolution_routes_pending_proposal_revision_request(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    ctx = SimpleNamespace(
        message="please revise proposal deadbeef to add a safer trigger",
        semantic_message="please revise proposal deadbeef to add a safer trigger",
        system_prompt=("base system prompt", ""),
        metadata={"skill_loader": loader},
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-skill-creator"


async def test_meta_resolution_routes_pending_proposal_benchmark_request(tmp_path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    ctx = SimpleNamespace(
        message="benchmark proposal abcd1234 against deadbeef",
        semantic_message="benchmark proposal abcd1234 against deadbeef",
        system_prompt=("base system prompt", ""),
        metadata={"skill_loader": loader},
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-skill-creator"


async def test_orchestrator_patches_proposal_id_from_user_message(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".opensquilla"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    from opensquilla.skills import proposals_lib

    parent = proposals_lib.write_proposal(
        home,
        """---
name: synth-user-message-patch
description: "Meta-skill proposal patched from a natural-language request."
kind: meta
meta_priority: 50
triggers:
  - "user message patch"
composition:
  steps:
    - id: digest
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
""",
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        raise AssertionError("patch branch should not start agent steps")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "ROUTE: meta-skill\n"
                "WORKFLOW_GOAL: revise pending proposal\n"
                "OUTPUT_SHAPE: revised proposal\n"
                f"TRIGGERS: proposal {parent['proposal_id']}\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            )
        if "Classify how far the creator workflow should go" in user_prompt:
            return "PATCH_PROPOSAL"
        if "Return only a JSON object" in user_prompt:
            return '{"add_triggers": ["user message patch revised"]}'
        raise AssertionError(f"unexpected llm prompt: {user_prompt[:200]}")

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        if tool_name == "meta_skill_extract_proposal_id":
            from opensquilla.skills.creator.proposer import meta_skill_extract_proposal_id

            return meta_skill_extract_proposal_id(
                str(args.get("text", "")),
                str(args.get("fallback", "")),
            )
        if tool_name == "meta_skill_patch_proposal":
            from opensquilla.skills.creator.proposer import meta_skill_patch_proposal

            return meta_skill_patch_proposal(
                str(args["proposal_id"]),
                str(args["patch_json"]),
                str(args.get("home", "")),
            )
        raise AssertionError(f"patch branch should not call {tool_name}")

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={
            "user_message": (
                f"请修订 proposal {parent['proposal_id']}，添加触发词 "
                "user message patch revised"
            ),
        },
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None
    assert final_result.ok, final_result.error
    patch_payload = json.loads(final_result.step_outputs["patch_proposal"])
    assert patch_payload["status"] == "ok"
    assert patch_payload["parent_proposal_id"] == parent["proposal_id"]
    assert final_result.step_outputs["persist"] == ""
    child_dir = home / "proposals" / patch_payload["proposal_id"]
    assert "user message patch revised" in (
        child_dir / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_manual_creator_persist_auto_enables_when_setting_is_on(tmp_path) -> None:
    """The manual meta-skill-creator persist tool should use the same
    conservative auto-enable path as cron/dream auto-propose when the
    operator has enabled it in runtime settings."""
    home = tmp_path / ".opensquilla"

    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    proposals_lib.write_auto_propose_settings(
        home,
        {"auto_enable": True, "auto_enable_max_risk": "low"},
    )
    skill_md = """---
name: synth-manual-auto-enable
description: "Manual creator output that is safe to auto-enable."
kind: meta
meta_priority: 50
triggers:
  - "manual auto enable"
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: digest
      skill: summarize
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    lint_result = {"G1": {"passed": True}, "G2": {"passed": True}}
    smoke_result = {"G3": {"passed": True}, "G4": {"passed": True}}

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md,
        json.dumps(lint_result),
        json.dumps(smoke_result),
        home=str(home),
        collision_result="PASS: no trigger collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_result=json.dumps({"required": True, "passed": True, "reason": "ok"}),
        activation_result=json.dumps({"required": True, "passed": True, "reason": "ok"}),
    ))

    assert out["status"] == "ok"
    assert out["auto_enable"]["status"] == "enabled"
    assert out["auto_enable"]["triggered_by"] == "manual"
    assert not (home / "proposals" / out["proposal_id"]).exists()
    assert (home / "skills" / "synth-manual-auto-enable" / "SKILL.md").is_file()


def test_auto_propose_persist_can_defer_manual_auto_enable(tmp_path) -> None:
    """Cron/dream auto-propose must own provenance and auto-enable decisions.

    The persist tool supports manual auto-enable for user-active creator runs,
    but auto-propose injects ``auto_enable_manual=False`` so it can patch
    auto_cron/auto_dream provenance before attempting promotion.
    """
    home = tmp_path / ".opensquilla"

    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    proposals_lib.write_auto_propose_settings(
        home,
        {"auto_enable": True, "auto_enable_max_risk": "low"},
    )
    skill_md = """---
name: synth-deferred-auto-enable
description: "Safe creator output whose promotion is deferred to auto_propose."
kind: meta
meta_priority: 50
triggers:
  - "deferred auto enable"
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: digest
      skill: summarize
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    lint_result = {"G1": {"passed": True}, "G2": {"passed": True}}
    smoke_result = {"G3": {"passed": True}, "G4": {"passed": True}}

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md,
        json.dumps(lint_result),
        json.dumps(smoke_result),
        home=str(home),
        auto_enable_manual=False,
    ))

    assert out["status"] == "ok"
    assert "auto_enable" not in out
    assert (home / "proposals" / out["proposal_id"] / "SKILL.md").is_file()
    assert not (home / "skills" / "synth-deferred-auto-enable").exists()


async def test_orchestrator_drives_creator_dag_end_to_end(tmp_path, monkeypatch) -> None:
    """Full DAG through MetaOrchestrator with stubbed downstream runners."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_PDF_DIGEST["co_occurrence_seed"])
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(log_dir))

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None, "meta-skill-creator not loaded; check Task 6"
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            yield TextDeltaEvent(text=(
                "Route: Meta-Skill\n"
                "WORKFLOW_GOAL: compose X then Y\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: orch e2e trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            ))
            return
        yield TextDeltaEvent(text="<stub:agent>")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "Route: Meta-Skill\n"
                "WORKFLOW_GOAL: compose X then Y\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: orch e2e trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            )
        return "p1_sequential"

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        if tool_name == "meta_skill_fill_slots":
            return json.dumps({
                "name": "synth-orch-e2e", "description": "x" * 50,
                "meta_priority": 50, "triggers": ["orch e2e trigger"],
                "steps": [
                    {"id": "a", "skill": "summarize", "task": "t", "with_keys": {}},
                    {"id": "b", "skill": "memory", "task": "t", "with_keys": {}},
                ],
            })
        if tool_name == "meta_skill_assemble":
            from opensquilla.skills.creator.proposer import meta_skill_assemble
            return meta_skill_assemble(args["pattern_id"], args["slots_json"])
        return f"<stub:{tool_name}>"

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={
            "user_message": "compose a meta-skill that does X then Y",
            "system_prompt": "Unattended meta-skill auto-propose run.",
        },
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None, "orchestrator did not yield a MetaResult"
    assert final_result.ok, f"orchestrator failed: {final_result.error}"
    assert set(final_result.step_outputs.keys()) >= {
        "harvest", "pick_pattern", "fill_slots", "assemble", "lint", "smoke", "persist"
    }
    # harvest now runs as skill_exec (history-explorer has an entrypoint:),
    # so it returns JSON from explore.py rather than a stub agent reply.
    harvest_output = final_result.step_outputs.get("harvest", "")
    assert harvest_output, "harvest step produced no output"
    harvest_json = json.loads(harvest_output)
    assert "co_occurrences" in harvest_json


async def test_creator_dag_stops_when_clarify_routes_normal_skill(tmp_path) -> None:
    """ROUTE: normal-skill must not reach assemble or proposal persistence."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        raise AssertionError("normal-skill route should not start creator agents")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "Route: Normal-Skill\n"
                "WORKFLOW_GOAL: create a standalone skill\n"
                "OUTPUT_SHAPE: normal SKILL.md\n"
                "TRIGGERS: standalone helper\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: not a meta-skill request"
            )
        raise AssertionError("normal-skill route should not call creator classifiers")

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        raise AssertionError(f"normal-skill route should not call {tool_name}")

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={"user_message": "please create a normal standalone skill"},
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None
    assert final_result.ok
    assert final_result.step_outputs["clarify_intent"].lower().startswith("route: normal-skill")
    assert final_result.step_outputs["assemble"] == ""
    assert final_result.step_outputs["persist"] == ""
    assert "normal standalone skill request" in final_result.final_text


async def test_orchestrator_p2_fan_out_merge_proposal(tmp_path, monkeypatch) -> None:
    """P2 fan-out-merge topology: two parallel branches + merge step."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_TRIP_PLANNER["co_occurrence_seed"])
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(log_dir))

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    plan = parse_meta_plan(creator_spec)

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            yield TextDeltaEvent(text=(
                "ROUTE: meta-skill\n"
                "WORKFLOW_GOAL: compose trip planning workflow\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: synth p2 trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            ))
            return
        yield TextDeltaEvent(text="<stub:agent>")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "ROUTE: meta-skill\n"
                "WORKFLOW_GOAL: compose trip planning workflow\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: synth p2 trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            )
        return "p2_fan_out_merge"

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        if tool_name == "meta_skill_fill_slots":
            return json.dumps({
                "name": "synth-p2-trip", "description": "x" * 50,
                "meta_priority": 50, "triggers": ["synth p2 trigger"],
                "branches": [
                    {"id": "weather", "skill": "weather", "task": "w", "with_keys": {}},
                    {"id": "poi", "skill": "multi-search-engine", "task": "p", "with_keys": {}},
                ],
                "merge": {"id": "itin", "skill": "summarize", "task": "m", "with_keys": {}},
                "tail": None,
            })
        if tool_name == "meta_skill_assemble":
            from opensquilla.skills.creator.proposer import meta_skill_assemble
            return meta_skill_assemble(args["pattern_id"], args["slots_json"])
        return f"<stub:{tool_name}>"

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={"user_message": "compose a trip-planner meta-skill"},
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None and final_result.ok
    assemble_output = final_result.step_outputs["assemble"]
    assert "depends_on: [weather, poi]" in assemble_output
