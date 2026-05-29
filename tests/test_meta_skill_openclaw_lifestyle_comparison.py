import asyncio
import json
from scripts.compare_meta_skill_openclaw_lifestyle import (
    OPENCLAW_T3_MODEL,
    LIFESTYLE_COMPARISON_CASES,
    _lifestyle_judge_result_is_complete,
    _compare_results,
    _apply_lifestyle_judge_result,
    _judge_lifestyle_with_retries,
    judge_existing,
    load_openclaw_baseline,
    build_lifestyle_rows,
    render_lifestyle_markdown,
    render_lifestyle_prompts_markdown,
    score_response,
)
from scripts.compare_meta_skill_openclaw import EndpointResult, JudgeResult, OpenSquillaRunner
from pathlib import Path

from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.templating import evaluate_when


SELECTED_SKILLS = [
    "meta-document-to-decision",
    "meta-web-research-to-report",
    "meta-daily-operator-brief",
    "meta-safe-skill-installer",
    "meta-family-day-coordinator",
]


def test_lifestyle_catalog_covers_selected_meta_skills_without_exclusions() -> None:
    assert [case.skill_name for case in LIFESTYLE_COMPARISON_CASES] == SELECTED_SKILLS
    assert {case.case_id for case in LIFESTYLE_COMPARISON_CASES} == {
        "document_vendor_decision",
        "web_research_parent_esim",
        "daily_operator_morning_plan",
        "safe_skill_install_audit",
        "family_school_errand_day",
    }
    assert all(case.scenario == "lifestyle_primary" for case in LIFESTYLE_COMPARISON_CASES)
    assert "meta-paper-write" not in {case.skill_name for case in LIFESTYLE_COMPARISON_CASES}
    assert "meta-skill-creator" not in {case.skill_name for case in LIFESTYLE_COMPARISON_CASES}


