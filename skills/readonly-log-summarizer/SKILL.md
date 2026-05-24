---
name: readonly-log-summarizer
description: Read, parse, and summarize log files (.log, .out, syslog, JSON-lines, build logs, CI pipeline logs, server logs, application traces) without modifying them. Trigger when the user asks to inspect, summarize, triage, or extract key events from a log file — including phrases like "summarize this log", "what went wrong in this build", "show me errors", "extract warnings", "find anomalies", "timeline of events", "triage this CI failure", "parse this log". Read-only — never modify the source files.
---

# Readonly Log Summarizer

## Overview

Read a log file, classify its format, extract structured data (timestamps, severity, error groupings), and produce a concise summary. Never modify the original file.

## Quick Start

### Plain-text logs

```python
import re
from collections import Counter

content = open("path/to/file.log").read()
lines = content.strip().split("\n")

severity = Counter()
for line in lines:
    for kw in ["ERROR", "WARN", "INFO", "DEBUG", "FATAL", "CRITICAL", "TRACE"]:
        if kw in line.upper():
            severity[kw] += 1
            break

print(f"Total lines: {len(lines)}")
print(f"Severity breakdown: {dict(severity)}")
```

### JSON-line logs

```python
import json

entries = [json.loads(line) for line in open("path/to/file.log") if line.strip()]
print(f"Parsed {len(entries)} JSON entries")
```

## Log Analysis Workflow

### Step 1: Detect format

Read the first 20-50 lines to classify:

| Format | Clue | Example |
|---|---|---|
| **Plain text** | Human-readable lines | `[2026-05-24 23:23:01] ERROR: Connection refused` |
| **JSON-lines** | Each line valid JSON | `{"time":"2026-05-24T23:23:01Z","level":"error","msg":"timeout"}` |
| **Syslog** | RFC 3164/5424 format | `May 24 23:23:01 hostname sshd[1234]: Failed password` |
| **Build log** | Mixed stdout/stderr, exit codes | `Step 5/10: RUN npm install` |
| **CSV/TSV** | Tabular with headers | `timestamp,level,message` |

For ambiguous formats, run `scripts/log_parser.py detect <path>`.

### Step 2: Extract structured data

**Structured logs (JSON-lines, CSV):** Parse all lines into records; group by severity; sort chronologically.

**Semi-structured (syslog, bracket timestamps):** Extract with regex: timestamp, severity, component, message.

```python
TIMESTAMP_SEV = re.compile(
    r'\[?(\d{4}[-\/]\d{2}[-\/]\d{2}[ T]\d{2}:\d{2}:\d{2})\]?\s*(\w+)?\s*(.+)'
)
```

See `references/log_patterns.md` for specialized format patterns (syslog, Apache, Nginx, Docker, K8s, pytest, Gradle, CMake, stack traces, CI pipelines).

**Unstructured (build output, stack traces):** Keyword-match error/anomaly lines; group multi-line stack traces into single events.

### Step 3: Produce the summary

```
## Log Summary: <filename>

**File info:** <path>, <size>, <line count>, <date range>

**Severity breakdown:** Errors: N, Warnings: N, Info: N

**Errors (most recent / most severe first):**
- <line ref> <message>
...

**Anomalies detected:**
- <repeated errors, sudden silence, pattern breaks>
```

For logs >10k lines, use `scripts/log_parser.py summarize <path>` for structured JSON without loading everything into context.

## Batch Comparison

When comparing multiple logs (before/after deploy, across servers):

1. Summarize each independently
2. Identify overlapping error signatures
3. Highlight unique errors per log
4. Produce side-by-side comparison if timestamps align

## Key Tools

- **`scripts/log_parser.py`** — Auto-detect format, summarize, parse, extract errors, build timelines.
  - `detect <path>` — Detect log format
  - `summarize <path>` — Structured JSON summary (good for large files)
  - `parse <path>` — Tabular record dump
  - `errors <path>` — Error/fatal/critical lines only
  - `timeline <path>` — Chronological event timeline
- **`references/log_patterns.md`** — Comprehensive regex patterns for syslog, Apache, Nginx, Docker, K8s, pytest, Gradle, CMake, Java stack traces, CI pipelines. Load when dealing with specialized log formats.
