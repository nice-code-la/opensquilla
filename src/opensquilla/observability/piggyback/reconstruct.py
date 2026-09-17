"""Deterministic evidence reconstruction, with no model calls or inferred missing facts."""

from __future__ import annotations

import base64
import gzip
import heapq
import io
import json
from collections import defaultdict

from .call_index import build_call_index
from .causal_views import build_causal_views
from .protocol import ProtocolError, canonical, digest, validate_record

MAX_RESPONSE_PARSE = 16 * 1024 * 1024


def parse_response(wire: bytes, encoding: str) -> dict:
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
                wire = stream.read(MAX_RESPONSE_PARSE + 1)
        elif encoding == "br":
            import brotli

            # Older optional decoders without bounded output remain view-only.
            decoder = brotli.Decompressor()
            wire = decoder.process(wire, output_buffer_limit=MAX_RESPONSE_PARSE + 1)
            if len(wire) <= MAX_RESPONSE_PARSE and not decoder.is_finished():
                return {"parse_status": "unavailable", "frames": []}
        elif encoding:
            return {"parse_status": "unsupported_content_encoding", "frames": []}
        if len(wire) > MAX_RESPONSE_PARSE:
            return {"parse_status": "size_limit", "frames": []}
        text = wire.decode("utf-8")
    except Exception:
        return {"parse_status": "unavailable", "frames": []}
    try:
        value = json.loads(text)
        frames = [value]
    except ValueError:
        frames = []
        partial = False
        for line in text.splitlines():
            if not line or line.startswith((":", "event:", "id:", "retry:")):
                continue
            candidate = line[5:].strip() if line.startswith("data:") else line.strip()
            if candidate == "[DONE]":
                continue
            try:
                frames.append(json.loads(candidate))
            except ValueError:
                partial = True
        value = {"frames": frames, "parse_status": "partial" if partial else "parsed"}
    usage = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        for source in (frame.get("message"), frame.get("response"), frame):
            if isinstance(source, dict) and isinstance(source.get("usage"), dict):
                usage.update(source["usage"])
        if frame.get("done") is True:
            for key, name in (
                ("prompt_eval_count", "prompt_tokens"),
                ("eval_count", "completion_tokens"),
            ):
                if key in frame:
                    usage[name] = frame[key]
    return {"body": value, "usage": usage}


