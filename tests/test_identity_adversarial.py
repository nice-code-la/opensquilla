"""Fault and boundary tests. Reconstruction receives ID records exclusively."""

from __future__ import annotations

import copy
import random
import shutil

import httpx
import pytest

from opensquilla.observability.piggyback.identity import kind, record, ref
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.identity_registry import IdentityRegistry
from opensquilla.observability.piggyback.journal import TraceJournal
from opensquilla.observability.piggyback.platform import PlatformIngest
from opensquilla.observability.piggyback.protocol import ProtocolError, digest, encode
from opensquilla.observability.piggyback.server import create_platform
from tests.test_identity_complete import recorded_run
from tests.test_identity_complete import registry as registry
from tests.test_identity_runtime import graph, nodes
from tests.test_piggyback_id_graph import recording as recording
from tests.test_piggyback_id_v2 import agent
from tests.test_piggyback_id_v2 import offline as offline
from tests.test_piggyback_id_v2 import scenario as scenario


@pytest.mark.parametrize(
    "target", ["phase", "lane", "operation", "attempt", "closure_part", "acceptance"]
)
def test_each_owner_scope_is_validated_even_when_call_owner_is_unchanged(registry, target):
    run, _, _ = recorded_run(registry)
    other, _, _ = recorded_run(registry)
    values = registry.declarations()
    victim = next(row for row in values if kind(row["id"]) == target and row["scope_id"] == run)
    victim["scope_id"] = other
    rebuilt = ExecutionIdReconstructor().reconstruct(values)
    assert not rebuilt["scopes"][run]["complete"]
    assert rebuilt["issues"]


def test_invalid_declaration_does_not_poison_an_unrelated_closed_scope(registry):
    bad, _, _ = recorded_run(registry)
    good, _, _ = recorded_run(registry)
    values = registry.declarations()
    victim = next(row for row in values if kind(row["id"]) == "call" and row["scope_id"] == bad)
    victim["unexpected"] = "not a protocol field"
    rebuilt = ExecutionIdReconstructor().reconstruct(values)
    assert not rebuilt["scopes"][bad]["complete"]
    assert rebuilt["scopes"][good]["complete"]


def test_local_random_id_collision_is_never_silently_merged(registry, monkeypatch):
    from types import SimpleNamespace

    import opensquilla.observability.piggyback.identity as identity

    monkeypatch.setattr(identity.uuid, "uuid4", lambda: SimpleNamespace(hex="same"))
    first = registry.put("definition")
    with pytest.raises(ValueError, match="allocation_collision"):
        registry.put("definition")
    assert len([node for node in registry.declarations() if node["id"] == first]) == 1


def test_database_copy_cannot_continue_allocating_in_the_original_namespace(registry, tmp_path):
    recorded_run(registry)
    registry.journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    clone = tmp_path / "clone"
    shutil.copytree(registry.journal.root, clone)
    journal = TraceJournal(clone, "destination")
    try:
        with pytest.raises(ValueError, match="copied_identity_journal"):
            IdentityRegistry(journal)
    finally:
        journal.close()


@pytest.mark.parametrize("first_conflict", [False, True])
def test_platform_keeps_conflict_variants_after_reopen_and_filters_tenants(
    registry, tmp_path, first_conflict
):
    run, _, _ = recorded_run(registry)
    values = registry.declarations()
    call = next(row for row in values if kind(row["id"]) == "call")
    changed = {**call, "previous_id": ref(registry.namespace, "call", "unknown")}
    packets = [
        encode(registry.namespace, "base", [record(row) for row in values]),
        encode(registry.namespace, "conflict", [record(changed)]),
    ]
    if first_conflict:
        packets.reverse()
    path = tmp_path / "platform.db"
    store = PlatformIngest(path)
    store.accept("tenant", packets[0])
    with pytest.raises(ProtocolError):
        store.accept("tenant", packets[1])
    store.close()
    reopened = PlatformIngest(path)
    try:
        rebuilt = ExecutionIdReconstructor().reconstruct(reopened.execution_identities("tenant"))
        assert call["id"] in rebuilt["conflicting_ids"]
        assert not rebuilt["scopes"][run]["complete"]
        assert reopened.execution_identities("other-tenant") == []
    finally:
        reopened.close()


