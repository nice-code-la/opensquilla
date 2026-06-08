from __future__ import annotations

import json

from opensquilla.persistence.meta_run_writer import RunRecord, StepRecord
from opensquilla.skills.meta.author_seed import draft_meta_skill_seed
from opensquilla.skills.meta.plan_serde import to_jsonable
from opensquilla.skills.meta.types import MetaPlan, MetaStep


def _record(
    *,
    status: str = "ok",
    user_message: str = "Research a vendor and produce a decision brief.",
    final_text: str | None = "Done.",
) -> RunRecord:
    plan = MetaPlan(
        name="meta-vendor-brief",
        triggers=("vendor decision brief",),
        priority=10,
        steps=(
            MetaStep(id="search", skill="web-search", kind="agent", label="Search"),
            MetaStep(id="draft", skill="summarize", kind="agent", label="Draft"),
        ),
        request_template={
            "outcome": "Decision brief",
            "fields": [{"name": "vendor", "required": True}],
        },
        output_contract={"required_sections": ["Recommendation", "Evidence"]},
        eval_prompts=[{
            "name": "vendor-brief",
            "prompt": "Research Acme and recommend whether to buy.",
            "rubric": ["Recommendation", "Evidence"],
        }],
    )
    return RunRecord(
        run_id="run_01",
        meta_skill_name="meta-vendor-brief",
        meta_skill_digest="digest",
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
        triggered_by="soft_meta_invoke",
        session_key="sess",
        turn_id="turn",
        owner_pid=None,
        status=status,
        started_at_ms=1,
        ended_at_ms=2,
        inputs_json=json.dumps({"user_message": user_message}),
        final_text=final_text,
        failed_step_id=None,
        error=None,
        truncated_fields=(),
        steps=(
            StepRecord(
                run_id="run_01",
                step_id="search",
                step_kind="agent",
                declared_skill="web-search",
                effective_skill="web-search",
                status="ok",
                started_at_ms=1,
                ended_at_ms=2,
                rendered_inputs_json=json.dumps({"query": "Acme vendor"}),
                output_text="found sources",
                error=None,
                substitute_step_id=None,
                truncated_fields=(),
            ),
            StepRecord(
                run_id="run_01",
                step_id="draft",
                step_kind="agent",
                declared_skill="summarize",
                effective_skill="summarize",
                status="ok",
                started_at_ms=2,
                ended_at_ms=3,
                rendered_inputs_json=json.dumps({"text": "{{ outputs.search }}"}),
                output_text="decision brief",
                error=None,
                substitute_step_id=None,
                truncated_fields=(),
            ),
        ),
    )


def test_author_seed_emits_normalized_creator_payload() -> None:
    seed = draft_meta_skill_seed(_record())

    assert seed["status"] == "ok"
    assert seed["source_kind"] == "meta_run"
    assert seed["goal"] == "Research a vendor and produce a decision brief."
    assert seed["observed_steps"] == ["web-search", "summarize"]
    assert seed["candidate_triggers"][0] == "Research a vendor and produce a decision brief."
    assert seed["inputs"] == ["vendor"]
    assert seed["outputs"] == ["Recommendation", "Evidence"]
    assert seed["negative_cases"]
    assert seed["evidence_refs"] == ["run_01"]
    assert seed["creator_input"]["recommended_mode"] == "PERSISTED_PROPOSAL"
    assert json.loads(seed["creator_input"]["draft_seed_json"])["goal"] == seed["goal"]


def test_author_seed_preserves_legacy_keys() -> None:
    seed = draft_meta_skill_seed(_record())

    assert seed["source_run"]["run_id"] == "run_01"
    assert seed["name"] == "meta-vendor-brief-draft"
    assert seed["trigger_candidates"] == seed["candidate_triggers"]
    assert seed["request_template"]["outcome"] == "Decision brief"
    assert seed["output_contract"]["required_sections"] == ["Recommendation", "Evidence"]
    assert seed["composition"]["steps"][0]["id"] == "search"
