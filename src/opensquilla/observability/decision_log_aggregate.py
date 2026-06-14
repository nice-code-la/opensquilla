"""Decision-log aggregation primitives.

Pure functions over ``~/.opensquilla/logs/decisions-*.jsonl``: skill
co-occurrence counts, meta-skill invocation counts. Lifted out of
``skills/bundled/history-explorer/scripts/explore.py`` so that both
the bundled history-explorer script (a subprocess entrypoint) and
in-tree callers (e.g. ``skills.creator.auto_propose``) can share the
exact same logic — duplicating the aggregation in two places would
inevitably drift.

These functions read directly from disk and never mutate; they are
safe to call concurrently from a cron handler, a dream hook, or a
plain CLI invocation.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path


def parse_log_line(line: str) -> dict | None:
    """Parse one JSONL line into a dict; return None for blank/malformed."""
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def within_window(ts_str: str, cutoff: datetime) -> bool:
    """True iff the ISO timestamp string is at or after ``cutoff``."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts >= cutoff


def aggregate_co_occurrences(
    log_dir: Path, window_days: int, top_k: int,
) -> list[dict]:
    """Return top-K most-frequent skill co-occurrence chains in the window.

    A "chain" is the exact tuple of ``skills_invoked`` from a single
    DecisionEntry — order preserved, only chains of length ≥ 2 are
    counted. Each returned dict has shape::

        {"skills": [str, ...], "freq": int}

    Returns ``[]`` when the log dir does not exist (fresh install path).
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    counter: Counter[tuple[str, ...]] = Counter()
    intents: dict[tuple[str, ...], Counter[str]] = {}
    if not log_dir.is_dir():
        return []
    for log_path in sorted(log_dir.glob("decisions-*.jsonl")):
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            payload = parse_log_line(raw)
            if not payload:
                continue
            if not within_window(payload.get("ts", ""), cutoff):
                continue
            skills = payload.get("skills_invoked") or []
            if not isinstance(skills, list) or len(skills) < 2:
                continue
            key = tuple(skills)
            counter[key] += 1
            intent = str(
                payload.get("intent_summary")
                or payload.get("session_intent")
                or payload.get("user_intent")
                or payload.get("user_message")
                or payload.get("prompt")
                or payload.get("message")
                or "",
            ).strip()
            if intent:
                intents.setdefault(key, Counter())[intent[:500]] += 1
    return [
        {
            "skills": list(combo),
            "freq": freq,
            "sample_intents": [
                intent for intent, _count in intents.get(combo, Counter()).most_common(3)
            ],
        }
        for combo, freq in counter.most_common(top_k)
    ]


def aggregate_meta_usage(
    log_dir: Path,
    window_days: int,
    meta_names: set[str] | None = None,
) -> list[dict]:
    """Count how often each kind=meta skill was invoked.

    Args:
        log_dir: decision-log directory containing decisions-*.jsonl files.
        window_days: time window for inclusion.
        meta_names: set of skill names where kind == "meta". When None,
            falls back to the name-prefix heuristic (skill.startswith("meta-")).
            The heuristic is less accurate because helper bundles like
            skill-creator-linter / skill-creator-proposals / skill-creator-smoke-test
            are kind=skill but share the prefix (N12 fix).
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    counter: Counter[str] = Counter()
    if not log_dir.is_dir():
        return []
    for log_path in sorted(log_dir.glob("decisions-*.jsonl")):
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            payload = parse_log_line(raw)
            if not payload or not within_window(payload.get("ts", ""), cutoff):
                continue
            for skill in payload.get("skills_invoked") or []:
                if not isinstance(skill, str):
                    continue
                if meta_names is not None:
                    if skill in meta_names:
                        counter[skill] += 1
                elif skill.startswith("meta-"):
                    counter[skill] += 1
    return [
        {"meta_skill_id": name, "invocation_count": ct}
        for name, ct in counter.most_common()
    ]


def _bounded_text(value: object, *, max_chars: int = 240) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _failure_task_hint(payload: dict) -> str:
    return _bounded_text(
        payload.get("intent_summary")
        or payload.get("session_intent")
        or payload.get("user_intent")
        or payload.get("user_message")
        or payload.get("prompt")
        or payload.get("message")
        or "global",
        max_chars=160,
    ) or "global"


