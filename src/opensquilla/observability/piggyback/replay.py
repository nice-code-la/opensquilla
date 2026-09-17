"""Recorded execution and new-branch execution have separate, explicit APIs."""

from __future__ import annotations

import base64
import inspect
import uuid

from .protocol import canonical


class ReplayDivergenceError(ValueError):
    pass


ReplayDivergence = ReplayDivergenceError


class ReplaySession:
    def __init__(self, bundle: dict, run_id: str):
        if not bundle["runs"].get(run_id, {}).get("complete"):
            raise ValueError("replay_requires_complete_trace")
        self.bundle = bundle
        self.events = [e for e in bundle["events"] if e["context"].get("run_id") == run_id]
        self.calls = [e for e in self.events if e["kind"] in {"llm.request", "tool.requested"}]
        self.index = 0

    def _next(self, kind: str, request: dict, field: str):
        if self.index >= len(self.calls):
            raise ReplayDivergence("unrecorded_operation")
        event = self.calls[self.index]
        if event["kind"] != kind or canonical(event["payload"].get(field)) != canonical(request):
            raise ReplayDivergence("recorded_input_mismatch")
        self.index += 1
        return event

    def llm(self, request: dict) -> bytes:
        exchange = self.exchange(request)
        outcome = exchange.get("outcome") or {}
        if outcome.get("status") != "completed" or (exchange.get("status_code") or 200) >= 400:
            raise ReplayDivergence("recorded_provider_failure:" + str(outcome.get("error_type")))
        return exchange["body"]

    def exchange(self, request: dict) -> dict:
        event = self._next("llm.request", request, "request")
        call = self.bundle["model_contexts"][event["context"]["execution_id"]]
        return {
            "body": base64.b64decode(call["wire_response_base64"]),
            "status_code": call.get("response_headers", {}).get("status_code"),
            "content_encoding": call.get("response_headers", {}).get("content_encoding", ""),
            "outcome": call.get("outcome"),
        }

    def tool(self, call: dict):
        event = self._next("tool.requested", call, "call")
        tool_id = event["context"]["tool_execution_id"]
        for result in self.bundle["tool_executions"][tool_id]:
            if result["kind"] == "tool.returned":
                return result["payload"]["result"]
            if result["kind"] == "tool.exception":
                raise ReplayDivergence("recorded_tool_exception:" + result["payload"]["type"])
        raise ReplayDivergence("missing_tool_result")

    def finish(self):
        if self.index != len(self.calls):
            raise ReplayDivergence("unconsumed_recorded_operations")


async def resume(bundle: dict, run_id: str, snapshot: dict, adapter, target, runner):
    report = bundle["runs"].get(run_id, {})
    if not report.get("complete") or report.get("environment_grade") != "executable":
        raise ValueError("resume_requires_complete_executable_bundle")
    if snapshot["snapshot_id"] not in report.get("snapshot_ids", []):
        raise ValueError("snapshot_not_in_source_run")
    adapter.restore(snapshot, target)
    verification = adapter.verify(snapshot, target)
    if not verification["ok"]:
        raise ValueError("restored_environment_verification_failed")
    new_run_id = uuid.uuid4().hex
    lineage = {
        "run_id": new_run_id,
        "parent_run_id": run_id,
        "source_revision": bundle["revision"],
        "snapshot_id": snapshot["snapshot_id"],
    }
    result = runner(
        target=target,
        lineage=lineage,
        transcript=bundle["transcript"],
        checkpoint=bundle.get("framework_state", {}).get(run_id, {}),
    )
    if inspect.isawaitable(result):
        result = await result
    return {"lineage": lineage, "result": result}
