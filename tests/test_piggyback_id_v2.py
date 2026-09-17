"""ID-only regression contracts with separate business/scheduling oracles."""

from __future__ import annotations

import asyncio
import json
import random
import socket

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.observability.piggyback.capture import context, trace_tool_handler
from opensquilla.observability.piggyback.id_capture import result_provenance, transform_result
from opensquilla.observability.piggyback.id_graph import IdTraceReconstructor, call_key
from opensquilla.observability.piggyback.platform import PlatformIngest
from opensquilla.observability.piggyback.protocol import ProtocolError, encode
from opensquilla.provider import Message, ToolDefinition, ToolInputSchema
from opensquilla.provider.types import MessageTraceIds
from tests.test_piggyback_causality import WireScenario
from tests.test_piggyback_id_graph import ObservedWire, collected, identity
from tests.test_piggyback_id_graph import recording as recording
from tests.test_provider_ensemble import _member


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def reject(*args, **kwargs):
        raise AssertionError("ID regression tests cannot open external connections")

    def socketpair(*args, **kwargs):
        # Windows emulates socketpair() with a loopback connect; the asyncio
        # event loop needs it for its self-pipe. That is not an external connection.
        socket.socket.connect = original_connect
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            socket.socket.connect = reject

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "socketpair", socketpair)


@pytest.fixture
async def scenario(tmp_path):
    result = WireScenario(tmp_path / "platform.db")
    result.app = ObservedWire(result.app)
    yield result
    await result.app.drain()
    result.store.close()


def agent(scenario, model="p2", tool=None):
    return Agent(
        provider=scenario.provider(_member(model).provider_config),
        config=AgentConfig(max_iterations=4),
        session_key="session",
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="delegate",
                input_schema=ToolInputSchema(),
            )
        ]
        if tool
        else [],
        tool_handler=trace_tool_handler(tool, None) if tool else None,
    )


def key(row):
    return call_key(row["source_id"], row["call_id"])


@pytest.mark.parametrize("conversion", ["direct", "str", "json", "fstring", "transform_api"])
@pytest.mark.parametrize("use_result", [True, False])
async def test_explicit_result_provenance_distinguishes_wait_from_use(
    recording,
    scenario,
    conversion,
    use_result,
):
    manager = SubagentManager()
    observed_child = []

    async def tool(call):
        handle = await manager.spawn(SubagentSpec(task="inspect"), lambda *args: agent(scenario))
        result = await handle.task
        observed_child.append(str(result))
        provenance = result_provenance(result if use_result else None)
        if not use_result:
            output = "independent result"
        elif conversion == "str":
            output = str(result)
        elif conversion == "json":
            output = json.dumps({"child": result})
        elif conversion == "fstring":
            output = f"Child: {result}"
        elif conversion == "transform_api":
            output = transform_result(result, lambda text: "Child: " + text)
        else:
            output = result
        return ToolResult(call.tool_use_id, call.tool_name, output, trace_ids=provenance)

    assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    assert len(rows) == len(scenario.requests) == 3
    _, child, following = rows
    assert (observed_child[0] in json.dumps(scenario.requests[-1]["messages"])) is use_result
    assert following["waited_call_ids"] == [key(child)]
    assert following["join_call_ids"] == ([key(child)] if use_result else [])
    assert (key(child) in following["context_parent_call_ids"]) is use_result
    assert following["unknown_context_call_ids"] == []
    graph = IdTraceReconstructor().reconstruct(rows)
    assert graph["references_resolved"] and graph["location_resolved"]
    assert [key(child), key(following), "waited_for"] in graph["edges"]
    assert ([key(child), key(following), "context_from"] in graph["edges"]) is use_result