async def test_read_only_http_query_after_erasing_all_non_id_tables(registry, tmp_path):
    run, _, _ = recorded_run(registry)
    values = registry.declarations()
    call = next(row["id"] for row in values if kind(row["id"]) == "call")
    path = tmp_path / "platform.db"
    store = PlatformIngest(path)
    store.accept("tenant", encode(registry.namespace, "B", [record(row) for row in values]))
    store.db.execute("DELETE FROM records WHERE type!='id_node'")
    store.db.execute("DROP TABLE observations")
    store.close()
    app = create_platform(
        {
            "token_hash_to_tenant": {digest(b"key"): "tenant"},
            "sqlite_path": str(path),
            "upstream_base_url": "https://unused.test",
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://query"
    ) as client:
        reply = await client.get(
            "/execution-calls/" + call + "/ids", headers={"Authorization": "Bearer key"}
        )
        assert reply.status_code == 200, reply.text
        result = reply.json()
        assert result["scopes"][run]["complete"]
        assert result["position_status"][call] == "unique"
        assert (await client.get("/execution-id-traces")).status_code == 401
    await app.drain()


@pytest.mark.parametrize("conversion", ["direct", "explicit_str", "opaque_str", "independent"])
async def test_wait_does_not_prove_use_and_unannotated_conversion_is_a_gap(
    recording, scenario, conversion
):
    from opensquilla.engine import ToolResult
    from opensquilla.engine.subagent import SubagentManager, SubagentSpec
    from opensquilla.observability.piggyback.id_capture import result_provenance

    manager = SubagentManager()

    async def tool(call):
        child = await manager.spawn(SubagentSpec(task="inspect"), lambda *args: agent(scenario))
        value = await child.task
        explicit = (
            result_provenance(value)
            if conversion == "explicit_str"
            else result_provenance(None)
            if conversion == "independent"
            else None
        )
        output = (
            value
            if conversion == "direct"
            else "independent"
            if conversion == "independent"
            else str(value)
        )
        return ToolResult(call.tool_use_id, call.tool_name, output, trace_ids=explicit)

    assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    rebuilt = graph(recording)
    assert len(nodes(rebuilt, "wait")) == 1
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()) is (
        conversion != "opaque_str"
    )
    assert bool(nodes(rebuilt, "gap")) is (conversion == "opaque_str")


@pytest.mark.parametrize("reject", [False, True])
async def test_actual_dispatch_validation_is_not_tool_execution(recording, scenario, reject):
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolSpec
    from tests.test_provider_ensemble import _member

    calls = []

    async def execute():
        calls.append("executed")
        return "raw observation"

    registry = ToolRegistry()
    if not reject:
        registry.register(
            ToolSpec(
                name="echo", description="echo", parameters={"type": "object", "properties": {}}
            ),
            execute,
        )
    handler = build_tool_handler(registry)
    runner = agent(scenario, "main")
    runner._tool_handler = handler
    # Use the public constructor to preserve normal provider-visible tool declarations.
    from opensquilla.provider import ToolDefinition, ToolInputSchema

    runner = Agent(
        provider=scenario.provider(_member("main").provider_config),
        config=AgentConfig(max_iterations=3),
        session_key="session",
        tool_handler=handler,
        tool_definitions=[
            ToolDefinition(name="echo", description="echo", input_schema=ToolInputSchema())
        ],
    )
    assert [event async for event in runner.run_turn("call tool")]
    rebuilt = graph(recording)
    assert not recording.failed_runs
    assert len(nodes(rebuilt, "tool_execution")) == len(calls) == int(not reject)
    assert len(nodes(rebuilt, "tool_rejection")) == int(reject)
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()), (
        rebuilt["scopes"],
        rebuilt["issues"],
    )


async def test_deleting_every_runtime_node_and_arbitrary_batch_order(recording, scenario):
    assert [event async for event in agent(scenario).run_turn("answer")]
    declarations = recording.identities.declarations()
    expected = ExecutionIdReconstructor().reconstruct(declarations)
    run = next(iter(expected["scopes"]))
    for omitted in declarations:
        partial = ExecutionIdReconstructor().reconstruct(
            [row for row in declarations if row is not omitted]
        )
        assert not partial["scopes"].get(run, {}).get("complete", False), omitted
    for seed in range(40):
        changed = copy.deepcopy(declarations * 2)
        random.Random(seed).shuffle(changed)
        assert ExecutionIdReconstructor().reconstruct(changed) == expected


