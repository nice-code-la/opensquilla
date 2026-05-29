---
name: meta-web-research-to-report
description: "Use this meta-skill instead of answering directly when the user needs a cited research report, market/technical briefing, or source-backed writeup that benefits from multi-skill orchestration across preference inference, web research, drafting, quality review, and export."
kind: meta
meta_priority: 80
always: false
final_text_mode: "step:final_report"
triggers:
  - "调研报告"
  - "research report"
  - "写一份报告"
  - "write up the findings"
  - "source-backed writeup"
  - "technical briefing"
  - "market briefing"
  - "cited report"
  - "查一下并写报告"
  - "查一下并写"
  - "决策 memo"
  - "决策备忘"
  - "来源、关键发现"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities: [network, filesystem-write]
    clawhub_top100_composition:
      - skill: "Multi Search Engine"
        local_skill: multi-search-engine
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 11
        role: "Gather current multi-engine sources before drafting."
      - skill: "Deep Researcher / deep research family"
        local_skill: deep-research
        rank_source: "ClawHub research-skill family, verified via current search results"
        role: "Run deeper source-backed research for long reports."
      - skill: "Word / DOCX"
        local_skill: docx
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 28
        role: "Export polished report artifacts when requested."
composition:
  steps:
    - id: preferences
      kind: llm_chat
      with:
        system: "You infer report requirements. Return only the requested contract."
        task: |
          Infer the report contract from the request. If details are missing,
          choose conservative defaults and mark them as assumptions instead of
          asking follow-up questions. Set NEEDS_CLARIFICATION: yes only when the topic is too broad
          to search usefully, or when the user asks for a decision-support
          report but the audience or decision context is missing. Do not ask
          for citation style, length, or language when a conservative default
          works.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          SEARCH_QUERY: <short web-search query, no benchmark/control instructions>
          AUDIENCE: <reader>
          REPORT_TYPE: <technical|market|policy|general>
          TARGET_LENGTH: <short|standard|long>
          LANGUAGE: <language>
          CITATION_STYLE: <inline links|footnotes|bibliography>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <topic|audience|decision_context|none>
          CLARIFY_REASON: <one concise reason, or none>
          ASSUMPTIONS:
            - <assumption>
    - id: report_clarify
      kind: user_input
      depends_on: [preferences]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.preferences"
      clarify:
        mode: form
        intro: |
          报告主题或决策场景还不够明确。请补齐最小信息，我再继续检索和写作。
        nl_extract: true
        fields:
          - name: topic
            type: string
            required: true
            prompt: "报告主题 / Report topic"
            max_chars: 240
          - name: audience
            type: string
            required: true
            prompt: "读者或受众 / Audience"
            max_chars: 160
          - name: decision_context
            type: string
            required: true
            prompt: "要支持的决策或使用场景 / Decision context"
            max_chars: 300
          - name: source_preferences
            type: string
            prompt: "偏好的来源或范围 / Preferred sources or scope"
            max_chars: 300
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: report_mode
      kind: llm_classify
      depends_on: [preferences, report_clarify]
      output_choices:
        - QUICK_DECISION_MEMO
        - DEEP_REPORT
        - EXPORT_DOCX
      with:
        text: |
          Classify the report request.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Preferences:
          {{ outputs.preferences | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('report_clarify', {}) | tojson }}

          Decision rules:
          - QUICK_DECISION_MEMO: user wants a concise answer, quick brief,
            compact research report, planning-meeting memo, comparison memo,
            CTO/leadership decision aid, or explicitly says compact/concise/
            paste into a decision memo. Prefer this even when the phrase
            "research report" appears.
          - DEEP_REPORT: user wants a comprehensive, long, detailed, or
            multi-round source-backed report/briefing/writeup and did not
            explicitly request a file export.
          - EXPORT_DOCX: user explicitly asks for a Word/docx/file/report
            artifact export.
    - id: search
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [preferences, report_clarify, report_mode]
      with:
        query: "{{ outputs.preferences | truncate(180) }} {{ inputs.get('collected', {}).get('report_clarify', {}) | tojson | truncate(180) }}"
        engines: [brave, tavily, duckduckgo]
        max_results: 20
    - id: source_quality
      kind: llm_chat
      depends_on: [search]
      with:
        system: "You curate search results for cited report writing. Be selective and source-aware."
        task: |
          Rank and deduplicate these web results for report writing.
          Prefer primary sources, official docs, reputable publications, and
          recent sources when the topic is time-sensitive. Remove low-quality
          SEO pages and repeated mirrors.

          Report preferences:
          {{ outputs.preferences | truncate(1200) }}

          Search results:
          {{ outputs.search | truncate(8000) }}

          Return a concise numbered source pack with 8-15 sources. For each
          source, include:
          [S#] Title — URL
          Credibility: <why this source is usable>
          Supports: <specific claim(s)>
          Evidence type: <direct|indirect|background>

          If a search result lacks a URL, omit it. Do not create placeholder
          source numbers. Prefer fewer credible sources over many weak ones.
          For quick decision memos, prioritize the best 5-8 sources over a
          long source list. Prefer official/vendor docs, standards, primary
          surveys, reputable engineering publications, and current release
          notes. Avoid Reddit, anonymous forums, content farms, and generic SEO
          listicles as primary evidence; include them only as background or
          evidence-limit notes when no stronger source exists.
          Label secondary roundup/blog/stat pages as indirect/background unless
          they are the source of the specific statistic being used. Do not let
          indirect sources support causal, benchmark, cost, or adoption claims
          without an explicit caveat.
    - id: research
      skill: deep-research
      depends_on: [source_quality]
      when: "outputs.report_mode in ('DEEP_REPORT', 'EXPORT_DOCX')"
      with:
        question: "{{ inputs.user_message | xml_escape | truncate(512) }}"
        sources: "{{ outputs.source_quality }}"
        rounds: 2
    - id: outline
      kind: llm_chat
      depends_on: [source_quality, research]
      with:
        system: "You design concise, evidence-backed report outlines."
        task: |
          Create a report outline before drafting. The outline must match the
          audience, report type, and target length below. Include sections for
          executive summary, key findings, evidence, risks/limits, and source
          list unless the user explicitly requested another structure.

          Preferences:
          {{ outputs.preferences | truncate(1200) }}

          Report mode:
          {{ outputs.report_mode }}

          Source pack:
          {{ outputs.source_quality | truncate(4000) }}

          Research:
          {{ outputs.research | truncate(8000) }}
    - id: report_draft
      skill: summarize
      depends_on: [outline]
      with:
        text: "Report mode:\n{{ outputs.report_mode }}\n\nPreferences:\n{{ outputs.preferences }}\n\nOutline:\n{{ outputs.outline }}\n\nSource pack:\n{{ outputs.source_quality }}\n\nResearch:\n{{ outputs.research }}"
        style: cited_report
        max_words: 3500
    - id: source_to_claim
      kind: llm_chat
      depends_on: [report_draft, source_quality]
      with:
        system: "You audit report claims against source packs."
        task: |
          Build a concise source-to-claim map for the draft. Keep only
          claims that are supported by the source pack or explicitly mark a
          caveat. Do not add process commentary.
          Every mapped claim must cite one or more existing source IDs from
          the source pack, e.g. [S1], [S3]. Do not cite source numbers that
          are absent from the source pack.
          For each major claim, mark SUPPORT as DIRECT, INDIRECT, or
          INFERENCE. Claims with INDIRECT/INFERENCE support must be phrased as
          caveats or removed from the final recommendation.

          Source pack:
          {{ outputs.source_quality | truncate(6000) }}

          Draft:
          {{ outputs.report_draft | truncate(8000) }}
    - id: quality_gate
      kind: llm_chat
      depends_on: [report_draft, source_quality, source_to_claim]
      with:
        system: "You polish final reports and remove process commentary."
        task: |
          Review the report draft for artifact readiness. Verify:
          - every major claim has a source or clear caveat
          - source list contains credible URLs
          - executive summary and limitations are present
          - output is in the requested language
          - claims marked INDIRECT or INFERENCE are not stated as direct facts

          If acceptable, return the polished report body. If not, repair it
          directly and return the repaired report body. Do not include process
          commentary.
          The repaired report must include a visible "Sources" or "Source
          list" section with source titles and URLs. If the draft references
          numbered sources, the same numbers must appear in that source list.
          Remove or caveat quantitative claims that are not mapped to a source.

          Source pack:
          {{ outputs.source_quality | truncate(4000) }}

          Source-to-claim map:
          {{ outputs.source_to_claim | truncate(4000) }}

          Draft:
          {{ outputs.report_draft | truncate(8000) }}
    - id: final_report
      kind: llm_chat
      depends_on: [quality_gate, source_quality, source_to_claim]
      with:
        system: "You produce the final user-facing report body."
        task: |
          Return the final report body only. Use the requested language and
          keep the report mode in mind:
          - QUICK_DECISION_MEMO: concise decision memo with bullets, sources,
            and caveats.
          - DEEP_REPORT: full cited report with executive summary, findings,
            evidence, limitations, and sources.
          - EXPORT_DOCX: same as DEEP_REPORT, suitable for DOCX export.

          Report mode:
          {{ outputs.report_mode }}

          Source pack:
          {{ outputs.source_quality | truncate(5000) }}

          Source-to-claim map:
          {{ outputs.source_to_claim | truncate(4000) }}

          Polished report:
          {{ outputs.quality_gate | truncate(10000) }}

          Final output contract:
          - For decision-memo requests, keep it compact and artifact-ready:
            use exactly these top-level sections in this order:
            Assumptions / Decision Context; Recommendation; Five Key Findings;
            Practical Risks / Tradeoffs; Evidence Limits; Next Steps; Sources.
            Keep the body under 900 words before the Sources section unless
            the user asks for a long report.
          - The Assumptions / Decision Context section must explicitly state
            the audience, decision being made, scope, and key assumptions from
            the preferences step. Do not omit this section when the user asks
            for assumptions, CTO context, planning context, or a decision memo.
          - The Five Key Findings section must contain exactly five numbered
            findings when the user asks for five. Each finding must cite at
            least one source ID or explicitly say "inference from [S#]" /
            "not directly proven by the source pack".
          - Treat the Source pack below as authoritative evidence input. If
            the Polished report says no sources were provided but the Source
            pack contains `[S#] Title — URL` entries, ignore that sentence and
            restore sources from the Source pack.
          - The Source list must copy title + URL entries from the Source pack
            verbatim or near-verbatim. Never output "No sources were provided"
            unless the Source pack is empty or contains no URLs.
          - Include a visible Source list with title + URL for every cited
            source ID. Do not cite [S#] or "Source #N" unless that source
            appears in the Source list.
          - Every non-obvious quantitative claim must be cited or explicitly
            marked as an assumption/inference. Remove invented cost, latency,
            benchmark, or model-quality numbers that are not supported by the
            source pack.
          - Key findings must cite only source IDs that appear in the Source
            list. When evidence is indirect, say "inference" or "not directly
            proven by the source pack" in the finding or limitations.
          - Add a short Evidence limits note when the source pack relies on
            secondary or indirect sources for strategic claims.
          - Do not use Reddit, anonymous forums, or generic listicles to support
            the final recommendation unless explicitly framed as anecdotal or
            background evidence. Prefer to omit them from Sources for quick
            decision memos when stronger sources exist.
          - If sources are weak or stale, say so in Limitations instead of
            overstating certainty.
          - Do not announce that a file was generated unless the user explicitly
            asked for DOCX/file export and the export step ran. For ordinary
            memo requests, the final chat reply must contain the complete memo
            body inline.
    - id: export
      skill: docx
      depends_on: [final_report]
      when: "outputs.report_mode == 'EXPORT_DOCX'"
      with:
        title: "{{ inputs.user_message | xml_escape | truncate(128) }}"
        body: "{{ outputs.final_report }}"
---

# Web Research to Report (Meta-Skill)

Produce a cited Word report from a single research question. The workflow
first derives the report contract, ranks sources, drafts from an outline, and
runs a readiness gate before exporting.

## Fallback

If the orchestrator fails, the LLM should manually drive each step using
the corresponding skill's SKILL.md as guidance.