@pytest.mark.parametrize("use_result", [True, False])
@pytest.mark.parametrize("conversion", ["str", "json", "fstring"])
async def test_opaque_conversion_reports_unknown_instead_of_guessing(
    recording, scenario, use_result, conversion
):
    manager = SubagentManager()

    async def tool(call):
        handle = await manager.spawn(SubagentSpec(task="inspect"), lambda *args: agent(scenario))
        result = await handle.task
        output = {
            "str": str,
            "json": lambda value: json.dumps({"child": value}),
            "fstring": lambda value: f"Child: {value}",
        }[conversion](result)
        if not use_result:
            output = "discarded child output"
        return ToolResult(call.tool_use_id, call.tool_name, output)

    assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    child, following = rows[1:]
    assert following["waited_call_ids"] == [key(child)]
    assert following["join_call_ids"] == []
    assert following["unknown_context_call_ids"] == [key(child)]
    graph = IdTraceReconstructor().reconstruct(rows)
    assert graph["calls"][key(following)]["provenance_coverage"] == "partial"
    assert graph["session_complete"] is None


async def test_child_completion_without_await_is_not_a_wait_or_use(recording, scenario):
    manager = SubagentManager()
    child_finished = asyncio.Event()

    def make_child(*args):
        child = agent(scenario)
        original = child.run_turn

        async def execute(*args, **kwargs):
            try:
                async for item in original(*args, **kwargs):
                    yield item
            finally:
                child_finished.set()

        child.run_turn = execute
        return child

    async def tool(call):
        await manager.spawn(SubagentSpec(task="inspect"), make_child)
        # Scheduling barrier, deliberately does not retrieve child task's result.
        await asyncio.wait_for(child_finished.wait(), 2)
        return ToolResult(call.tool_use_id, call.tool_name, "queued")

    assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    assert len(rows) == 3
    assert rows[-1]["waited_call_ids"] == rows[-1]["join_call_ids"] == []
    assert key(rows[1]) not in rows[-1]["context_parent_call_ids"]


@pytest.mark.parametrize("roundtrip", ["json", "dict"])
async def test_persisted_history_restores_sources_without_changing_wire(
    recording,
    scenario,
    roundtrip,
):
    first = agent(scenario)
    assert [event async for event in first.run_turn("first")]
    history = first.history_snapshot()
    restored = [
        Message.model_validate_json(m.model_dump_json())
        if roundtrip == "json"
        else Message.model_validate(m.model_dump())
        for m in history
    ]
    second = agent(scenario)
    second.set_history(restored)
    assert [event async for event in second.run_turn("second")]
    rows = collected(scenario.app)
    assert rows[1]["context_parent_call_ids"] == [key(rows[0])]
    assert rows[0]["user_turn_id"] in rows[1]["input_turn_ids"]
    assert all("trace_ids" not in m for request in scenario.requests for m in request["messages"])
    assert len(rows) == 2


async def test_gateway_dispatch_survives_json_rehydration_and_parent_exit(recording, scenario):
    from tests.test_gateway.test_task_runtime_terminal_message import _make_envelope, _make_runtime

    snapshots = []

    async def handler(run):
        assert [event async for event in agent(scenario).run_turn("child")]

    first = _make_runtime(handler)

    async def tool(call):
        reservation = await first.reserve(_make_envelope("child"), "child", run_kind="subagent")
        snapshots.append(
            json.loads(json.dumps(reservation.task_record.details["trace_dispatch_scope"]))
        )
        await first.abort_reservation(reservation)
        return ToolResult(call.tool_use_id, call.tool_name, "reserved")

    try:
        assert [event async for event in agent(scenario, "main", tool).run_turn("delegate")]
    finally:
        await first.shutdown()
    # The original live parent RunIds is gone. Only the durable JSON snapshot remains.
    assert not context.get().get("_id_state")
    second = _make_runtime(handler)
    try:
        reservation = await second.reserve(
            _make_envelope("child"),
            "child",
            run_kind="subagent",
            _trace_dispatch_scope=snapshots[0],
        )
        await second._storage.create_agent_task(reservation.task_record)
        handle = await second.activate(reservation)
        await second.wait(handle.task_id, timeout=3)
        rows = collected(scenario.app)
        assert len(rows) == 3
        assert rows[2]["parent_call_id"] == key(rows[0])
        assert rows[2]["parent_call_id"] != key(rows[1])
        assert rows[2]["dispatch_id"] == snapshots[0]["dispatch_id"]
        assert IdTraceReconstructor().reconstruct(rows)["location_resolved"]
    finally:
        await second.shutdown()


