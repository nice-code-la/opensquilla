---
name: meta-personal-finance-radar
description: "Use this meta-skill instead of answering directly when the user wants a personal finance, watchlist, stock, prediction-market, or portfolio radar that needs multi-skill orchestration across market search, watchlist memory, spreadsheet tracking, risk synthesis, and optional recurring reports."
kind: meta
meta_priority: 58
always: false
final_text_mode: "step:finance_radar"
triggers:
  - "finance radar"
  - "portfolio radar"
  - "stock watchlist"
  - "投资雷达"
  - "股票观察"
  - "持仓分析"
  - "市场异动"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [network, filesystem-write, memory]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You parse personal finance radar requests and avoid giving personalized financial advice."
        task: |
          Parse the finance radar contract.

          Request:
          {{ inputs.user_message | xml_escape | truncate(3000) }}

          Return exactly:
          ASSETS:
            - <ticker/name/market or unknown>
          MODE: <watchlist|portfolio_review|market_scan|recurring_digest>
          HORIZON: <intraday|weekly|monthly|long_term|unknown>
          HAS_POSITIONS: <yes|no|unknown>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <assets|objective|none>
          RISK_NOTE: not financial advice
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "投资雷达需要至少知道关注资产或目标。"
        nl_extract: true
        fields:
          - name: assets
            type: string
            required: true
            prompt: "关注的股票/基金/币/市场 / Assets"
            max_chars: 300
          - name: objective
            type: string
            prompt: "目标 / Objective"
            max_chars: 240
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: memory_watchlist
      kind: skill_exec
      skill: memory
      depends_on: [intake, clarify]
      with:
        query: "personal finance watchlist holdings risk preferences {{ outputs.intake | truncate(400) }}"
        max_results: 8
    - id: market_search
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [intake, clarify]
      with:
        query: "{{ outputs.intake | truncate(320) }} earnings price news analyst risk market"
        engines: [duckduckgo, brave]
        max_results: 18
    - id: spreadsheet_snapshot
      kind: skill_exec
      skill: xlsx
      depends_on: [intake, clarify]
      when: "'.xlsx' in inputs.user_message or 'spreadsheet' in (inputs.user_message | lower) or '表格' in inputs.user_message"
      with:
        task: "Inspect the portfolio/watchlist spreadsheet mentioned by the user and summarize holdings, weights, dates, and missing fields."
    - id: risk_synthesis
      kind: llm_chat
      depends_on: [memory_watchlist, market_search, spreadsheet_snapshot]
      with:
        system: "You synthesize finance radar briefs with evidence and non-advice boundaries."
        task: |
          Create a finance radar:
          - market/news drivers
          - watchlist changes
          - risks and catalysts
          - questions to verify
          - what changed since memory/spreadsheet if available
          - no buy/sell commands; frame as research and risk review

          Intake:
          {{ outputs.intake | truncate(1000) }}
          Memory:
          {{ outputs.memory_watchlist | truncate(2500) }}
          Search:
          {{ outputs.market_search | truncate(6000) }}
          Spreadsheet:
          {{ outputs.spreadsheet_snapshot | truncate(3000) }}
    - id: finance_radar
      kind: llm_chat
      depends_on: [risk_synthesis]
      with:
        system: "You return concise personal finance radar reports."
        task: |
          Return:
          - top alerts
          - watchlist table
          - risk/catalyst matrix
          - evidence links
          - portfolio tracking fields to update
          - recurring-report suggestion if requested
          - clear non-advice disclaimer

          Synthesis:
          {{ outputs.risk_synthesis | truncate(7000) }}
---

# Personal Finance Radar

Builds a research-oriented finance and watchlist radar with explicit
non-advice boundaries.
