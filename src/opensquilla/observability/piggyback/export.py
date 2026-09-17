"""Offline JSON/OTLP/OpenInference/ATIF projections and a self-contained trace viewer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .journal import atomic_write
from .protocol import canonical, digest


def _id(value: str, length: int) -> str:
    return digest(value.encode())[:length]


def _attrs(values: dict) -> list[dict]:
    return [
        {
            "key": key,
            "value": {
                "stringValue": value if isinstance(value, str) else canonical(value).decode()
            },
        }
        for key, value in values.items()
        if value is not None
    ]


def to_otlp(bundle: dict) -> dict:
    """OTLP JSON, with OpenInference attributes. This function does not export over a socket."""
    spans = []
    groups = {}
    event_spans = {}
    for event in bundle["events"]:
        ctx = event["context"]
        run = ctx.get("run_id")
        if run:
            groups.setdefault(("CHAIN", run), []).append(event)
        if event["kind"].startswith("llm.") and ctx.get("execution_id"):
            groups.setdefault(("LLM", ctx["execution_id"]), []).append(event)
        if event["kind"].startswith("tool.") and ctx.get("tool_execution_id"):
            groups.setdefault(("TOOL", ctx["tool_execution_id"]), []).append(event)
        identity = (
            ctx.get("execution_id")
            if event["kind"].startswith("llm.")
            else ctx.get("tool_execution_id")
            if event["kind"].startswith("tool.")
            else run
        )
        if identity:
            event_spans[event["event_id"]] = (
                _id(ctx.get("trace_id") or run or identity, 32),
                _id(identity, 16),
            )
    for (kind, identity), events in sorted(groups.items()):
        ctx = events[0]["context"]
        run = ctx.get("run_id", identity)
        attributes = {
            "openinference.span.kind": kind,
            "opensquilla.run_id": run,
            "opensquilla.identity": identity,
            "session.id": ctx.get("session_id"),
            "opensquilla.user_turn_id": ctx.get("user_turn_id"),
        }
        if kind == "LLM":
            call = bundle["model_contexts"].get(identity, {})
            attributes.update(
                {
                    "input.value": call.get("request"),
                    "opensquilla.call_id": identity,
                    "opensquilla.call_location": bundle.get("call_index", {}).get(identity),
                    "input.mime_type": "application/json",
                    "llm.model_name": (call.get("request") or {}).get("model"),
                    "output.value": call.get("response"),
                    "output.mime_type": "application/json",
                }
            )
        elif kind == "TOOL":
            attributes.update(
                {"input.value": events[0]["payload"], "output.value": events[-1]["payload"]}
            )
        timestamps = [e.get("ts_ns", 0) for e in events]
        error = any(
            e["kind"].endswith("exception")
            or e["payload"].get("status") in ("error", "interrupted", "cancelled")
            or (
                isinstance(e["payload"].get("status_code"), int)
                and e["payload"]["status_code"] >= 400
            )
            for e in events
        )
        parent = ctx.get("parent_run_id") if kind == "CHAIN" else run
        span = {
            "traceId": _id(ctx.get("trace_id") or run, 32),
            "spanId": _id(identity, 16),
            "name": kind.lower(),
            "kind": 3 if kind == "LLM" else 1,
            "startTimeUnixNano": str(min(timestamps)),
            "endTimeUnixNano": str(max(timestamps)),
            "attributes": _attrs(attributes),
            "status": {"code": 2 if error else 0},
            "events": [
                {
                    "name": e["kind"],
                    "timeUnixNano": str(e.get("ts_ns", 0)),
                    "attributes": _attrs({"event.id": e["event_id"], "payload": e["payload"]}),
                }
                for e in events
            ],
        }
        if parent:
            span["parentSpanId"] = _id(parent, 16)
        event_ids = {e["event_id"] for e in events}
        linked = set()
        for edge in bundle.get("graph", []):
            if edge["relation"] == "producer_order" or edge["to"] not in event_ids:
                continue
            source = event_spans.get(edge["from"])
            if source and source != (span["traceId"], span["spanId"]):
                linked.add((*source, edge["relation"]))
        if linked:
            span["links"] = [
                {"traceId": trace, "spanId": source, "attributes": _attrs({"relation": relation})}
                for trace, source, relation in sorted(linked)
            ]
        spans.append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs({"service.name": "opensquilla"})},
                "scopeSpans": [
                    {"scope": {"name": "opensquilla.piggyback", "version": "1"}, "spans": spans}
                ],
            }
        ]
    }


def to_atif(bundle: dict, run_id: str) -> dict:
    events = [e for e in bundle["events"] if e["context"].get("run_id") == run_id]
    steps = []
    for event in events:
        payload = event["payload"]
        step = None
        if event["kind"] == "run.start" and isinstance(payload.get("input"), str):
            step = {"source": "user", "message": payload["input"]}
        elif event["kind"] == "tool.requested":
            call = payload.get("call", {})
            tool_id = event["context"].get("tool_execution_id")
            results = bundle["tool_executions"].get(tool_id, [])
            terminal = next(
                (e for e in results if e["kind"] in {"tool.returned", "tool.exception"}), None
            )
            call_id = event["context"].get("tool_call_id") or tool_id
            step = {
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {
                        "tool_call_id": call_id,
                        "function_name": call.get("tool_name", call.get("name", "unknown")),
                        "arguments": call.get("arguments", {}),
                    }
                ],
            }
            if terminal:
                step["observation"] = {
                    "results": [
                        {
                            "source_call_id": call_id,
                            "content": canonical(terminal["payload"]).decode(),
                        }
                    ]
                }
        elif event["kind"] == "session.message_committed":
            message = payload.get("message", {})
            if message.get("role") in {"user", "assistant", "system"}:
                content = message.get("content", "")
                step = {
                    "source": "agent" if message["role"] == "assistant" else message["role"],
                    "message": content if isinstance(content, str) else canonical(content).decode(),
                }
        if step is not None:
            step.update(
                step_id=len(steps) + 1,
                timestamp=datetime.fromtimestamp(event.get("ts_ns", 0) / 1e9, UTC).isoformat(),
                extra={"source_event_id": event["event_id"]},
            )
            steps.append(step)
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": run_id,
        "agent": {"name": "opensquilla", "version": "0.5.4+tracepoint.3"},
        "steps": steps,
        "extra": {
            "source_revision": bundle["revision"],
            "completeness": bundle["runs"].get(run_id),
            "projection": "observed_actions_and_committed_messages",
        },
    }


def html_report(bundle: dict) -> str:
    # Neither trace prose nor JSON can terminate the script or become executable HTML.
    data = (
        canonical(bundle)
        .decode()
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (
        Path(__file__)
        .with_name("viewer.html")
        .read_text(encoding="utf-8")
        .replace("__TRACEPOINT_DATA__", data)
    )


def write_bundle(bundle: dict, target: Path) -> None:
    atomic_write(target, json.dumps(bundle, ensure_ascii=False, indent=2).encode())


def import_legacy(rows: list[dict]) -> list[dict]:
    """Preserve evidence and explicit IDs. No synthetic complete manifests or heuristic joins."""
    records = []
    for row in rows:
        key = digest(canonical(row))
        context = {
            k: str(row[k])
            for k in ("run_id", "turn_id", "session_id", "agent_id", "trace_id")
            if row.get(k)
        }
        # A legacy turn is a useful grouping only when it was explicitly recorded.
        if "run_id" not in context and row.get("turn_id"):
            context["run_id"] = str(row["turn_id"])
        event = {
            "schema_version": 2,
            "event_id": key,
            "producer_id": "legacy-" + key[:24],
            "producer_epoch": "import",
            "seq": 1,
            "ts_ns": 0,
            "context": context,
            "kind": "legacy.record",
            "payload": row,
            "blob_refs": [],
            "links": [],
            "coverage": {"legacy": True, "unknown_order": True},
        }
        records.append({"type": "event", "id": key, "value": event})
    return records