@pytest.mark.parametrize("order", [False, True])
def test_conflict_survives_platform_restart_and_does_not_leak_tenant(tmp_path, order):
    path = tmp_path / "platform.db"
    first = identity("C")
    second = {**first, "run_id": "OTHER", "run_path_ids": ["OTHER"]}
    rows = [first, second]
    if order:
        rows.reverse()
    store = PlatformIngest(path)
    try:
        store.accept(
            "tenant",
            encode("SRC", "B1", [{"id": "call_ids:C", "type": "call_ids", "value": rows[0]}]),
        )
        with pytest.raises(ProtocolError, match="conflict"):
            store.accept(
                "tenant",
                encode("SRC", "B2", [{"id": "call_ids:C", "type": "call_ids", "value": rows[1]}]),
            )
    finally:
        store.close()
    reopened = PlatformIngest(path)
    try:
        graph = IdTraceReconstructor().reconstruct(reopened.call_identities("tenant"))
        assert graph["conflicting_call_ids"] == ["SRC:C"]
        assert not graph["location_resolved"]
        assert reopened.call_identities("another-tenant") == []
    finally:
        reopened.close()


@pytest.mark.parametrize("missing", ["user_turn_id", "iteration_id"])
async def test_auxiliary_null_scope_is_legitimate_but_loop_missing_scope_is_not(
    recording,
    scenario,
    missing,
):
    provider = scenario.provider(_member("p2").provider_config)
    assert [event async for event in provider.chat([Message(role="user", content="system job")])]
    row = collected(scenario.app)[0]
    assert row["user_turn_id"] is None and row["iteration_id"] is None
    assert row["system_activation_id"] and row["auxiliary_phase_id"]
    assert IdTraceReconstructor().reconstruct([row])["location_resolved"]
    damaged = identity("C")
    damaged[missing] = None
    assert not IdTraceReconstructor().reconstruct([damaged])["location_resolved"]


def test_seeded_id_graph_mutations_have_deterministic_diagnostics():
    for seed in range(50):
        rng = random.Random(seed)
        count = rng.randint(3, 30)
        rows = [
            identity(
                f"C{i}", iteration=f"I{i}", logical=f"L{i}", previous=f"C{i - 1}" if i else None
            )
            for i in range(count)
        ]
        for i in range(1, count):
            rows[i]["previous_iteration_id"] = f"I{i - 1}"
        good = IdTraceReconstructor().reconstruct(rows)
        assert good["location_resolved"]
        index = rng.randrange(1, count)
        broken = json.loads(json.dumps(rows))
        broken[index]["retry_of_call_id"] = "SRC:C0"  # not the same logical operation
        result = IdTraceReconstructor().reconstruct(broken)
        assert not result["location_resolved"]
        rng.shuffle(broken)
        assert IdTraceReconstructor().reconstruct(broken * 2) == result


def test_provenance_envelope_is_strict_and_business_projection_is_explicit():
    provenance = MessageTraceIds(message_id="M1", source_call_ids=("SRC:C1",))
    message = Message(
        role="user", content='literal {"trace_ids":"keep user text"}', trace_ids=provenance
    )
    assert Message.model_validate_json(message.model_dump_json()).trace_ids == provenance
    assert message.business_dump() == {"role": "user", "content": message.content}
    with pytest.raises(ValueError):
        MessageTraceIds.model_validate({"message_id": "M1", "messages": ["not IDs"]})


