---
name: meta-weekly-life-review
description: "Use this meta-skill instead of answering directly when the user wants a weekly personal review, work-life retrospective, habit check, open-loop cleanup, memory-backed recap, or next-week plan through multi-skill orchestration."
kind: meta
meta_priority: 54
always: false
final_text_mode: "step:weekly_review"
triggers:
  - "weekly review"
  - "周复盘"
  - "本周复盘"
  - "下周计划"
  - "open loop cleanup"
  - "life review"
  - "这周做了什么"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities: [memory, scheduler]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You parse weekly review requests and infer sensible defaults."
        task: |
          Parse the weekly review contract.

          Request:
          {{ inputs.user_message | xml_escape | truncate(2500) }}

          Return exactly:
          WEEK_SCOPE: <this_week|last_week|explicit>
          REVIEW_AREAS:
            - <work|family|health|learning|money|projects|habits>
          SOURCE_STATUS: <memory_only|pasted_context|mixed|none>
          OUTPUT_STYLE: <compact|coach|manager|journal>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <week_scope|review_areas|none>
    - id: memory_recall
      kind: skill_exec
      skill: memory
      depends_on: [intake]
      with:
        query: "weekly review accomplishments blockers habits health family work open loops {{ outputs.intake | truncate(400) }}"
        max_results: 20
    - id: pasted_context_digest
      kind: skill_exec
      skill: summarize
      depends_on: [intake]
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(8000) }}"
        style: weekly_review_sources
        max_words: 1800
    - id: pattern_analysis
      kind: llm_chat
      depends_on: [memory_recall, pasted_context_digest]
      with:
        system: "You identify weekly patterns, wins, misses, open loops, and realistic next actions."
        task: |
          Analyze:
          - wins and completed work
          - unfinished loops
          - recurring blockers
          - energy/health/family signals if present
          - commitments for next week
          - memory gaps

          Intake:
          {{ outputs.intake | truncate(1000) }}
          Memory:
          {{ outputs.memory_recall | truncate(5000) }}
          Pasted context:
          {{ outputs.pasted_context_digest | truncate(3000) }}
    - id: next_week_plan
      kind: llm_chat
      depends_on: [pattern_analysis]
      with:
        system: "You design humane, realistic next-week plans."
        task: |
          Create a next-week plan with:
          - top 3 outcomes
          - stop doing / continue / start
          - concrete first actions
          - reminders worth scheduling
          - memory facts worth saving

          Analysis:
          {{ outputs.pattern_analysis | truncate(6000) }}
    - id: weekly_review
      kind: llm_chat
      depends_on: [pattern_analysis, next_week_plan]
      with:
        system: "You return weekly reviews that are specific, kind, and action-oriented."
        task: |
          Return:
          - one-paragraph recap
          - wins
          - missed/blocked items
          - lessons
          - next-week plan
          - open-loop checklist
          - memory update suggestions

          Pattern analysis:
          {{ outputs.pattern_analysis | truncate(5000) }}
          Next plan:
          {{ outputs.next_week_plan | truncate(4000) }}
---

# Weekly Life Review

Creates a memory-backed weekly review and next-week operating plan.