class TraceReconstructor:
    def reconstruct(
        self,
        records: list[dict],
        *,
        blobs: dict[str, bytes] | None = None,
        manifests: list[dict] | None = None,
        provenance: str = "unknown",
    ) -> dict:
        if provenance not in {"unknown", "local", "platform"}:
            raise ValueError("invalid_reconstruction_provenance")
        unique, conflicts, invalid = {}, set(), []
        for record in records:
            try:
                validate_record(record)
            except (ProtocolError, TypeError, ValueError):
                invalid.append(digest(canonical(record)))
                continue
            key = record["id"]
            if key in unique and canonical(unique[key]) != canonical(record):
                conflicts.add(key)
            else:
                unique[key] = record
        # Conflicting evidence has no winner, independent of arrival order.
        for key in conflicts:
            unique.pop(key, None)
        content = dict(blobs or {})
        content_errors = set()
        for ref, data in list(content.items()):
            if digest(data) != ref:
                content.pop(ref)
                content_errors.add(ref)
        chunks = defaultdict(list)
        for record in unique.values():
            if record["type"] == "blob":
                chunks[record["value"]["sha256"]].append(record["value"])
        for ref, pieces in chunks.items():
            totals = {p["total"] for p in pieces}
            if len(totals) != 1:
                content_errors.add(ref)
                continue
            offset, parts = 0, []
            for part in sorted(pieces, key=lambda p: p["offset"]):
                data = base64.b64decode(part["data"])
                if part["offset"] != offset:
                    break
                parts.append(data)
                offset += len(data)
            if offset == next(iter(totals)):
                data = b"".join(parts)
                if digest(data) == ref:
                    content[ref] = data
                else:
                    content_errors.add(ref)
        events = {r["id"]: r["value"] for r in unique.values() if r["type"] == "event"}
        manifest_map = {
            r["value"]["run_id"]: r["value"] for r in unique.values() if r["type"] == "manifest"
        }
        for manifest in manifests or []:
            validate_record(
                {"id": "manifest:" + manifest["run_id"], "type": "manifest", "value": manifest}
            )
            run_id = manifest["run_id"]
            if run_id in manifest_map and canonical(manifest_map[run_id]) != canonical(manifest):
                conflicts.add(f"manifest:{run_id}")
                manifest_map.pop(run_id)
            else:
                manifest_map[run_id] = manifest
        sequences, by_run = defaultdict(list), defaultdict(list)
        sequence_conflicts = set()
        for event in events.values():
            sequences[(event["producer_id"], event["producer_epoch"])].append(event)
            if event["context"].get("run_id"):
                by_run[event["context"]["run_id"]].append(event)
        edges, missing_links = set(), set()
        for sequence in sequences.values():
            ordered = sorted(sequence, key=lambda e: (e["seq"], e["event_id"]))
            for previous, current in zip(ordered, ordered[1:]):
                if previous["seq"] == current["seq"]:
                    sequence_conflicts.update((previous["event_id"], current["event_id"]))
                else:
                    edges.add((previous["event_id"], current["event_id"], "producer_order"))
        for event in events.values():
            for link in event.get("links", []):
                if link.get("event_id") in events:
                    edges.add(
                        (link["event_id"], event["event_id"], link.get("relation", "caused_by"))
                    )
                else:
                    missing_links.add(event["event_id"])
            if event["kind"] == "agent.spawn":
                child_id = event["payload"].get("child_run_id")
                for child in by_run.get(child_id, []):
                    if child["kind"] == "run.start":
                        edges.add((event["event_id"], child["event_id"], "spawn"))
        children, degree = defaultdict(set), dict.fromkeys(events, 0)
        for left, right, _ in edges:
            if right not in children[left]:
                children[left].add(right)
                degree[right] += 1
        ready = [key for key, count in degree.items() if count == 0]
        heapq.heapify(ready)
        order = []
        while ready:
            key = heapq.heappop(ready)
            order.append(key)
            for child in sorted(children[key]):
                degree[child] -= 1
                if degree[child] == 0:
                    heapq.heappush(ready, child)
        cyclic = set(events) - set(order)
        order.extend(sorted(cyclic))
        hydrated = []
        for key in order:
            event = dict(events[key])
            payload = event["payload"]
            ref = payload.get("content_ref")
            if ref and payload.get("encoding") == "json" and ref in content:
                try:
                    event["payload"] = json.loads(content[ref])
                except (ValueError, UnicodeError):
                    content_errors.add(ref)
            hydrated.append(event)
        hydrated_map = {e["event_id"]: e for e in hydrated}
        reports = {}
        for run_id in sorted(set(by_run) | set(manifest_map)):
            run_events = by_run.get(run_id, [])
            ids = {e["event_id"] for e in run_events}
            manifest = manifest_map.get(run_id)
            reasons = []
            if not manifest:
                reasons.append("missing_manifest")
                required = ids
            else:
                required = set(manifest["event_ids"])
                if required - ids:
                    reasons.append("missing_events")
                if ids - required:
                    reasons.append("events_after_manifest")
                terminal = events.get(manifest.get("terminal_event_id"))
                if (
                    not terminal
                    or terminal["kind"] != "run.end"
                    or terminal["context"].get("run_id") != run_id
                    or manifest.get("terminal_event_id") != manifest["event_ids"][-1]
                ):
                    reasons.append("missing_terminal")
                if manifest.get("coverage", {}).get("capture_gap"):
                    reasons.append("capture_gap")
                if not any(e["kind"] == "run.start" for e in run_events):
                    reasons.append("missing_start")
            refs = {ref for e in run_events for ref in e.get("blob_refs", [])}
            refs.update(manifest.get("blob_refs", []) if manifest else [])
            missing_refs = refs - content.keys()
            if missing_refs:
                reasons.append("missing_content")
            if refs & content_errors:
                reasons.append("corrupt_content")
            if required & (conflicts | sequence_conflicts) or f"manifest:{run_id}" in conflicts:
                reasons.append("conflicting_evidence")
            if ids & missing_links:
                reasons.append("missing_causal_link")
            if ids & cyclic:
                reasons.append("causal_cycle")
            if invalid:
                reasons.append("invalid_records")
            # Every observed start must close, even if a client crashed before producing the end.
            for start_kind, end_kinds, identity_key in (
                ("llm.request", {"llm.end"}, "execution_id"),
                ("tool.requested", {"tool.returned", "tool.exception"}, "tool_execution_id"),
            ):
                started = {
                    e["context"].get(identity_key) for e in run_events if e["kind"] == start_kind
                }
                ended = {
                    e["context"].get(identity_key) for e in run_events if e["kind"] in end_kinds
                }
                if started - ended:
                    reasons.append("open_operations")
            environment_payloads = [
                hydrated_map[e["event_id"]]["payload"]
                for e in run_events
                if e["kind"] == "environment.checkpoint"
            ]
            environments = [p.get("snapshot", p) for p in environment_payloads]
            reports[run_id] = {
                "complete": not reasons,
                "reasons": sorted(set(reasons)),
                "missing_event_ids": sorted(required - ids),
                "missing_blob_refs": sorted(missing_refs),
                "coverage": manifest.get("coverage", {}) if manifest else {},
                "causality_coverage": manifest.get("coverage", {}).get(
                    "loop_causality", "legacy-unverified"
                )
                if manifest
                else "awaiting_manifest",
                "environment_grade": "incomplete"
                if reasons
                else (
                    "executable"
                    if environments and all(s.get("grade") == "executable" for s in environments)
                    else "replay_only"
                ),
                "child_run_ids": manifest.get("child_run_ids", []) if manifest else [],
                "snapshot_ids": sorted(
                    {s["snapshot_id"] for s in environments if s.get("snapshot_id")}
                ),
            }
        # A complete parent cannot conceal an unfinished or absent child.
        changed = True
        while changed:
            changed = False
            for report in reports.values():
                if report["complete"] and any(
                    not reports.get(child, {}).get("complete") for child in report["child_run_ids"]
                ):
                    report["complete"] = False
                    report["environment_grade"] = "incomplete"
                    report["reasons"].append("incomplete_child")
                    changed = True
        calls, tools = defaultdict(list), defaultdict(list)
        for event in hydrated:
            if event["kind"].startswith("llm."):
                calls[event["context"].get("execution_id", "unscoped")].append(event)
            if event["kind"].startswith("tool."):
                tools[event["context"].get("tool_execution_id", "unscoped")].append(event)
        model_contexts = {}
        for call_id, observations in sorted(calls.items()):
            request = next(
                (e["payload"].get("request") for e in observations if e["kind"] == "llm.request"),
                None,
            )
            fragments = [e["payload"] for e in observations if e["kind"] == "llm.chunk"]
            fragments.sort(key=lambda p: p.get("ordinal", -1))
            wire = b"".join(base64.b64decode(p["data"]) for p in fragments if "data" in p)
            headers = next((e["payload"] for e in observations if e["kind"] == "llm.headers"), {})
            parsed = parse_response(wire, headers.get("content_encoding", ""))
            model_contexts[call_id] = {
                "identity": dict(observations[0]["context"]),
                "request": request,
                "wire_response_base64": base64.b64encode(wire).decode(),
                "response": parsed.get("body"),
                "usage": parsed.get("usage", {}),
                "response_headers": headers,
                "outcome": next(
                    (e["payload"] for e in observations if e["kind"] == "llm.end"), None
                ),
                "normalized": [e["payload"] for e in observations if e["kind"] == "llm.normalized"],
                "event_ids": [e["event_id"] for e in observations],
            }
        iterations, rounds, causal_errors = build_causal_views(hydrated, model_contexts)
        call_index, user_turns, input_contexts, identity_errors = build_call_index(
            hydrated, model_contexts, edges
        )
        for run_id, reasons in identity_errors.items():
            causal_errors[run_id].update(reasons)
        for run_id, reasons in causal_errors.items():
            if run_id in reports and reasons:
                reports[run_id]["complete"] = False
                reports[run_id]["environment_grade"] = "incomplete"
                reports[run_id]["reasons"] = sorted(set(reports[run_id]["reasons"]) | reasons)
        changed = True
        while changed:
            changed = False
            for report in reports.values():
                if report["complete"] and any(
                    not reports.get(child, {}).get("complete") for child in report["child_run_ids"]
                ):
                    report["complete"] = False
                    report["environment_grade"] = "incomplete"
                    report["reasons"] = sorted(set(report["reasons"]) | {"incomplete_child"})
                    changed = True
        framework_state = {}
        for turn in user_turns.values():
            turn["complete"] = not turn["reasons"] and all(
                reports.get(run, {}).get("complete", False) for run in turn["run_ids"]
            )
        for call in call_index.values():
            run = (call["location"] or {}).get("run_id")
            call["run_complete"] = reports.get(run, {}).get("complete", False)
        for event in hydrated:
            run = event["context"].get("run_id")
            if not run:
                continue
            state = framework_state.setdefault(
                run,
                {
                    "committed_messages": [],
                    "context_history": [],
                    "compactions": [],
                    "memory_checkpoints": [],
                    "accepted_assistant_message": None,
                },
            )
            if event["kind"] == "session.message_committed":
                message = event["payload"].get("message", {})
                state["committed_messages"].append(
                    {"event_id": event["event_id"], "message": message}
                )
                if message.get("role") == "assistant":
                    state["accepted_assistant_message"] = message
            elif event["kind"] == "llm.request":
                state["context_history"].append(
                    {
                        "event_id": event["event_id"],
                        "execution_id": event["context"].get("execution_id"),
                        "request": event["payload"].get("request"),
                    }
                )
            elif event["kind"].startswith("context.compaction_"):
                state["compactions"].append(event)
            elif event["kind"].startswith("memory."):
                state["memory_checkpoints"].append(event)
        bundle = {
            "schema_version": 1,
            "provenance": provenance,
            "events": hydrated,
            "graph": [{"from": a, "to": b, "relation": r} for a, b, r in sorted(edges)],
            "causal_graph": [
                {"from": a, "to": b, "relation": r}
                for a, b, r in sorted(edges)
                if r != "producer_order"
            ],
            "loop_iterations": iterations,
            "ensemble_rounds": rounds,
            "runs": reports,
            "model_contexts": model_contexts,
            "call_index": call_index,
            "user_turns": user_turns,
            "input_contexts": input_contexts,
            "framework_state": framework_state,
            "tool_executions": dict(sorted(tools.items())),
            "transcript": [
                e
                for e in hydrated
                if e["kind"] in {"run.start", "session.message_committed", "delivery.completed"}
            ],
            "environment_timeline": [e for e in hydrated if e["kind"].startswith("environment.")],
            "content_index": {ref: {"bytes": len(data)} for ref, data in sorted(content.items())},
            "conflicts": sorted(conflicts | sequence_conflicts),
            "invalid_records": sorted(invalid),
        }
        for report in reports.values():
            report["local_complete"] = report["complete"] if provenance == "local" else None
            report["platform_complete"] = report["complete"] if provenance == "platform" else None
        bundle["revision"] = digest(canonical(bundle))
        return bundle


def reconstruct(events, blobs=None, manifests=None):
    records = [{"type": "event", "id": e["event_id"], "value": e} for e in events]
    return TraceReconstructor().reconstruct(records, blobs=blobs, manifests=manifests)
