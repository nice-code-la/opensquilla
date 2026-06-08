from __future__ import annotations

import json
from dataclasses import replace

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


def test_author_seed_refuses_failed_or_empty_trace() -> None:
    failed = draft_meta_skill_seed(_record(status="failed", final_text=None))
    assert failed["status"] == "cannot_draft"
    assert failed["reason"] == "run_not_successful"
    assert "creator_input" not in failed

    empty = draft_meta_skill_seed(_record(user_message="", final_text=""))
    assert empty["status"] == "cannot_draft"
    assert empty["reason"] == "missing_goal_or_output"

    no_steps = draft_meta_skill_seed(replace(_record(), steps=()))
    assert no_steps["status"] == "cannot_draft"
    assert no_steps["reason"] == "missing_observed_steps"
    assert "creator_input" not in no_steps


def test_author_seed_scrubs_secret_and_file_like_literals() -> None:
    seed = draft_meta_skill_seed(_record(
        user_message=(
            "Use API key sk-live-1234567890abcdef and summarize "
            "/home/alice/private/customer_contract.pdf for finance."
        )
    ))

    payload = json.dumps(seed, ensure_ascii=False)
    assert "sk-live" not in payload
    assert "/home/alice/private" not in payload
    assert "[secret]" in payload
    assert "[file]" in payload
    assert seed["privacy_warnings"] == ["secret_like_text_redacted", "file_path_redacted"]


def test_author_seed_scrubs_plan_derived_payloads() -> None:
    plan = MetaPlan(
        name="meta-vendor-brief",
        triggers=("vendor decision brief",),
        priority=10,
        steps=(
            MetaStep(id="search", skill="web-search", kind="agent", label="Search"),
        ),
        request_template={
            "outcome": "Decision brief from ghp_1234567890abcdef",
            "fields": [{
                "name": "vendor",
                "prompt": "Review /home/alice/private/customer_contract.pdf",
            }],
        },
        output_contract={
            "constraints": ["Do not expose xoxb_1234567890abcdef"],
        },
        eval_prompts=[{
            "name": "vendor-brief",
            "prompt": "Use /home/alice/private/vendor.pdf and pk-live-1234567890abcdef",
            "rubric": ["Evidence from /home/alice/private/rubric.md"],
        }],
    )
    seed = draft_meta_skill_seed(replace(
        _record(),
        plan_snapshot_json=json.dumps(to_jsonable(plan)),
    ))

    payload = json.dumps(seed, ensure_ascii=False)
    assert "ghp_1234567890abcdef" not in payload
    assert "xoxb_1234567890abcdef" not in payload
    assert "pk-live" not in payload
    assert "/home/alice/private" not in payload
    assert "[secret]" in payload
    assert "[file]" in payload
    assert seed["outputs"] == ["Evidence from [file]"]
    assert seed["privacy_warnings"] == ["secret_like_text_redacted", "file_path_redacted"]


class _Spec:
    def __init__(self, name: str, description: str, triggers: list[str]) -> None:
        self.name = name
        self.description = description
        self.triggers = triggers


def test_author_seed_recommends_patch_for_strong_duplicate() -> None:
    seed = draft_meta_skill_seed(
        _record(user_message="Research a vendor and produce a decision brief."),
        existing_specs=[
            _Spec(
                "meta-vendor-decision-brief",
                "Research a vendor and produce a decision brief.",
                ["vendor decision brief"],
            )
        ],
    )

    duplicate = seed["duplicate_detection"]
    assert duplicate["suggested_action"] == "patch_existing"
    assert duplicate["target"] == "meta-vendor-decision-brief"
    assert duplicate["score"] >= 0.75


def test_author_seed_allows_distinct_seed() -> None:
    seed = draft_meta_skill_seed(
        _record(user_message="Plan a school science fair project."),
        existing_specs=[
            _Spec(
                "meta-vendor-decision-brief",
                "Research vendors for procurement decisions.",
                ["vendor decision brief"],
            )
        ],
    )

    assert seed["duplicate_detection"]["suggested_action"] == "create_new"