async def test_zero_call_turns_are_ordered_without_synthetic_uploads(recording):
    from opensquilla.observability.piggyback.capture import trace_run

    @trace_run
    async def local(message, session_key):
        yield {"kind": "done"}

    for text in ("first", "second"):
        assert [event async for event in local(text, "S")]
    rebuilt = graph(recording)
    assert not rebuilt["positions"]
    turns = nodes(rebuilt, "turn")
    assert len(turns) == 2 and sum(row["previous_id"] is not None for row in turns) == 1
    assert all(scope["complete"] for scope in rebuilt["scopes"].values())


async def test_cancellation_during_http_has_a_closed_local_failure_scope(recording, scenario):
    import asyncio

    scenario.block = asyncio.Event()

    async def run():
        return [event async for event in agent(scenario).run_turn("waiting")]

    task = asyncio.create_task(run())
    await asyncio.wait_for(scenario.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rebuilt = graph(recording)
    assert len(rebuilt["positions"]) == len(scenario.requests) == 1
    assert nodes(rebuilt, "call_failure") and nodes(rebuilt, "run_cancelled")
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()), rebuilt["scopes"]


async def test_parent_closure_waits_for_delayed_subagent_and_late_receipt(recording, scenario):
    import asyncio

    from opensquilla.engine import ToolResult
    from opensquilla.engine.subagent import SubagentManager, SubagentSpec

    manager = SubagentManager()
    release = asyncio.Event()
    handles = []

    def factory(*args):
        child = agent(scenario)
        original = child.run_turn

        async def execute(*args, **kwargs):
            await release.wait()
            stream = original(*args, **kwargs)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()

        child.run_turn = execute
        return child

    async def tool(call):
        handles.append(await manager.spawn(SubagentSpec(task="later"), factory))
        return ToolResult(call.tool_use_id, call.tool_name, "scheduled")

    assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    pending = graph(recording)
    assert any(not scope["complete"] for scope in pending["scopes"].values())
    assert not nodes(pending, "wait")
    release.set()
    await handles[0].task
    completed = graph(recording)
    assert all(scope["complete"] for scope in completed["scopes"].values()), (
        completed["scopes"],
        completed["issues"],
    )
    assert len(scenario.requests) == 3
    assert not nodes(completed, "wait")  # observation outside an Agent is not an Agent wait


async def test_in_process_steering_adds_input_turn_ids_without_changing_old_call_owner(
    recording, scenario
):
    from opensquilla.engine import ToolResult
    from opensquilla.engine.agent_injection import ListPendingInputProvider

    pending = ListPendingInputProvider()
    pending.append("steer one")
    pending.append("steer two")

    async def tool(call):
        return ToolResult(call.tool_use_id, call.tool_name, "observed")

    runner = agent(scenario, "main", tool)
    assert [event async for event in runner.run_turn("initial", pending_input_provider=pending)]
    assert [event async for event in agent(scenario).run_turn("next user question")]
    rebuilt = graph(recording)
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()), (
        rebuilt["scopes"],
        rebuilt["issues"],
    )
    turns = nodes(rebuilt, "turn")
    assert len(turns) == 4
    roots = [row for row in turns if row["previous_id"] is None]
    assert len(roots) == 1
    visited = {roots[0]["id"]}
    while following := [
        row for row in turns if row["previous_id"] in visited and row["id"] not in visited
    ]:
        visited.update(row["id"] for row in following)
    assert len(visited) == 4
    assert len(scenario.requests) == 3
    groups = nodes(rebuilt, "input_group")
    assert any(len(row["turn_ids"]) == 2 for row in groups)


async def test_three_levels_of_subagents_close_without_reference_cycle_false_positives(
    recording, scenario
):
    from opensquilla.engine import ToolResult
    from opensquilla.engine.subagent import SubagentManager, SubagentSpec

    def make(depth):
        manager = SubagentManager()

        async def tool(call):
            child = await manager.spawn(SubagentSpec(task="nested"), lambda *args: make(depth - 1))
            return ToolResult(call.tool_use_id, call.tool_name, await child.task)

        return agent(scenario, "main", tool) if depth else agent(scenario)

    assert [event async for event in make(3).run_turn("delegate recursively")]
    rebuilt = graph(recording)
    assert len(rebuilt["positions"]) == len(scenario.requests) == 7
    assert all(scope["complete"] for scope in rebuilt["scopes"].values()), rebuilt["scopes"]