@pytest.mark.parametrize("with_tool", [False, True])
async def test_native_session_storage_reload_retains_each_iteration_source(
    recording,
    scenario,
    tmp_path,
    with_tool,
):
    from opensquilla.engine.history import reconstruct_messages_from_entry
    from opensquilla.observability.piggyback.capture import trace_run
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage

    path = str(tmp_path / "native-sessions.db")
    storage = SessionStorage(path)
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:main")

    async def tool(call):
        return ToolResult(call.tool_use_id, call.tool_name, "observed tool output")

    @trace_run
    async def write_turn(message, session_key, expected_session_id):
        await manager.append_message(session_key, "user", message)
        runner = agent(scenario, "main" if with_tool else "p2", tool if with_tool else None)
        async for event in runner.run_turn(message):
            yield event
        # Native persistence's flattened transcript representation. This is
        # built from business messages, independently of trace metadata.
        segments = []
        final_text = ""
        for item in runner.history_snapshot()[1:]:
            if isinstance(item.content, str):
                if item.role == "assistant":
                    segments.append({"type": "text", "text": item.content})
                    final_text = item.content
            else:
                for block in item.content:
                    data = block.model_dump(mode="json", exclude_none=True)
                    segments.append(data)
                    if data.get("type") == "text":
                        final_text = data["text"]
        await manager.append_message(
            session_key,
            "assistant",
            final_text,
            tool_calls=segments if with_tool else None,
        )

    try:
        assert [event async for event in write_turn("original", node.session_key, node.session_id)]
    finally:
        await storage.close()
    previous_calls = collected(scenario.app)
    reopened = SessionStorage(path)
    await reopened.connect()
    try:
        rows = await SessionManager(reopened, inject_time_prefix=False).read_transcript(
            node.session_key
        )
        history = [
            message
            for row in rows
            for message in reconstruct_messages_from_entry(
                row["role"],
                row.get("content"),
                row.get("tool_calls"),
                row.get("reasoning_content"),
                turn_context=row.get("turn_context"),
            )
        ]
        assert history and all(message.trace_ids is not None for message in history)

        @trace_run
        async def read_turn(message, session_key, expected_session_id):
            runner = agent(scenario)
            runner.set_history(history)
            async for event in runner.run_turn(message):
                yield event

        assert [event async for event in read_turn("next", node.session_key, node.session_id)]
        following = collected(scenario.app)[-1]
        assert set(following["context_parent_call_ids"]) == {key(row) for row in previous_calls}
        assert previous_calls[0]["user_turn_id"] in following["input_turn_ids"]
        assert IdTraceReconstructor().reconstruct(collected(scenario.app))["location_resolved"]
    finally:
        await reopened.close()


async def test_subrun_in_another_session_keeps_original_turn_owner(recording, scenario):
    from opensquilla.observability.piggyback.capture import trace_run

    @trace_run
    async def child(message, expected_session_id):
        async for item in agent(scenario).run_turn(message):
            yield item

    async def tool(call):
        assert [item async for item in child("child input", "CHILD-SESSION")]
        return ToolResult(call.tool_use_id, call.tool_name, "child completed")

    assert [item async for item in agent(scenario, "main", tool).run_turn("parent input")]
    rows = collected(scenario.app)
    assert rows[1]["session_id"] == "CHILD-SESSION"
    assert rows[1]["user_turn_id"] == rows[0]["user_turn_id"]
    graph = IdTraceReconstructor().reconstruct(rows)
    assert graph["location_resolved"], graph["issues"]


async def test_legacy_history_without_source_metadata_cannot_be_declared_complete(
    recording, scenario
):
    runner = agent(scenario)
    runner.set_history([Message(role="assistant", content="legacy output")])
    assert [item async for item in runner.run_turn("next")]
    row = collected(scenario.app)[0]
    graph = IdTraceReconstructor().reconstruct([row])
    assert graph["calls"][key(row)]["provenance_coverage"] == "partial"