def _failure_stage(step_id: str, skill: str) -> str:
    haystack = f"{step_id} {skill}".lower()
    if any(token in haystack for token in ("clarify", "intent", "intake")):
        return "intent_brief"
    if any(token in haystack for token in ("pick", "pattern", "mode")):
        return "pattern_picker"
    if any(token in haystack for token in ("history", "harvest", "catalog", "context")):
        return "context_builder"
    if any(token in haystack for token in ("gate", "benchmark", "eval", "lint", "smoke")):
        return "gate_calibration"
    if "review" in haystack:
        return "review_ux"
    return "slot_generator"


def _failure_error_family(reason: str) -> str:
    haystack = reason.lower()
    if any(token in haystack for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in haystack for token in ("permission", "sandbox", "denied")):
        return "permission"
    if any(token in haystack for token in ("not found", "missing", "no such file", "command")):
        return "missing_tool"
    if any(token in haystack for token in ("provider", "rate limit", "api", "model")):
        return "provider_error"
    if any(token in haystack for token in ("gate", "benchmark", "eval", "lint", "smoke")):
        return "gate_failed"
    if any(token in haystack for token in ("invalid", "schema", "contract", "parse")):
        return "validation_error"
    return "unknown"


def _failure_steps(payload: dict) -> list[dict]:
    for field in ("pipeline_steps", "steps", "meta_steps"):
        raw = payload.get(field)
        if isinstance(raw, list):
            return [step for step in raw if isinstance(step, dict)]
    return []


def aggregate_failure_paths(
    log_dir: Path, window_days: int, top_k: int,
) -> list[dict]:
    """Return compact failed-step paths from decision logs.

    The output is intentionally small and scrubbed: it keeps only the task
    class hint, failed step/skill identifiers, coarse error family, bounded
    reason text, and fallback relationship. Downstream creator-learning code
    can turn these into feedback cards without exposing raw history.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    counter: Counter[tuple[str, str, str, str, str, str, bool, str, str]] = Counter()
    if not log_dir.is_dir():
        return []
    for log_path in sorted(log_dir.glob("decisions-*.jsonl")):
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            payload = parse_log_line(raw)
            if not payload or not within_window(payload.get("ts", ""), cutoff):
                continue
            task_hint = _failure_task_hint(payload)
            for step in _failure_steps(payload):
                status = str(step.get("status") or step.get("state") or "").lower()
                if status not in {"failed", "error", "errored"}:
                    continue
                step_id = _bounded_text(
                    step.get("step_id") or step.get("id") or step.get("name"),
                    max_chars=80,
                )
                skill = _bounded_text(
                    step.get("effective_skill")
                    or step.get("declared_skill")
                    or step.get("skill")
                    or step.get("tool")
                    or "",
                    max_chars=120,
                )
                reason = _bounded_text(
                    step.get("error")
                    or step.get("error_message")
                    or step.get("terminal_reason")
                    or step.get("reason")
                    or status,
                )
                recovered_by = _bounded_text(
                    step.get("substitute_step_id")
                    or step.get("on_failure")
                    or step.get("recovered_by")
                    or "",
                    max_chars=80,
                )
                had_fallback = bool(
                    recovered_by
                    or step.get("rescue")
                    or step.get("fallback")
                    or step.get("failover")
                )
                key = (
                    "decision_log",
                    task_hint,
                    step_id,
                    skill,
                    _failure_stage(step_id, skill),
                    _failure_error_family(reason),
                    had_fallback,
                    recovered_by,
                    reason,
                )
                counter[key] += 1
    rows: list[dict] = []
    for (
        source,
        task_hint,
        step_id,
        skill,
        stage,
        family,
        had_fallback,
        recovered_by,
        reason,
    ), count in counter.most_common(top_k):
        rows.append({
            "source": source,
            "task_class_hint": task_hint,
            "failed_step_id": step_id,
            "failed_skill": skill,
            "failed_stage": stage,
            "error_family": family,
            "had_fallback": had_fallback,
            "recovered_by": recovered_by,
            "sample_reason": reason,
            "count": count,
        })
    return rows


__all__ = [
    "parse_log_line",
    "within_window",
    "aggregate_co_occurrences",
    "aggregate_failure_paths",
    "aggregate_meta_usage",
]
