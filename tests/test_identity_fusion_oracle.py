"""Independent scheduling/HTTP oracle; identical text cannot hide wrong candidate IDs."""

import contextvars
import json
from dataclasses import replace

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.observability.piggyback.capture import trace_tool_handler
from opensquilla.observability.piggyback.identity import parts
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider.ensemble import EnsembleProvider
from tests.test_identity_runtime import graph, nodes
from tests.test_piggyback_id_graph import recording as recording
from tests.test_piggyback_id_v2 import offline as offline
from tests.test_piggyback_id_v2 import scenario as scenario
from tests.test_provider_ensemble import _member


@pytest.mark.parametrize(
    "mode", ["distinct", "identical", "partial_failure", "all_failed", "fallback"]
)
async def test_fusion_relationships_equal_independent_wire_and_scheduler_oracle(
    recording, scenario, monkeypatch, mode
):
    import opensquilla.provider.ensemble as ensemble

    actor = contextvars.ContextVar("independent_test_actor", default=None)
    oracle = []
    original_collect = EnsembleProvider._collect_candidate

    async def collect(self, *args, **kwargs):
        token = actor.set((kwargs["index"], kwargs["sample_index"]))
        try:
            return await original_collect(self, *args, **kwargs)
        finally:
            actor.reset(token)

    monkeypatch.setattr(EnsembleProvider, "_collect_candidate", collect)
    original_model = scenario.app.target.app

    async def model(scope, receive, send):
        request = await receive()
        business = json.loads(request["body"])
        observed = {
            "call": scope["trace_platform"]["call_id"],
            "slot": actor.get(),
            "model": business["model"],
            "request": business,
        }
        oracle.append(observed)

        async def replay():
            return request

        async def sent(event):
            if event["type"] == "http.response.body":
                payload = json.loads(event["body"])
                if observed["slot"] is not None:
                    if (
                        mode == "all_failed"
                        or mode == "partial_failure"
                        and observed["slot"][0] == 1
                    ):
                        payload = {"error": "refused", "code": "403"}
                    elif mode == "identical" and "text" in payload:
                        payload["text"] = "identical candidate text " * 80
                observed["success"] = "error" not in payload
                observed["response"] = payload
                event = {**event, "body": json.dumps(payload).encode()}
            await send(event)

        await original_model(scope, replay, sent)

    scenario.app.target.app = model
    scenario.fallback = mode == "fallback"
    monkeypatch.setattr(ensemble, "_build_provider", scenario.provider)
    monkeypatch.setattr(ensemble, "_proposer_retry_backoff_seconds", lambda _: 0)
    monkeypatch.setattr(ensemble, "_aggregator_retry_backoff_seconds", lambda _: 0)
    monkeypatch.setattr(ensemble.random, "shuffle", lambda values: values.reverse())
    provider = EnsembleProvider(
        profile_name="oracle",
        proposers=[replace(_member("p1"), k=2), _member("p2")],
        aggregator=_member("agg"),
        fallback_provider=scenario.provider(_member("fixed").provider_config),
        fallback_provider_name="fake",
        fallback_model="fixed",
        min_successful_proposers=1,
        proposer_max_retries=1,
        candidate_max_chars=90,
        shuffle_candidates=True,
    )

    async def tool(call):
        return ToolResult(call.tool_use_id, call.tool_name, "observed")

    runner = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        session_key="session",
        tool_handler=trace_tool_handler(tool, None),
        tool_definitions=[
            ToolDefinition(name="echo", description="echo", input_schema=ToolInputSchema())
        ],
    )
    events = [event async for event in runner.run_turn("do tool work and answer")]
    assert any(event.kind == "done" for event in events)
    rebuilt = graph(recording)
    assert not recording.failed_runs
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()), (
        rebuilt["issues"],
        rebuilt["scopes"],
    )
    calls = {row["call"]: row for row in oracle}
    assert len(rebuilt["positions"]) == len(calls) == len(scenario.requests)
    ids = rebuilt["nodes"]
    successful = {
        row["slot"]: row["call"] for row in oracle if row["slot"] is not None and row.get("success")
    }
    candidate_calls = {}
    for candidate in nodes(rebuilt, "candidate_result"):
        source = ids[candidate["result_id"]]["producer_id"]
        call = parts(source)[2]
        assert calls[call]["success"]
        assert call == successful[calls[call]["slot"]]
        candidate_calls[candidate["id"]] = call
    assert len(candidate_calls) == len(successful)
    expected_order = [successful[slot] for slot in sorted(successful, reverse=True)]
    for selection in nodes(rebuilt, "selection"):
        selected = [
            candidate_calls[ids[projection]["candidate_result_id"]]
            for projection in selection["projection_ids"]
        ]
        assert selected == expected_order
    if not successful:
        assert not nodes(rebuilt, "selection")
        assert len(nodes(rebuilt, "candidate_failure")) == 3
    for acceptance in nodes(rebuilt, "acceptance"):
        call = parts(ids[acceptance["result_id"]]["producer_id"])[2]
        assert calls[call]["slot"] is None and calls[call]["success"]
    if mode == "fallback":
        assert nodes(rebuilt, "generation_discarded") and nodes(rebuilt, "replacement")
    # Erasing an entire independent proposer branch must leave an inventory gap.
    candidate = nodes(rebuilt, "candidate")[0]
    missing_lane = candidate["lane_id"]
    gone_operations = {
        row["id"] for row in nodes(rebuilt, "operation") if row["lane_id"] == missing_lane
    }
    gone_attempts = {
        row["id"] for row in nodes(rebuilt, "attempt") if row["operation_id"] in gone_operations
    }
    removed = {candidate["id"], missing_lane, *gone_operations, *gone_attempts}
    removed.update(
        row["id"] for row in nodes(rebuilt, "call") if row["attempt_id"] in gone_attempts
    )
    from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor

    broken = ExecutionIdReconstructor().reconstruct(
        [row for row in ids.values() if row["id"] not in removed]
    )
    assert not any(scope["complete"] for scope in broken["scopes"].values())