@pytest.mark.parametrize("count", [0, 1, 140])
async def test_zero_call_turns_beyond_inline_window_are_uploaded_as_ids(recording, scenario, count):
    from opensquilla.observability.piggyback.capture import trace_run

    @trace_run
    async def local_turn(message, session_key="session"):
        yield {"kind": "done"}

    for index in range(count):
        assert [item async for item in local_turn(str(index))]
    assert scenario.requests == []
    assert [item async for item in agent(scenario).run_turn("actual business call")]
    await scenario.app.drain()
    rows = scenario.store.call_identities("tenant")
    turns = scenario.store.turn_identities("tenant")
    graph = IdTraceReconstructor().reconstruct(rows + turns)
    assert len(rows) == len(scenario.requests) == 1
    assert len(graph["turns"]) == count + 1
    assert graph["location_resolved"], graph["missing_ids"]
    if count > 128:
        assert not IdTraceReconstructor().reconstruct(rows)["references_resolved"]
    scenario.store.db.execute("DELETE FROM records WHERE type NOT IN ('call_ids','turn_ids')")
    scenario.store.db.execute("DELETE FROM observations")
    assert (
        IdTraceReconstructor().reconstruct(
            scenario.store.call_identities("tenant") + scenario.store.turn_identities("tenant"),
        )
        == graph
    )


def test_turn_conflict_is_retained_without_any_model_call(tmp_path):
    store = PlatformIngest(tmp_path / "platform.db")
    first = {
        "version": 1,
        "source_id": "SRC",
        "session_id": "S1",
        "user_turn_id": "U1",
        "previous_user_turn_id": None,
    }
    second = {**first, "session_id": "S2"}
    try:
        for index, row in enumerate((first, second)):
            batch = encode(
                "SRC", f"B{index}", [{"id": "turn_ids:U1", "type": "turn_ids", "value": row}]
            )
            if index:
                with pytest.raises(ProtocolError, match="record_identity_conflict"):
                    store.accept("tenant", batch)
            else:
                store.accept("tenant", batch)
        rows = store.turn_identities("tenant")
        graph = IdTraceReconstructor().reconstruct(rows)
        assert not graph["references_resolved"]
        assert ["scope_conflict", "SRC:U1"] in graph["issues"]
        assert IdTraceReconstructor().reconstruct(list(reversed(rows))) == graph
    finally:
        store.close()


def test_native_history_keyword_api_and_changed_segmentation_are_safe():
    from opensquilla.engine.history import reconstruct_messages_from_entry

    ids = MessageTraceIds(message_id="M1", source_call_ids=("SRC:C1",))
    metadata = {
        "trace_history_ids": [
            {"shape": ["assistant", [["tool_use", "T1"]]], "trace_ids": ids.model_dump()}
        ]
    }
    messages = reconstruct_messages_from_entry(
        role="assistant",
        content="projected text",
        tool_calls=None,
        turn_context=metadata,
    )
    assert messages[0].trace_ids.source_call_ids == ()
    assert messages[0].trace_ids.unknown_call_ids == ("SRC:C1",)


@pytest.mark.parametrize("field", ["phase_id", "logical_call_id"])
async def test_v2_scope_ids_cannot_be_reused_by_a_different_run(recording, scenario, field):
    runner = agent(scenario)
    assert [item async for item in runner.run_turn("first")]
    assert [item async for item in runner.run_turn("second")]
    rows = json.loads(json.dumps(collected(scenario.app)))
    assert rows[0]["run_id"] != rows[1]["run_id"]
    rows[1][field] = rows[0][field]
    result = IdTraceReconstructor().reconstruct(rows)
    assert not result["location_resolved"], f"duplicate {field} silently acquired two owners"
    assert IdTraceReconstructor().reconstruct(list(reversed(rows))) == result


