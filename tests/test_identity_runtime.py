"""Actual Agent/Provider execution; the oracle observes business requests independently."""

from dataclasses import replace

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.observability.piggyback.capture import trace_tool_handler
from opensquilla.observability.piggyback.identity import kind
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider.ensemble import EnsembleProvider
from tests.test_piggyback_id_graph import recording as recording
from tests.test_piggyback_id_v2 import agent
from tests.test_piggyback_id_v2 import offline as offline
from tests.test_piggyback_id_v2 import scenario as scenario
from tests.test_provider_ensemble import _member


def graph(capture):
    result = ExecutionIdReconstructor().reconstruct(capture.identities.declarations())
    import json
    import os
    from pathlib import Path

    if directory := os.environ.get("TRACE_ID_GRAPH_EVIDENCE"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / (capture.identities.namespace + ".json")).write_text(json.dumps(result, indent=2))
    return result


def nodes(result, typ):
    return [row for row in result["nodes"].values() if kind(row["id"]) == typ]


async def test_real_two_turns_have_complete_local_scopes_and_only_business_uploads(
    recording, scenario
):
    runner = agent(scenario)
    assert [item async for item in runner.run_turn("first task")]
    first = graph(recording)
    assert not recording.failed_runs
    assert all(scope["complete"] for scope in first["scopes"].values()), (
        first["scopes"],
        first["issues"],
        first["missing_ids"],
        nodes(first, "gap"),
    )
    first_scope = next(iter(first["scopes"]))
    assert [item async for item in runner.run_turn("second task")]
    complete = graph(recording)
    assert len(nodes(complete, "call")) == len(scenario.requests) == 2
    assert all(scope["complete"] for scope in complete["scopes"].values()), complete
    rows = [
        record["value"]
        for source in scenario.store.sources("tenant")
        for record in scenario.store.records("tenant", source)
        if record["type"] == "id_node"
    ]
    received = ExecutionIdReconstructor().reconstruct(rows)
    assert received["scopes"][first_scope]["complete"]
    assert not all(scope["complete"] for scope in received["scopes"].values())
    scenario.store.db.execute("DELETE FROM records WHERE type!='id_node'")
    scenario.store.db.execute("DELETE FROM observations")
    assert len(scenario.requests) == 2


async def test_real_tool_loop_proposals_executions_and_acceptances_are_distinct(
    recording, scenario
):
    async def tool(call):
        return ToolResult(call.tool_use_id, call.tool_name, "observed")

    assert [item async for item in agent(scenario, "main", tool).run_turn("use tool")]
    result = graph(recording)
    assert not recording.failed_runs
    assert len(nodes(result, "call")) == len(scenario.requests) == 2
    assert len(nodes(result, "action")) == len(nodes(result, "tool_execution")) == 1
    assert len(nodes(result, "acceptance")) == 2
    assert all(scope["complete"] for scope in result["scopes"].values()), (
        result["scopes"],
        result["issues"],
        result["missing_ids"],
        nodes(result, "gap"),
    )


async def test_real_subagent_and_wait_have_no_invented_serial_order(recording, scenario):
    manager = SubagentManager()

    async def tool(call):
        child = await manager.spawn(SubagentSpec(task="inspect"), lambda *args: agent(scenario))
        return ToolResult(call.tool_use_id, call.tool_name, await child.task)

    assert [item async for item in agent(scenario, "main", tool).run_turn("delegate")]
    result = graph(recording)
    assert not recording.failed_runs
    assert len(nodes(result, "call")) == len(scenario.requests) == 3
    assert len(nodes(result, "dispatch")) == len(nodes(result, "wait")) == 1
    assert all(scope["complete"] for scope in result["scopes"].values()), result


@pytest.mark.parametrize("fallback", [False, True])
async def test_real_fusion_ids_only_cover_parallel_retry_selection_and_takeover(
    recording, scenario, monkeypatch, fallback
):
    import opensquilla.provider.ensemble as ensemble

    scenario.fallback = fallback
    monkeypatch.setattr(ensemble, "_build_provider", scenario.provider)
    monkeypatch.setattr(ensemble, "_proposer_retry_backoff_seconds", lambda _: 0)
    monkeypatch.setattr(ensemble, "_aggregator_retry_backoff_seconds", lambda _: 0)
    monkeypatch.setattr(ensemble.random, "shuffle", lambda values: values.reverse())
    provider = EnsembleProvider(
        profile_name="test",
        proposers=[replace(_member("p1"), k=2), _member("p2")],
        aggregator=_member("agg"),
        fallback_provider=scenario.provider(_member("fixed").provider_config),
        fallback_provider_name="fake",
        fallback_model="fixed",
        min_successful_proposers=2,
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
        tool_definitions=[
            ToolDefinition(name="echo", description="echo", input_schema=ToolInputSchema())
        ],
        tool_handler=trace_tool_handler(tool, None),
    )
    output = [item async for item in runner.run_turn("use tool and answer")]
    assert any(item.kind == "done" and "final answer" in item.text for item in output)
    result = graph(recording)
    assert not recording.failed_runs
    assert len(nodes(result, "call")) == len(scenario.requests)
    assert len(nodes(result, "candidate")) == 3
    assert len(nodes(result, "candidate_result")) == 3
    assert len(nodes(result, "ensemble")) == 2
    assert nodes(result, "selection")
    assert all(scope["complete"] for scope in result["scopes"].values()), result
    assert len({row["lane_id"] for row in nodes(result, "candidate")}) == 3
    assert len([row for row in nodes(result, "call") if row["retry_id"]]) >= 1
    if fallback:
        assert any(row["fallback_id"] for row in nodes(result, "aggregation"))
