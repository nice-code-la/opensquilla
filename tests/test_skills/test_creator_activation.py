from __future__ import annotations

from opensquilla.skills.creator.activation import evaluate_candidate_activation

CANDIDATE_SKILL_MD = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
kind: meta
meta_priority: 50
triggers:
  - "alpha report"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""


def test_activation_gate_passes_positive_and_catalog_adjacent_negative() -> None:
    result = evaluate_candidate_activation(
        CANDIDATE_SKILL_MD,
        positive_prompts=["please run the alpha report"],
        negative_prompts=["please run the beta digest"],
    )

    assert result["required"] is True
    assert result["passed"] is True
    assert result["reason"] == "ok"
    assert result["true_positive_rate"] == 1.0
    assert result["false_positive_count"] == 0
    assert result["issues"] == []
    assert [case["matched"] for case in result["cases"]] == [True, False]


def test_activation_gate_fails_false_positive() -> None:
    result = evaluate_candidate_activation(
        CANDIDATE_SKILL_MD,
        positive_prompts=["please run the alpha report"],
        negative_prompts=["run the alpha report but only explain the name"],
    )

    assert result["passed"] is False
    assert result["reason"] == "activation_failed"
    assert result["true_positive_rate"] == 1.0
    assert result["false_positive_count"] == 1
    assert "false_positive:run the alpha report but only explain the name" in result[
        "issues"
    ]


def test_activation_gate_fails_when_positive_rate_is_below_threshold() -> None:
    result = evaluate_candidate_activation(
        CANDIDATE_SKILL_MD,
        positive_prompts=[
            "please run the alpha report",
            "please run the beta digest",
        ],
        negative_prompts=[],
        threshold=0.8,
    )

    assert result["passed"] is False
    assert result["true_positive_rate"] == 0.5
    assert "true_positive_rate_below_threshold:0.50<0.80" in result["issues"]


def test_activation_gate_fails_malformed_skill_metadata_without_raising() -> None:
    result = evaluate_candidate_activation(
        "---\nname: [broken\n---\n",
        positive_prompts=["please run the alpha report"],
        negative_prompts=["please run the beta digest"],
    )

    assert result["passed"] is False
    assert result["reason"] == "invalid_skill_metadata"
    assert result["true_positive_rate"] == 0.0
    assert result["false_positive_count"] == 0
    assert result["cases"] == []
    assert result["issues"]
