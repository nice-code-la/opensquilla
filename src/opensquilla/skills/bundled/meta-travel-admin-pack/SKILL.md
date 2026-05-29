---
name: meta-travel-admin-pack
description: "Use this meta-skill instead of answering directly when the user needs to organize travel logistics, tickets, hotel confirmations, visa/passport reminders, reimbursement packets, packing lists, or trip admin documents through multi-skill orchestration."
kind: meta
meta_priority: 55
always: false
final_text_mode: "step:admin_pack"
triggers:
  - "travel admin"
  - "trip documents"
  - "整理出行材料"
  - "报销材料"
  - "机票酒店整理"
  - "出差材料"
  - "旅行清单"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities: [filesystem-read, filesystem-write, network]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You parse travel admin requests and preserve all file references."
        task: |
          Parse the travel admin contract.

          Request:
          {{ inputs.user_message | xml_escape | truncate(4000) }}

          Return exactly:
          TRIP_TYPE: <business|family|solo|group|unknown>
          DESTINATION: <destination or unknown>
          DATES: <dates or unknown>
          DOCUMENTS:
            - <pdf|email|image|docx|xlsx|pasted_text|unknown>
          OUTPUTS:
            - <checklist|expense_pack|itinerary_sheet|calendar_brief|packing_list>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <destination|dates|documents|none>
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "出行事务包需要目的地、日期或材料。"
        nl_extract: true
        fields:
          - name: destination
            type: string
            prompt: "目的地 / Destination"
            max_chars: 120
          - name: dates
            type: string
            prompt: "日期 / Dates"
            max_chars: 120
          - name: documents
            type: string
            required: true
            prompt: "材料路径、邮件摘录或说明 / Documents"
            max_chars: 2500
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: pdf_extract
      kind: skill_exec
      skill: pdf-toolkit
      depends_on: [intake, clarify]
      when: "'pdf' in (outputs.intake | lower)"
      on_failure: document_fallback
      with:
        task: "Extract itinerary, ticket, hotel, payment, passenger, and date fields from travel PDFs."
    - id: document_fallback
      kind: llm_chat
      with:
        system: "You extract travel admin facts from pasted text and filenames only."
        task: |
          Build a limited travel evidence packet from the request.
          {{ inputs.user_message | xml_escape | truncate(5000) }}
    - id: weather
      kind: skill_exec
      skill: weather
      depends_on: [intake, clarify]
      with:
        location: "{{ outputs.intake | truncate(300) }}"
        days: 3
    - id: admin_synthesis
      kind: llm_chat
      depends_on: [pdf_extract, document_fallback, weather]
      with:
        system: "You synthesize travel logistics and reimbursement readiness."
        task: |
          Create:
          - itinerary facts
          - document checklist
          - missing confirmations
          - packing/weather notes
          - reimbursement fields and receipt gaps
          - calendar reminders to add

          Intake:
          {{ outputs.intake | truncate(1000) }}
          PDF:
          {{ outputs.pdf_extract | truncate(4000) }}
          Fallback:
          {{ outputs.document_fallback | truncate(3000) }}
          Weather:
          {{ outputs.weather | truncate(1800) }}
    - id: admin_pack
      kind: llm_chat
      depends_on: [admin_synthesis]
      with:
        system: "You return travel admin packs that are ready to use."
        task: |
          Return:
          - trip snapshot
          - document checklist
          - day/time logistics table
          - packing and weather notes
          - reimbursement packet checklist
          - reminders to schedule
          - missing-data warnings

          Synthesis:
          {{ outputs.admin_synthesis | truncate(7000) }}
---

# Travel Admin Pack

Organizes travel documents, logistics, reminders, and reimbursement readiness.