def test_selected_meta_skills_are_grounded_in_clawhub_top100_components() -> None:
    expectations = {
        "meta-document-to-decision": ["Word / DOCX", "Excel / XLSX", "Pdf"],
        "meta-web-research-to-report": ["Multi Search Engine", "Word / DOCX"],
        "meta-daily-operator-brief": ["Weather", "Multi Search Engine", "Elite Longterm Memory"],
        "meta-safe-skill-installer": ["Skill Vetter", "Find Skills Skill", "Multi Search Engine"],
        "meta-family-day-coordinator": ["Weather", "Elite Longterm Memory", "Caldav Calendar"],
    }

    for skill_name in SELECTED_SKILLS:
        raw = Path(f"src/opensquilla/skills/bundled/{skill_name}/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "clawhub_top100_composition:" in raw
        assert "Top ClawHub Skills" in raw
        for component in expectations[skill_name]:
            assert component in raw


def test_lifestyle_prompts_are_conversational_and_realistic() -> None:
    prompts = [case.prompt for case in LIFESTYLE_COMPARISON_CASES]

    assert all("benchmark:" not in prompt.lower() for prompt in prompts)
    assert all("OpenSquilla" not in prompt and "OpenClaw" not in prompt for prompt in prompts)
    assert any("爸妈" in prompt for prompt in prompts)
    assert any("幼儿园" in prompt for prompt in prompts)
    assert any("报价" in prompt or "供应商" in prompt for prompt in prompts)
    assert any("今天" in prompt for prompt in prompts)
    assert any("能不能装" in prompt for prompt in prompts)
    assert all("example.invalid" not in prompt for prompt in prompts)
    assert all("manifest" not in prompt.lower() for prompt in prompts)
    assert any("孩子老师昨天 16:20" in prompt for prompt in prompts)


def _bundled_meta_plan(skill_name: str, tmp_path: Path):
    loader = SkillLoader(
        bundled_dir=Path("src/opensquilla/skills/bundled"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    spec = loader.get_by_name(skill_name)
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    return plan


def test_lifestyle_meta_skills_do_not_clarify_when_intake_found_no_missing_fields(
    tmp_path: Path,
) -> None:
    examples = {
        "meta-document-to-decision": """
DOCUMENT_TYPES:
  - pasted_text
SOURCES:
  - quote
DECISION_QUESTION: should we sign?
NEEDS_CLARIFICATION: yes
MISSING_FIELDS:
  - none
""",
        "meta-daily-operator-brief": """
DATE_SCOPE: today
TIMEZONE: Asia/Shanghai
LOCATION: Shanghai
NEEDS_CLARIFICATION: yes
MISSING_FIELDS:
  - none
""",
        "meta-family-day-coordinator": """
DATE_SCOPE: tomorrow
LOCATION: Hangzhou
FIXED_EVENTS:
  - pickup at 17:00
NEEDS_CLARIFICATION: yes
MISSING_FIELDS:
  - none
""",
    }

    for skill_name, intake in examples.items():
        plan = _bundled_meta_plan(skill_name, tmp_path)
        clarify = next(step for step in plan.steps if step.id == "clarify")

        assert evaluate_when(
            clarify.when,
            inputs={},
            outputs={"intake": intake},
        ) is False


def test_lifestyle_meta_skills_handle_missing_memory_skill_with_failover(
    tmp_path: Path,
) -> None:
    expectations = {
        "meta-daily-operator-brief": ("memory_recall", "memory_recall_fallback"),
        "meta-family-day-coordinator": ("family_memory", "family_memory_fallback"),
    }

    for skill_name, (step_id, fallback_id) in expectations.items():
        plan = _bundled_meta_plan(skill_name, tmp_path)
        memory_step = next(step for step in plan.steps if step.id == step_id)
        fallback_step = next(step for step in plan.steps if step.id == fallback_id)

        assert memory_step.on_failure == fallback_id
        assert fallback_step.kind == "llm_chat"


def test_document_decision_pasted_text_path_does_not_wait_on_substitute_fallbacks(
    tmp_path: Path,
) -> None:
    plan = _bundled_meta_plan("meta-document-to-decision", tmp_path)
    step_by_id = {step.id: step for step in plan.steps}

    assert "pasted_text_extract" in step_by_id

    risk_review = step_by_id["risk_review"]
    assert "pasted_text_extract" in risk_review.depends_on
    assert "pdf_extract" in risk_review.depends_on
    assert "docx_extract" in risk_review.depends_on
    assert "xlsx_extract" in risk_review.depends_on
    assert "pdf_extract_fallback" not in risk_review.depends_on
    assert "docx_extract_fallback" not in risk_review.depends_on
    assert "xlsx_extract_fallback" not in risk_review.depends_on


def test_document_decision_prompt_prevents_false_overdue_and_fake_export() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-document-to-decision/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "payment deadline" in raw
    assert "overdue" in raw
    assert "upcoming/待确认" in raw
    assert "payment due" in raw
    assert "cancellation window has already passed" in raw
    assert "create, save, export, download, or attach a file" in raw
    assert "no workflow commentary" in raw
    assert "no meta-skill" in raw
    assert "exact reply deadlines" in raw
    assert "Do not cite statutes" in raw


def test_document_decision_never_derives_cancel_window_from_payment_deadline() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-document-to-decision/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Do not derive cancellation deadlines by subtracting days from invoice or payment due dates" in raw
    assert "If the contract end date or renewal effective date is missing" in raw
    assert "cancellation deadline unknown" in raw
    assert "avoid saying the notice window has passed" in raw
    assert "one-paragraph boss-forwardable summary" in raw
    assert "sign / negotiate first / reject" in raw
    assert "Do not speculate that the notice period may already be too short" in raw


def test_daily_operator_brief_hides_runtime_failures_and_clears_small_debts() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-daily-operator-brief/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Never expose raw tool/runtime failure details" in raw
    assert "Do not mention HTTP status codes, API failures, connector stack traces, or search errors" in raw
    assert "When live data is unavailable, summarize only the user-facing limit" in raw
    assert "Clear one-minute social debts before deep work when they unblock other people" in raw
    assert "include ready-to-send message drafts" in raw
    assert "overdue school, caregiver, vendor, HR, finance, or customer replies" in raw
    assert "clear them in the first 15 minutes" in raw
    assert "teacher / 老师 replies that were sent yesterday must be cleared in the first 15 minutes" not in raw
    assert "Do not rely on remembered or previous-day weather" in raw
    assert "live weather not verified" in raw
    assert "ready-to-send drafts for named recipients or roles" in raw
    assert "examples include school, caregiver, HR, finance, customer, vendor, and quote replies" in raw
    assert "drafts for teacher, HR, finance, customer, and quote replies" not in raw


def test_safe_skill_installer_uses_valid_isolated_audit_commands() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-safe-skill-installer/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Never show invalid container flags such as --network=host=NO" in raw
    assert "Use --network=none for offline container inspection" in raw
    assert "download without executing" in raw
    assert "do not run bash install.sh until static review passes" in raw
    assert "Do not say the domain is unreachable" in raw
    assert "Keep the final answer concise enough to finish in one turn" in raw
    assert "Do not include a long incident-response runbook unless the user says it was already installed" in raw
    assert "For not-yet-installed tools, keep rollback to pre-install backup and what to rotate if accidentally run" in raw


def test_safe_skill_installer_keeps_source_evidence_boundaries_upstream() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-safe-skill-installer/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Apply this evidence boundary in every step, not only the final answer" in raw
    assert "Search failure, missing lookup output, or absent public reputation is not proof of NXDOMAIN" in raw
    assert "Do not call a domain dead, unreachable, unresolvable, NXDOMAIN, or verified-bad" in raw
    assert "Source inspection must pass through only visible evidence and unknowns" in raw
    assert "Vetting and backup steps must not add new reputation or DNS claims" in raw
    assert "Download commands are for isolated audit only and must not be run on the real machine" in raw
    assert "Do not expose tool parameter errors, fetch failures, connector wording, or internal audit mechanics" in raw
    assert "Do not claim zero public footprint, no GitHub record, or no security-community record" in raw
    assert "unless the output cites the checked sources or says these checks were not verified" in raw


def test_lifestyle_meta_skills_have_natural_language_activation_cues() -> None:
    expectations = {
        "meta-document-to-decision": ["供应商续费", "要不要签"],
        "meta-daily-operator-brief": ["今天先帮我排一下", "前三优先级"],
        "meta-safe-skill-installer": ["这个插件能不能装", "curl -fsSL"],
    }

    for skill_name, cues in expectations.items():
        raw = Path(f"src/opensquilla/skills/bundled/{skill_name}/SKILL.md").read_text(
            encoding="utf-8"
        )
        for cue in cues:
            assert cue in raw


def test_family_day_coordinator_prioritizes_realistic_parent_schedule() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-family-day-coordinator/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "tonight / before-bed prep pass" in raw
    assert "Never compress errands into the" in raw
    assert "pre-dropoff window" in raw
    assert "dropoff first" in raw
    assert "live weather was not verified" in raw
    assert "without runtime/tool chatter" in raw
    assert "Allergy guidance should be operational" in raw


def test_safe_skill_installer_avoids_unverified_external_lookup_claims() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-safe-skill-installer/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "source_lookup_fallback" in raw
    assert "external source not verified / 外部来源未验证" in raw
    assert "Do not claim that" in raw
    assert "Never say the domain is unreachable" in raw
    assert "one-line threat model" in raw
    assert "immediate action plan" in raw
    assert "isolated audit recipe" in raw
    assert "what evidence would change the verdict" in raw
    assert "short message the user can send back" in raw
    assert "credential/key rotation" in raw
    assert "safer alternatives" in raw
    assert "infer that a project is not well-known" in raw
    assert "default to reject" in raw


def test_lifestyle_prompts_have_english_equivalents_without_benchmark_jargon() -> None:
    rows = build_lifestyle_rows("en")
    prompts = [row["case"]["prompt"] for row in rows]

    assert all(row["case"]["case_id"].endswith("_en") for row in rows)
    assert all("benchmark:" not in prompt.lower() for prompt in prompts)
    assert all("meta-skill" not in prompt.lower() for prompt in prompts)
    assert all("OpenSquilla" not in prompt and "OpenClaw" not in prompt for prompt in prompts)
    assert any("My parents are going to Japan" in prompt for prompt in prompts)
    assert any("daily report helper" in prompt for prompt in prompts)
    assert any("messaged yesterday at 16:20" in prompt for prompt in prompts)
    assert all("example.invalid" not in prompt for prompt in prompts)


def test_lifestyle_rubrics_reward_meta_specific_artifacts() -> None:
    for case in LIFESTYLE_COMPARISON_CASES:
        assert len(case.rubric) >= 5
        assert case.failure_modes
        assert "Squilla Router" in case.expected_advantage
        assert "Opus 4.7" in case.expected_advantage
        assert "If OpenSquilla does not beat OpenClaw" in case.optimization_if_not_better


def test_lifestyle_score_rewards_strong_answers_over_t3_generic_answers() -> None:
    weak = "可以，建议你按优先级处理。我会列一个简短计划。"
    strong_by_case = {
        "document_vendor_decision": """
        Bottom-line recommendation: negotiate before signing.
        Evidence table: source Quote A, source contract excerpt, due date 2026-06-03,
        amount RMB 18,600, auto-renewal obligation, cancellation penalty.
        Risks ranked high/medium/low. Questions to ask vendor. Next 24 hours.
        Professional-review caveat.
        """,
        "web_research_parent_esim": """
        Assumptions / Decision Context: parents travel to Japan for 8 days.
        Recommendation: buy a travel eSIM plus backup roaming day pass.
        Five Key Findings with sources [S1] [S2] and URL https://example.com.
        Practical Risks / Tradeoffs: activation, hotspot, support, refund.
        Evidence Limits. Next Steps. Sources.
        """,
        "daily_operator_morning_plan": """
        Top 3 priorities. Calendar/task risks. Weather/commute implications.
        Follow up with Li and finance. Time blocks 09:00, 11:00, 15:00.
        Missing connector/data limits. Optional reminders.
        """,
        "safe_skill_install_audit": """
        VERDICT: audit only.
        Risk table: file access, shell, network, secrets, persistence.
        Suspicious patterns: curl installer, reads ~/.ssh, postinstall.
        Required manual checks. Backup/rollback plan. Unknowns that block trust.
        """,
        "family_school_errand_day": """
        Time-blocked family plan. Pickup/dropoff/errand checklist.
        Weather adjustments. Meal/health/sleep/hydration notes.
        Remind teacher and dad. Optional reminder schedule. Missing data limits.
        """,
    }

    for case in LIFESTYLE_COMPARISON_CASES:
        assert score_response(strong_by_case[case.case_id], case).total > score_response(weak, case).total


def test_lifestyle_report_labels_openclaw_t3_opus_baseline() -> None:
    rows = build_lifestyle_rows()
    markdown = render_lifestyle_markdown(rows)
    prompts = render_lifestyle_prompts_markdown(rows)

    assert "# OpenSquilla Meta-Skills vs OpenClaw t3 Matched-Skills Lifestyle Benchmark" in markdown
    assert "OpenSquilla + Squilla Router" in markdown
    assert "OpenClaw + t3 + capability-equivalent normal skills baseline" in markdown
    assert "multi-search-engine" in markdown
    assert "pdf-toolkit" in markdown
    assert "docx -> OpenClaw word-docx" in markdown
    assert "deep-research -> OpenClaw deep-research-pro" in markdown
    assert "anthropic/claude-opus-4.7" in markdown
    assert "# Lifestyle Test Prompts" in prompts
    assert "OpenSquilla" not in prompts
    assert "OpenClaw" not in prompts
    assert "Benchmark constraints" not in prompts
    assert "Meta-skill:" not in prompts
    assert "Expected advantage:" not in prompts
    assert all(row["openclaw"]["model"] == "anthropic/claude-opus-4.7" for row in rows)


def test_lifestyle_report_surfaces_models_and_judge_scores() -> None:
    rows = build_lifestyle_rows()
    row = rows[0]
    row["opensquilla"]["model"] = "deepseek/deepseek-v4-flash-20260423"
    row["openclaw"]["model"] = OPENCLAW_T3_MODEL
    row["score_basis"] = "llm_judge"
    row["winner"] = "opensquilla"
    row["judge"] = {
        "scores": {"opensquilla": 91, "openclaw": 87},
        "confidence": 0.81,
        "rationale": "OpenSquilla has the better final artifact.",
        "raw": {
            "subscores": {
                "opensquilla": {"final_artifact_quality": 38},
                "openclaw": {"final_artifact_quality": 34},
            }
        },
    }

    markdown = render_lifestyle_markdown(rows)

    assert "OpenSquilla model" in markdown
    assert "Judge 0-100" in markdown
    assert "Final artifact" in markdown
    assert "deepseek/deepseek-v4-flash-20260423" in markdown
    assert "91-87" in markdown
    assert "38-34" in markdown


def test_lifestyle_judge_result_requires_subscores_and_rationale() -> None:
    incomplete = JudgeResult(
        winner="openclaw",
        scores={"opensquilla": 78, "openclaw": 83},
        confidence=0.0,
        rationale="",
        risks=[],
        raw={"opensquilla": 78, "openclaw": 83},
        model="judge-model",
    )
    complete = JudgeResult(
        winner="opensquilla",
        scores={"opensquilla": 91, "openclaw": 87},
        confidence=0.8,
        rationale="OpenSquilla has the better final artifact.",
        risks=[],
        raw={
            "subscores": {
                "opensquilla": {
                    "final_artifact_quality": 38,
                    "task_completion": 19,
                    "evidence_traceability": 14,
                    "actionability": 9,
                    "risk_boundary_safety": 8,
                    "meta_skill_fit": 3,
                },
                "openclaw": {
                    "final_artifact_quality": 34,
                    "task_completion": 18,
                    "evidence_traceability": 13,
                    "actionability": 8,
                    "risk_boundary_safety": 9,
                    "meta_skill_fit": 5,
                },
            }
        },
        model="judge-model",
    )

    assert _lifestyle_judge_result_is_complete(incomplete) is False
    assert _lifestyle_judge_result_is_complete(complete) is True


def test_lifestyle_judge_scores_are_recomputed_from_weighted_subscores() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="strong answer",
        score={"total": 1},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 1},
        model=OPENCLAW_T3_MODEL,
    )
    row = _compare_results(case, opensquilla, openclaw)
    judge = JudgeResult(
        winner="openclaw",
        scores={"opensquilla": 0, "openclaw": 100},
        confidence=0.9,
        rationale="OpenSquilla has the better weighted final artifact.",
        risks=[],
        raw={
            "subscores": {
                "opensquilla": {
                    "final_artifact_quality": 40,
                    "task_completion": 20,
                    "evidence_traceability": 15,
                    "actionability": 10,
                    "risk_boundary_safety": 10,
                    "meta_skill_fit": 5,
                },
                "openclaw": {
                    "final_artifact_quality": 30,
                    "task_completion": 20,
                    "evidence_traceability": 15,
                    "actionability": 10,
                    "risk_boundary_safety": 10,
                    "meta_skill_fit": 5,
                },
            }
        },
        model="judge-model",
    )

    updated = _apply_lifestyle_judge_result(row, judge, case)

    assert updated["judge"]["scores"] == {"opensquilla": 100, "openclaw": 90}
    assert updated["winner"] == "opensquilla"
    assert updated["judge"]["raw"]["score_source"] == "weighted_subscores"


def test_lifestyle_judge_retries_until_weighted_payload_is_complete() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="strong answer",
        score={"total": 1},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 1},
        model=OPENCLAW_T3_MODEL,
    )

    class FakeJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return JudgeResult(
                    winner="openclaw",
                    scores={"opensquilla": 10, "openclaw": 20},
                    confidence=0.1,
                    rationale="missing subscores",
                    risks=[],
                    raw={},
                    model="judge-model",
                )
            return JudgeResult(
                winner="openclaw",
                scores={"opensquilla": 10, "openclaw": 20},
                confidence=0.9,
                rationale="complete weighted payload",
                risks=[],
                raw={
                    "subscores": {
                        "opensquilla": {
                            "final_artifact_quality": 40,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                        "openclaw": {
                            "final_artifact_quality": 30,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                    }
                },
                model="judge-model",
            )

    fake = FakeJudge()
    result = asyncio.run(
        _judge_lifestyle_with_retries(fake, case, opensquilla, openclaw)  # type: ignore[arg-type]
    )

    assert fake.calls == 2
    assert result.scores == {"opensquilla": 100, "openclaw": 90}


def test_lifestyle_judge_existing_rejudges_jsonl_with_weighted_scores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    report = tmp_path / "existing.jsonl"
    row = _compare_results(
        case,
        EndpointResult(
            endpoint="opensquilla",
            case_id=case.case_id,
            ok=True,
            elapsed_s=1.0,
            response_text="strong answer",
            score={"total": 1},
        ),
        EndpointResult(
            endpoint="openclaw",
            case_id=case.case_id,
            ok=True,
            elapsed_s=1.0,
            response_text="baseline answer",
            score={"total": 1},
            model=OPENCLAW_T3_MODEL,
        ),
    )
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    captured: dict[str, list[dict]] = {}

    class FakeJudge:
        def __init__(self, **_kwargs) -> None:
            pass

        async def judge(self, *_args):
            return JudgeResult(
                winner="openclaw",
                scores={"opensquilla": 0, "openclaw": 100},
                confidence=0.9,
                rationale="complete weighted payload",
                risks=[],
                raw={
                    "subscores": {
                        "opensquilla": {
                            "final_artifact_quality": 40,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                        "openclaw": {
                            "final_artifact_quality": 30,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                    }
                },
                model="judge-model",
            )

    def fake_write(rows, stamp=None):
        captured["rows"] = rows
        return tmp_path / "out.jsonl", tmp_path / "out.md"

    monkeypatch.setattr(
        "scripts.compare_meta_skill_openclaw_lifestyle.LLMJudge",
        FakeJudge,
    )
    monkeypatch.setattr(
        "scripts.compare_meta_skill_openclaw_lifestyle.write_lifestyle_reports",
        fake_write,
    )
    args = type(
        "Args",
        (),
        {
            "judge_jsonl": str(report),
            "judge_model": "judge-model",
            "judge_api_key": "x",
            "judge_base_url": "http://judge",
            "judge_timeout": 1.0,
        },
    )()

    asyncio.run(judge_existing(args))

    judged = captured["rows"][0]
    assert judged["winner"] == "opensquilla"
    assert judged["judge"]["scores"] == {"opensquilla": 100, "openclaw": 90}


def test_lifestyle_comparison_marks_endpoint_failure_invalid_not_win() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=False,
        elapsed_s=1.0,
        response_text="",
        score={"total": 0},
        error="401 Missing Authentication header",
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "invalid"
    assert row["score_basis"] == "invalid_endpoint"
    assert row["opensquilla_better"] is False
    assert row["recommended_optimization"] is None


def test_lifestyle_comparison_marks_bootstrap_response_invalid_not_win() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="Bootstrap removed. Ready for the task — what would you like me to do?",
        score={"total": 0},
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "invalid"
    assert row["score_basis"] == "invalid_endpoint"
    assert row["opensquilla_better"] is False
    assert "openclaw: unrelated bootstrap response" in row["invalid_reasons"]


def test_lifestyle_comparison_only_scores_when_both_endpoints_are_valid() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 4},
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "opensquilla"
    assert row["score_basis"] == "deterministic"
    assert row["opensquilla_better"] is True


def test_opensquilla_runner_can_isolate_agent_per_case() -> None:
    runner = OpenSquillaRunner(
        "ws://example/ws",
        token=None,
        agent_id="main",
        isolated_agent_per_case=True,
        run_id="testrun",
    )
    first = runner._agent_id_for_case(LIFESTYLE_COMPARISON_CASES[0])
    second = runner._agent_id_for_case(LIFESTYLE_COMPARISON_CASES[1])

    assert first == "meta-compare-testrun-document-vendor-decision"
    assert second == "meta-compare-testrun-web-research-parent-esim"
    assert first != second
    assert first != "main"


def test_load_openclaw_baseline_refreshes_final_text_from_state(
    tmp_path: Path,
) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    state_dir = tmp_path / "openclaw"
    sessions_dir = state_dir / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_key = "agent:main:dashboard:test"
    session_file = sessions_dir / "abc.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"'
                + case.prompt
                + '"}]}}',
                '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"final openclaw baseline answer with 风险 证据表 24 小时"}]}}',
            ]
        ),
        encoding="utf-8",
    )
    (sessions_dir / "abc.trajectory.jsonl").write_text(
        f'{{"sessionKey":"{session_key}"}}\n',
        encoding="utf-8",
    )
    report = tmp_path / "baseline.jsonl"
    row = {
        "case": {"case_id": case.case_id, "prompt": case.prompt},
        "openclaw": {
            "endpoint": "openclaw",
            "case_id": case.case_id,
            "ok": True,
            "elapsed_s": 1.0,
            "response_text": "checking sources",
            "score": {"total": 0},
            "session_key": session_key,
            "model": OPENCLAW_T3_MODEL,
        },
    }
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    baseline = load_openclaw_baseline(report, [case], state_dir=state_dir)

    assert baseline[case.case_id].response_text.startswith("final openclaw baseline")
    assert baseline[case.case_id].ok is True


def test_load_openclaw_baseline_rejects_prompt_mismatch(tmp_path: Path) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    report = tmp_path / "baseline.jsonl"
    row = {
        "case": {"case_id": case.case_id, "prompt": "changed prompt"},
        "openclaw": {
            "endpoint": "openclaw",
            "case_id": case.case_id,
            "ok": True,
            "elapsed_s": 1.0,
            "response_text": "baseline answer",
            "score": {"total": 1},
            "model": OPENCLAW_T3_MODEL,
        },
    }
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        load_openclaw_baseline(report, [case])
    except SystemExit as exc:
        assert "baseline prompt mismatch" in str(exc)
    else:
        raise AssertionError("expected prompt mismatch to fail")
