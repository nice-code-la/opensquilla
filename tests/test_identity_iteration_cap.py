"""Regression discovered by real Luna-driven repeated subagent delegation."""

import asyncio

from opensquilla.engine import ToolResult
from opensquilla.observability.piggyback.identity import kind
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from tests.test_piggyback_id_graph import recording as recording
from tests.test_piggyback_id_v2 import agent
from tests.test_piggyback_id_v2 import offline as offline
from tests.test_piggyback_id_v2 import scenario as scenario


async def test_iteration_cap_instruction_has_framework_provenance_and_creates_no_user_turn(
    recording, scenario
):
    async def tool(call):
        return ToolResult(call.tool_use_id, call.tool_name, "observed")

    runner = agent(scenario, "main", tool)
    runner.config.max_iterations = 1
    output = [event async for event in runner.run_turn("Use the tool then report its result.")]
    assert any(event.kind == "done" for event in output)
    assert len(scenario.requests) == 2
    final = scenario.requests[-1]
    assert any("configured iteration limit" in str(m.get("content")) for m in final["messages"])
    assert not final.get("tools")
    graph = ExecutionIdReconstructor().reconstruct(recording.identities.declarations())
    assert len([n for n in graph["nodes"] if kind(n) == "turn"]) == 1
    assert len(graph["positions"]) == 2
    assert all(s["complete"] for s in graph["scopes"].values()), graph["scopes"]
    assert not [n for n in graph["nodes"] if kind(n) == "gap"]


async def test_framework_timeout_result_has_cancelled_execution_provenance(recording, scenario):
    async def tool(call):
        await asyncio.sleep(20)
        return ToolResult(call.tool_use_id, call.tool_name, "must never be returned")

    runner = agent(scenario, "main", tool)
    runner.tool_definitions[0].execution_timeout_seconds = 0.02
    output = [event async for event in runner.run_turn("Use the tool then report its result.")]
    assert any(event.kind == "done" for event in output)
    assert len(scenario.requests) == 2
    assert "timed out" in str(scenario.requests[-1]["messages"])
    graph = ExecutionIdReconstructor().reconstruct(recording.identities.declarations())
    assert all(s["complete"] for s in graph["scopes"].values()), graph["scopes"]
    cancelled = {k for k in graph["nodes"] if kind(k) == "tool_cancelled"}
    assert len(cancelled) == 1
    assert not [n for n in graph["nodes"] if kind(n) == "tool_success"]
    assert any(
        kind(k) == "result" and n["producer_id"] in cancelled for k, n in graph["nodes"].items()
    )
