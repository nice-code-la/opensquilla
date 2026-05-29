---
name: meta-daily-operator-brief
description: "Use this meta-skill instead of answering directly when the user wants a practical daily command center, morning brief, priority list, or today plan that benefits from multi-skill orchestration across calendar/mail-style context, weather, news/search, memory, and optional scheduling."
kind: meta
meta_priority: 64
always: false
final_text_mode: "step:final_brief"
triggers:
  - "daily brief"
  - "morning brief"
  - "today plan"
  - "今天安排"
  - "今日简报"
  - "早上简报"
  - "今天先做什么"
  - "今天先帮我排一下"
  - "先帮我排一下"
  - "前三优先级"
  - "时间块"
  - "该跟进谁"
  - "客户 demo"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities: [network, memory, scheduler]
    clawhub_top100_composition:
      - skill: "Weather"
        local_skill: weather
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 10
        role: "Add weather and commute implications to the daily plan."
      - skill: "Multi Search Engine"
        local_skill: multi-search-engine
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 11
        role: "Scan current local/work context that may affect the day."
      - skill: "Elite Longterm Memory"
        local_skill: memory
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 35
        role: "Recall preferences, open loops, and recurring priorities."
      - skill: "Gog / Caldav Calendar / Notion"
        local_skill: "optional connector family"
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        role: "Connector targets to name as missing when not installed."
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You extract a daily operating brief contract without asking unless the date or timezone is unusable."
        task: |
          Parse the request into a practical daily brief contract. Treat pasted
          calendar, email, chat, task, or reminder text as source material.

          Request:
          {{ inputs.user_message | xml_escape | truncate(3000) }}

          Return exactly:
          DATE_SCOPE: <today|tomorrow|this_week|explicit>
          TIMEZONE: <timezone or ASSUMED: local>
          LOCATION: <city or ASSUMED: unknown>
          SOURCE_STATUS: <pasted_context|needs_external_connectors|mixed|none>
          OUTPUT_STYLE: <compact|detailed>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <date|timezone|none>
          ASSUMPTIONS:
            - <assumption>
          Set NEEDS_CLARIFICATION: no when the request, runtime timestamp, or
          pasted context gives a usable date scope and timezone. If you can
          assume local timezone from the timestamp, use ASSUMED: local and
          MISSING_FIELDS must be exactly "- none".
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake and '- none' not in outputs.intake"
      clarify:
        mode: form
        intro: "今天的简报缺少关键时间信息。补齐后我会继续整理优先级。"
        nl_extract: true
        fields:
          - name: date_scope
            type: string
            required: true
            prompt: "日期范围 / Date scope"
            max_chars: 80
          - name: timezone
            type: string
            prompt: "时区 / Timezone"
            max_chars: 80
          - name: location
            type: string
            prompt: "城市 / Location"
            max_chars: 80
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: memory_recall
      kind: skill_exec
      skill: memory
      depends_on: [intake, clarify]
      on_failure: memory_recall_fallback
      with:
        query: "daily priorities open loops preferences {{ outputs.intake | truncate(400) }}"
        max_results: 8
    - id: memory_recall_fallback
      kind: llm_chat
      with:
        system: "You produce a no-memory fallback note for a daily operating brief."
        task: |
          No runnable memory skill is available. Return a compact note that no
          stored preferences or open loops were read, then extract any recurring
          preferences or open loops only from the pasted request.

          Request:
          {{ inputs.user_message | xml_escape | truncate(3000) }}

          Intake:
          {{ outputs.intake | truncate(1000) }}
    - id: weather_check
      kind: skill_exec
      skill: weather
      depends_on: [intake, clarify]
      with:
        location: "{{ outputs.intake | truncate(400) }}"
        days: 2
    - id: context_digest
      kind: agent
      skill: sub-agent
      depends_on: [intake, clarify]
      with:
        task: |
          Build a compact digest from pasted calendar/email/task/chat context.
          If connector skills such as Gmail, CalDAV, Apple Reminders, Notion,
          Trello, or Slack are not installed, do not claim live access; use
          only the user's pasted context and name missing connector inputs.

          Request:
          {{ inputs.user_message | xml_escape | truncate(5000) }}

          Intake:
          {{ outputs.intake | truncate(1000) }}
    - id: news_scan
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [intake]
      with:
        query: "{{ outputs.intake | truncate(220) }} local commute weather market work news"
        engines: [duckduckgo, brave]
        max_results: 8
    - id: final_brief
      kind: llm_chat
      depends_on: [memory_recall, weather_check, context_digest, news_scan]
      with:
        system: "You produce concise daily operating briefs that users can act on immediately."
        task: |
          Produce the final daily brief in the user's language. Include:
          1. Top 3 priorities
          2. Calendar/task risks. Use the literal label "Risk / 风险 / 冲突"
             and name any schedule conflicts, delay risk, or impossible
             sequencing.
          3. Weather/commute implications
          4. Messages or people to follow up with
          5. Suggested time blocks
          6. Missing connector/data limits. Use the literal label "Data
             limits / 数据限制" and state "only pasted / 仅根据" when no live
             calendar, email, reminder, weather, or location connector data was
             actually read.
          7. Optional reminders worth scheduling
          Never expose raw tool/runtime failure details. Do not mention HTTP status codes, API failures, connector stack traces, or search errors.
          When live data is unavailable, summarize only the user-facing limit:
          for example, "live weather/calendar/email was not verified; check the
          relevant app before leaving." Do not narrate which tool failed.
          Clear one-minute social debts before deep work when they unblock other people,
          especially overdue school, caregiver, vendor, HR, finance, or customer replies
          that can be sent in under 5 minutes. Keep the top priorities focused
          on impact, but let the schedule clear these tiny blockers early.
          If an external reply was due yesterday or is blocking another
          person's planning, clear them in the first 15 minutes of the plan
          unless the user gives a stronger fixed conflict. For follow-ups,
          include ready-to-send message drafts when the user named recipients
          or obvious reply contexts. Include ready-to-send drafts for named recipients or roles;
          examples include school, caregiver, HR, finance, customer, vendor, and quote replies
          when enough context exists.
          Do not rely on remembered or previous-day weather. If live weather
          is unavailable, write "live weather not verified" and give generic
          commute buffers instead of citing stale weather facts.

          Intake:
          {{ outputs.intake | truncate(1200) }}
          Memory:
          {{ outputs.memory_recall | truncate(2500) }}
          Weather:
          {{ outputs.weather_check | truncate(1800) }}
          Context:
          {{ outputs.context_digest | truncate(4000) }}
          News/search:
          {{ outputs.news_scan | truncate(3000) }}
---

# Daily Operator Brief

Creates a practical daily command brief from available local context, memory,
weather, and pasted or connector-backed calendar/mail/task material.