async def test_manager_barrier_records_wait_without_inventing_adoption(recording, scenario):
    manager = SubagentManager()

    async def tool(call):
        await manager.spawn(SubagentSpec(task="inspect"), lambda *args: agent(scenario))
        await manager.wait_all(timeout=3)
        return ToolResult(
            call.tool_use_id, call.tool_name, "independent", trace_ids=result_provenance()
        )

    assert [item async for item in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    assert rows[-1]["waited_call_ids"] == [key(rows[1])]
    assert rows[-1]["join_call_ids"] == []


async def test_child_local_terminal_output_does_not_inherit_prior_http_call(recording, scenario):
    from opensquilla.provider.types import DoneEvent, TextDeltaEvent

    manager = SubagentManager()
    child_history = []

    def child_factory(*args):
        async def echo(call):
            return ToolResult(call.tool_use_id, call.tool_name, "observation")

        child = agent(scenario, "main", echo)
        original = child.provider._chat
        invocations = 0

        async def local_second(messages, **kwargs):
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                async for event in original(messages, **kwargs):
                    yield event
            else:
                yield TextDeltaEvent(text="local final output")
                yield DoneEvent(stop_reason="stop", model="local")

        child.provider._chat = local_second
        child_history.append(child)
        return child

    async def tool(call):
        handle = await manager.spawn(SubagentSpec(task="inspect"), child_factory)
        result = await handle.task
        return ToolResult(call.tool_use_id, call.tool_name, result)

    assert [item async for item in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    assert len(rows) == len(scenario.requests) == 3
    assert "local final output" in json.dumps(scenario.requests[-1]["messages"])
    local = child_history[0].history_snapshot()[-1].trace_ids
    assert local.source_call_ids == ()
    assert local.unknown_message_ids
    assert rows[-1]["join_call_ids"] == []
    assert rows[-1]["unknown_message_ids"]


@pytest.mark.parametrize("use_result", [True, False])
async def test_gather_adapter_keeps_multiple_waits_separate_from_adoption(
    recording, scenario, use_result
):
    from opensquilla.observability.piggyback.id_capture import await_child_results

    manager = SubagentManager()

    async def tool(call):
        handles = [
            await manager.spawn(
                SubagentSpec(task=f"inspect {index}"), lambda *args: agent(scenario)
            )
            for index in range(2)
        ]
        results = await await_child_results(asyncio.gather(*(handle.task for handle in handles)))
        provenance = result_provenance(results if use_result else None)
        content = json.dumps(results) if use_result else "independent result"
        return ToolResult(call.tool_use_id, call.tool_name, content, trace_ids=provenance)

    assert [item async for item in agent(scenario, "main", tool).run_turn("delegate")]
    rows = collected(scenario.app)
    assert len(rows) == 4
    child_ids = sorted(key(row) for row in rows[1:3])
    assert rows[-1]["waited_call_ids"] == child_ids
    assert rows[-1]["join_call_ids"] == (child_ids if use_result else [])
    assert len({row["dispatch_id"] for row in rows[1:3]}) == 2
    graph = IdTraceReconstructor().reconstruct(rows)
    assert graph["location_resolved"], graph["issues"]


async def test_call_lookup_does_not_merge_same_session_strings_from_other_sources(tmp_path):
    import httpx

    from opensquilla.observability.piggyback.protocol import digest
    from opensquilla.observability.piggyback.server import create_platform

    path = tmp_path / "platform.db"
    store = PlatformIngest(path)
    try:
        for index, source in enumerate(("SOURCE-A", "SOURCE-B")):
            row = {**identity(f"C{index}"), "source_id": source}
            store.accept(
                "tenant",
                encode(
                    source,
                    f"B{index}",
                    [{"id": f"call_ids:C{index}", "type": "call_ids", "value": row}],
                ),
            )
    finally:
        store.close()
    app = create_platform(
        {
            "sqlite_path": str(path),
            "token_hash_to_tenant": {digest(b"key"): "tenant"},
            "upstream_base_url": "https://unused.test",
        }
    )
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://platform.test"
        ) as client:
            response = await client.get("/calls/C0/ids", headers={"Authorization": "Bearer key"})
            assert response.status_code == 200
            assert list(response.json()["calls"]) == ["SOURCE-A:C0"]
