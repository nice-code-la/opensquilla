#!/usr/bin/env python3
"""
Log Parser — auto-detect format, summarize, parse, and extract errors from log files.

Usage:
    python3 log_parser.py detect <path>          # Detect log format
    python3 log_parser.py summarize <path>       # Structured JSON summary
    python3 log_parser.py parse <path>           # Parse into records (table)
    python3 log_parser.py errors <path>          # Extract error/fatal/critical lines
    python3 log_parser.py timeline <path>        # Timeline of events
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime


# ── Format detection ──────────────────────────────────────────────────────────

PATTERNS = {
    "json-lines": re.compile(r'^\s*\{.*\}\s*$'),
    "syslog-rfc3164": re.compile(
        r'^\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+'
    ),
    "syslog-rfc5424": re.compile(
        r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?\s+\S+'
    ),
    "iso-timestamp": re.compile(
        r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
    ),
    "bracket-timestamp": re.compile(
        r'^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'
    ),
    "apache-combined": re.compile(
        r'^\S+\s+\S+\s+\S+\s+\[.*\]\s+"[A-Z]+'
    ),
    "csv-header": re.compile(
        r'^(timestamp|time|date|level|severity|message|msg|event)\b',
        re.IGNORECASE,
    ),
    "gradle-build": re.compile(
        r'^>\s+Task\s+:|^:[A-Za-z]'
    ),
    "pytest": re.compile(
        r'^(PASSED|FAILED|ERROR|test_)'
    ),
}

SEVERITY_KEYWORDS = [
    "EMERGENCY", "FATAL", "CRITICAL", "ERROR", "WARNING", "WARN",
    "INFO", "DEBUG", "TRACE",
]

SEVERITY_RANK = {
    "EMERGENCY": 0, "FATAL": 1, "CRITICAL": 2, "ERROR": 3,
    "WARNING": 4, "WARN": 4, "INFO": 5, "DEBUG": 6, "TRACE": 7,
}


def detect_format(path: str) -> str:
    """Read first 50 lines and classify log format."""
    with open(path, errors="replace") as f:
        head = [line for line in (f.readline() for _ in range(50)) if line.strip()]

    if not head:
        return "empty"

    # JSON-lines check
    json_count = sum(1 for line in head if PATTERNS["json-lines"].match(line))
    if json_count >= max(3, len(head) * 0.5):
        return "json-lines"

    # CSV check — sniff header
    first = head[0].strip()
    if "," in first and PATTERNS["csv-header"].match(first):
        return "csv"
    if "\t" in first and PATTERNS["csv-header"].match(first):
        return "tsv"

    # Score each format
    scores = {"syslog-rfc3164": 0, "syslog-rfc5424": 0, "apache-combined": 0,
              "gradle-build": 0, "pytest": 0}
    for line in head:
        for fmt, pat in PATTERNS.items():
            if fmt in scores and pat.match(line):
                scores[fmt] += 1

    best = max(scores, key=scores.get)
    if scores[best] >= max(3, len(head) * 0.3):
        return best

    # Check for bracket timestamps
    bracket_count = sum(1 for line in head if PATTERNS["bracket-timestamp"].match(line))
    iso_count = sum(1 for line in head if PATTERNS["iso-timestamp"].match(line))
    if bracket_count >= 3:
        return "bracket-timestamp"
    if iso_count >= 3:
        return "iso-timestamp"

    # Has severity keywords?
    sev_count = sum(1 for line in head if any(k in line.upper() for k in SEVERITY_KEYWORDS))
    if sev_count >= 3:
        return "plain-text-severity"

    return "plain-text"


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_timestamp(line: str):
    """Extract first datetime from a line, return ISO string or None."""
    # ISO 8601
    m = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)', line)
    if m:
        return m.group(1)
    # Bracket timestamp
    m = re.search(r'\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)', line)
    if m:
        return m.group(1)
    # Syslog RFC 3164
    m = re.search(r'(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})', line)
    if m:
        return m.group(1)
    return None


def parse_severity(line: str):
    """Detect severity keyword in line."""
    upper = line.upper()
    for kw in SEVERITY_KEYWORDS:
        if kw in upper:
            return kw
    return None


def parse_record(line: str, line_no: int, fmt: str):
    """Parse a single log line into a record dict."""
    record = {"line": line_no, "raw": line.rstrip("\n")}
    if fmt == "json-lines":
        try:
            obj = json.loads(line.strip())
            record["structured"] = obj
            record["timestamp"] = obj.get("time") or obj.get("timestamp") or obj.get("@timestamp")
            record["severity"] = (obj.get("level") or obj.get("severity") or "").upper()
        except json.JSONDecodeError:
            pass
    else:
        record["timestamp"] = parse_timestamp(line)
        record["severity"] = parse_severity(line)
    return record


def parse_file(path: str):
    """Parse entire log file into records."""
    fmt = detect_format(path)
    records = []
    with open(path, errors="replace") as f:
        for i, line in enumerate(f, 1):
            if line.strip():
                records.append(parse_record(line.strip(), i, fmt))
    return fmt, records


# ── Summarization ────────────────────────────────────────────────────────────

def summarize(path: str) -> dict:
    """Produce structured summary of a log file."""
    fmt, records = parse_file(path)
    sev_counter = Counter()
    error_lines = []
    timestamps = []
    messages_by_sev = defaultdict(list)

    for rec in records:
        sev = rec.get("severity") or "OTHER"
        sev_counter[sev] += 1
        if sev in ("EMERGENCY", "FATAL", "CRITICAL", "ERROR"):
            error_lines.append(rec["raw"])
        if rec.get("timestamp"):
            timestamps.append(rec["timestamp"])

        # Try to extract meaningful message
        msg = None
        if "structured" in rec:
            msg = rec["structured"].get("message") or rec["structured"].get("msg")
        else:
            # Remove timestamp/severity prefix for cleaner message
            parts = re.split(r'\]\s+', rec["raw"], maxsplit=1)
            if len(parts) > 1:
                msg = parts[-1]
            else:
                msg = rec["raw"][:120]
        if sev in ("EMERGENCY", "FATAL", "CRITICAL", "ERROR"):
            messages_by_sev[sev].append(msg[:200])

    # Find top repeated error messages
    error_msg_counts = Counter(m for msgs in messages_by_sev.values() for m in msgs)
    top_errors = [{"message": m, "count": c} for m, c in error_msg_counts.most_common(10)]

    # Detect anomalies: repeated identical lines
    line_counter = Counter(rec["raw"] for rec in records)
    repeated = [{"line": l, "count": c} for l, c in line_counter.most_common(5) if c > 3]

    st = os.stat(path)
    result = {
        "file": path,
        "size_bytes": st.st_size,
        "total_lines": len(records),
        "format": fmt,
        "severity_breakdown": dict(sev_counter),
        "time_range": {"first": timestamps[0] if timestamps else None,
                        "last": timestamps[-1] if timestamps else None},
        "error_count": sum(sev_counter.get(s, 0) for s in ("EMERGENCY", "FATAL", "CRITICAL", "ERROR")),
        "warning_count": sev_counter.get("WARNING", 0) + sev_counter.get("WARN", 0),
        "top_errors": top_errors,
        "anomalies": {
            "repeated_lines": repeated,
            "empty_severity_count": sev_counter.get("OTHER", 0),
        },
    }
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_detect(paths):
    for p in paths:
        fmt = detect_format(p)
        print(f"{p}: {fmt}")


def cmd_summarize(paths):
    for p in paths:
        s = summarize(p)
        print(json.dumps(s, indent=2, default=str))
        print()


def cmd_parse(paths):
    for p in paths:
        fmt, records = parse_file(p)
        print(f"Format: {fmt}  |  Records: {len(records)}")
        print(f"{'Line':>6} {'Timestamp':<30} {'Severity':<10} Message")
        print("-" * 80)
        for rec in records[:50]:
            ts = rec.get("timestamp") or ""
            sev = rec.get("severity") or ""
            msg = rec["raw"][:80]
            print(f"{rec['line']:>6} {ts:<30} {sev:<10} {msg}")
        if len(records) > 50:
            print(f"  ... ({len(records) - 50} more records)")


def cmd_errors(paths):
    for p in paths:
        fmt, records = parse_file(p)
        count = 0
        for rec in records:
            sev = rec.get("severity") or ""
            if sev in ("EMERGENCY", "FATAL", "CRITICAL", "ERROR"):
                print(f"  L{rec['line']:>6} [{sev}] {rec['raw'][:200]}")
                count += 1
        if count == 0:
            print("  (no errors found)")
        print()


def cmd_timeline(paths):
    for p in paths:
        fmt, records = parse_file(p)
        timed = [rec for rec in records if rec.get("timestamp")]
        if not timed:
            print(f"{p}: no timestamps found")
            continue
        timed.sort(key=lambda r: r["timestamp"])
        print(f"Timeline: {p}")
        for rec in timed[:60]:
            ts = rec["timestamp"]
            msg = rec["raw"][:120]
            print(f"  {ts}  {msg}")
        if len(timed) > 60:
            print(f"  ... ({len(timed) - 60} more entries)")


HELP = """Usage: python3 log_parser.py <action> <path> [...]

Actions:
  detect    <path>     Detect log format
  summarize <path>     Produce JSON summary
  parse     <path>     Show parsed records
  errors    <path>     Show only error lines
  timeline  <path>     Chronological timeline of events
"""


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(HELP)
        sys.exit(0 if sys.argv[1:] and sys.argv[1] in ("-h", "--help") else 1)

    action = sys.argv[1]
    paths = sys.argv[2:]
    cmds = {
        "detect": cmd_detect,
        "summarize": cmd_summarize,
        "parse": cmd_parse,
        "errors": cmd_errors,
        "timeline": cmd_timeline,
    }
    fn = cmds.get(action)
    if not fn:
        print(f"Unknown action: {action}", file=sys.stderr)
        print(HELP, file=sys.stderr)
        sys.exit(1)

    for p in paths:
        if not os.path.isfile(p):
            print(f"File not found: {p}", file=sys.stderr)
            sys.exit(1)

    fn(paths)


if __name__ == "__main__":
    main()
