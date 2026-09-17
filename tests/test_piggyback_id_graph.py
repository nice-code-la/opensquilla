"""The reconstruction oracle never supplies event bodies, model text or timestamps."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
from pathlib import Path

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.observability.piggyback.capture import (
    Capture,
    _active,
    context,
    trace_tool_handler,
)
from opensquilla.observability.piggyback.id_graph import IdTraceReconstructor, call_key
from opensquilla.observability.piggyback.platform import TraceMiddleware
from opensquilla.observability.piggyback.protocol import (
    FIELD,
    IDS_ACK,
    digest,
)
from opensquilla.observability.piggyback.server import create_platform
from opensquilla.observability.piggyback.transport import Destination
from opensquilla.provider import ToolDefinition, ToolInputSchema
from tests.test_piggyback_causality import WireScenario
from tests.test_provider_ensemble import _member


@pytest.fixture
def recording(tmp_path):
    cap = Capture(
        tmp_path / "client", Destination("https://platform.test", "tenant", digest(b"key"))
    )
    tokens = _active.set(cap), context.set({})
    yield cap
    context.reset(tokens[1])
    _active.reset(tokens[0])
    cap.journal.close()


def identity(
    call,
    *,
    run="R1",
    turn="U1",
    iteration="I1",
    logical="L1",
    previous=None,
    retry=None,
    parent=None,
    parent_run=None,
    joins=(),
    sources=(),
    prior_turn=None,
):
    return {
        "version": 1,
        "source_id": "SRC",
        "session_id": "S1",
        "user_turn_id": turn,
        "previous_user_turn_id": prior_turn,
        "trace_id": "TR-" + turn,
        "run_id": run,
        "parent_run_id": parent_run,
        "iteration_id": iteration,
        "previous_iteration_id": None,
        "logical_call_id": logical,
        "outer_attempt_id": "A-" + call,
        "call_id": call,
        "context_id": "CTX-" + call,
        "previous_call_id": "SRC:" + previous if previous else None,
        "parent_call_id": "SRC:" + parent if parent else None,
        "retry_of_call_id": "SRC:" + retry if retry else None,
        "join_call_ids": sorted("SRC:" + value for value in joins),
        "context_parent_call_ids": sorted("SRC:" + value for value in sources),
        "input_turn_ids": [turn],
        "run_path_ids": ([parent_run] if parent_run else []) + [run],
    }


def test_stitch_five_calls_using_only_ids_in_any_arrival_order():
    rows = [
        identity("C1"),
        identity("C2", run="R2", iteration="IC", logical="LC", parent="C1", parent_run="R1"),
        identity(
            "C3", iteration="I2", logical="L2", previous="C1", joins=["C2"], sources=["C1", "C2"]
        ),
        identity(
            "C4",
            iteration="I2",
            logical="L2",
            previous="C3",
            retry="C3",
            joins=["C2"],
            sources=["C1", "C2"],
        ),
        identity(
            "C5", run="R3", turn="U2", iteration="IN", logical="LN", prior_turn="U1", sources=["C4"]
        ),
    ]
    rows[2]["previous_iteration_id"] = rows[3]["previous_iteration_id"] = "I1"
    graph = IdTraceReconstructor().reconstruct(rows)
    assert graph["references_resolved"]
    assert graph["session_complete"] is None
    assert ["SRC:C1", "SRC:C2", "spawned_by"] in graph["edges"]
    assert ["SRC:C2", "SRC:C3", "joins"] in graph["edges"]
    assert ["SRC:C3", "SRC:C4", "retry_of"] in graph["edges"]
    assert ["SRC:C4", "SRC:C5", "context_from"] in graph["edges"]
    assert graph["turn_edges"] == [["SRC:U1", "SRC:U2", "next_user_turn"]]
    for seed in range(20):
        shuffled = copy.deepcopy(rows + rows)
        random.Random(seed).shuffle(shuffled)
        assert IdTraceReconstructor().reconstruct(shuffled) == graph


def test_ids_only_detect_missing_conflict_cycle_and_wrong_retry():
    c1, c2 = identity("C1"), identity("C2", previous="C1", retry="C1")
    missing = IdTraceReconstructor().reconstruct([c2])
    assert missing["missing_ids"] == ["SRC:C1"] and not missing["references_resolved"]
    bad = {**c1, "user_turn_id": "DIFFERENT"}
    for rows in ([c1, bad], [bad, c1]):
        result = IdTraceReconstructor().reconstruct(rows)
        assert result["conflicting_call_ids"] == ["SRC:C1"]
        assert result["calls"] == {}
    result = IdTraceReconstructor().reconstruct([{**c1, "previous_call_id": "SRC:C2"}, c2])
    assert ["call_cycle", "SRC:C1"] in result["issues"]
    result = IdTraceReconstructor().reconstruct([c1, {**c2, "logical_call_id": "DIFFERENT"}])
    assert ["wrong_retry_operation", "SRC:C2"] in result["issues"]


def test_id_reconstructor_rejects_prose_or_events_in_input():
    # Strict schema ensures the ID-only test cannot accidentally hide an event graph.
    result = IdTraceReconstructor().reconstruct([{**identity("C1"), "messages": []}])
    assert result["invalid_identity_hashes"] and not result["calls"]


class ObservedWire:
    def __init__(self, target):
        self.target = target
        self.requests = []
        self.response_headers = []

    async def __call__(self, scope, receive, send):
        body = bytearray()
        while True:
            item = await receive()
            body.extend(item.get("body", b""))
            if not item.get("more_body"):
                break
        self.requests.append(json.loads(body))
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body)}
            return await receive()

        async def observe_send(message):
            if message["type"] == "http.response.start":
                self.response_headers.append(dict(message.get("headers", [])))
            await send(message)

        return await self.target(scope, replay, observe_send)

    async def drain(self):
        await self.target.drain()


def collected(wire):
    return [request[FIELD]["call_ids"] for request in wire.requests]


async def test_real_agent_two_turns_emit_self_contained_ids(recording, tmp_path):
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)
    agent = Agent(
        provider=scenario.provider(_member("p2").provider_config),
        config=AgentConfig(max_iterations=2),
        session_key="session",
    )
    try:
        assert [e async for e in agent.run_turn("first")]
        assert [e async for e in agent.run_turn("second")]
        await wire.drain()
        rows = collected(wire)
        assert len(rows) == len(scenario.requests) == 2
        first, second = rows
        assert second["previous_user_turn_id"] == first["user_turn_id"]
        assert second["context_parent_call_ids"] == [call_key(first["source_id"], first["call_id"])]
        # Delete all detailed records from the platform. Query remains fully operational.
        scenario.store.db.execute("DELETE FROM records WHERE type!='call_ids'")
        scenario.store.db.execute("DELETE FROM observations")
        received = scenario.store.call_identities("tenant")
        assert IdTraceReconstructor().reconstruct(received) == IdTraceReconstructor().reconstruct(
            rows
        )
        assert not scenario.store.call_identities("different-tenant")
    finally:
        await wire.drain()
        scenario.store.close()


async def test_real_subagent_join_and_parent_loop_need_no_event_graph(recording, tmp_path):
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)
    manager = SubagentManager()

    def factory(spec, depth, run_id):
        return Agent(
            provider=scenario.provider(_member("p2").provider_config),
            config=AgentConfig(max_iterations=2),
            session_key="session",
        )

    async def tool(call):
        handle = await manager.spawn(SubagentSpec(task="inspect"), factory)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=await handle.task
        )

    definition = ToolDefinition(name="echo", description="inspect", input_schema=ToolInputSchema())
    agent = Agent(
        provider=scenario.provider(_member("main").provider_config),
        config=AgentConfig(max_iterations=3),
        session_key="session",
        tool_definitions=[definition],
        tool_handler=trace_tool_handler(tool, None),
    )
    try:
        assert [e async for e in agent.run_turn("delegate")]
        await wire.drain()
        rows = collected(wire)
        assert len(rows) == 3
        parent, child, following = rows
        pk = call_key(parent["source_id"], parent["call_id"])
        ck = call_key(child["source_id"], child["call_id"])
        assert child["parent_call_id"] == pk
        assert child["parent_run_id"] == parent["run_id"]
        assert child["user_turn_id"] == parent["user_turn_id"]
        assert following["previous_call_id"] == pk
        assert following["join_call_ids"] == [ck]
        graph = IdTraceReconstructor().reconstruct(rows)
        assert graph["references_resolved"], graph
    finally:
        await wire.drain()
        scenario.store.close()


async def test_actual_openai_stream_fallback_has_direct_retry_id(recording, monkeypatch):
    from opensquilla.provider.openai import OpenAIProvider

    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        if body.get("stream"):
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(upstream)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    # Use the same real Provider setup as the previously failing audit.
    from dataclasses import replace

    from opensquilla.provider.compat_policy import compat_policy_for_kind

    provider = OpenAIProvider(
        api_key="key",
        model="test-model",
        base_url="https://platform.test",
        compat=replace(compat_policy_for_kind("openai"), stream_timeout_fallback=True),
    )
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=2), session_key="session")
    assert [e async for e in agent.run_turn("answer")]
    assert len(requests) == 2
    rows = [request[FIELD]["call_ids"] for request in requests]
    first, second = rows
    assert second["retry_of_call_id"] == call_key(first["source_id"], first["call_id"])
    assert second["logical_call_id"] == first["logical_call_id"]
    assert second["call_id"] != first["call_id"]
    assert IdTraceReconstructor().reconstruct(rows)["references_resolved"]


async def test_id_only_api_does_not_read_events_or_content(recording, tmp_path, monkeypatch):
    path = tmp_path / "platform.db"
    scenario = WireScenario(path)
    scenario.app = ObservedWire(scenario.app)
    agent = Agent(
        provider=scenario.provider(_member("p2").provider_config),
        config=AgentConfig(max_iterations=2),
        session_key="session",
    )
    try:
        assert [e async for e in agent.run_turn("one")]
        await scenario.app.drain()
        row = collected(scenario.app)[0]
        scenario.store.db.execute("DELETE FROM records WHERE type!='call_ids'")
        scenario.store.db.execute("DELETE FROM observations")
        from opensquilla.observability.piggyback.environment import RecordedContentStore
        from opensquilla.observability.piggyback.reconstruct import TraceReconstructor

        def forbidden(*args, **kwargs):
            raise AssertionError("ID-only API accessed detailed reconstruction/content")

        monkeypatch.setattr(TraceReconstructor, "reconstruct", forbidden)
        monkeypatch.setattr(RecordedContentStore, "get", forbidden)
        app = create_platform(
            {
                "sqlite_path": str(path),
                "token_hash_to_tenant": {digest(b"key"): "tenant", digest(b"other"): "other"},
                "upstream_base_url": "https://unused.test",
            }
        )
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://platform.test"
            ) as client:
                url = f"/calls/{row['call_id']}/ids"
                assert (await client.get(url)).status_code == 401
                assert (
                    await client.get(url, headers={"Authorization": "Bearer other"})
                ).status_code == 404
                response = await client.get(url, headers={"Authorization": "Bearer key"})
                assert response.status_code == 200
                graph = response.json()
                assert graph["references_resolved"]
                assert graph["calls"][graph["selected_call_id"]]["ids"] == row
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_parallel_agent_runs_do_not_share_call_cursor(recording, tmp_path):
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)

    async def run(model):
        agent = Agent(
            provider=scenario.provider(_member(model).provider_config),
            config=AgentConfig(max_iterations=2),
            session_key="session",
        )
        assert [e async for e in agent.run_turn("identical text")]

    try:
        await asyncio.gather(run("p2"), run("p3"))
        await wire.drain()
        rows = collected(wire)
        assert len(rows) == 2 and len({r["call_id"] for r in rows}) == 2
        assert len({r["user_turn_id"] for r in rows}) == 2
        assert all(r["previous_call_id"] is None and r["parent_call_id"] is None for r in rows)
        assert IdTraceReconstructor().reconstruct(rows)["references_resolved"]
    finally:
        await wire.drain()
        scenario.store.close()


async def test_current_ids_bypass_an_old_batch_without_extra_calls(
    recording, tmp_path, monkeypatch
):
    from opensquilla.observability.piggyback.protocol import encode

    old_event = recording.emit("fixture.old_backlog", {})
    old = encode(
        recording.journal.source_id,
        "stuck-batch",
        [{"id": old_event["event_id"], "type": "event", "value": old_event}],
    )
    monkeypatch.setattr(recording.transport, "prepare", lambda *args: old)
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)
    agent = Agent(
        provider=scenario.provider(_member("p2").provider_config),
        config=AgentConfig(max_iterations=2),
        session_key="session",
    )
    try:
        assert [e async for e in agent.run_turn("first real task")]
        assert [e async for e in agent.run_turn("second real task")]
        await wire.drain()
        assert len(wire.requests) == 2
        assert all(row[FIELD]["backlog"] == old for row in wire.requests)
        received = scenario.store.call_identities("tenant")
        assert len(received) == 2
        assert IdTraceReconstructor().reconstruct(received)["references_resolved"]
    finally:
        await wire.drain()
        scenario.store.close()


async def test_failed_ingestion_does_not_ack_ids_or_retry_model(recording, tmp_path, monkeypatch):
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)
    original_accept = scenario.store.accept

    def fail(*args):
        raise OSError("fixture storage failure")

    monkeypatch.setattr(scenario.store, "accept", fail)
    agent = Agent(
        provider=scenario.provider(_member("p2").provider_config),
        config=AgentConfig(max_iterations=2),
        session_key="session",
    )
    try:
        assert [e async for e in agent.run_turn("first task")]
        await wire.drain()
        assert len(wire.requests) == 1
        assert IDS_ACK.encode() not in wire.response_headers[0]
        assert not scenario.store.call_identities("tenant")
        pending = recording.journal.db.execute(
            "SELECT acknowledged FROM records WHERE type='call_ids'"
        ).fetchall()
        assert [row[0] for row in pending] == [0]
        monkeypatch.setattr(scenario.store, "accept", original_accept)
        assert [e async for e in agent.run_turn("second task")]
        await wire.drain()
        assert len(wire.requests) == 2
        assert len(scenario.store.call_identities("tenant")) == 2
    finally:
        await wire.drain()
        scenario.store.close()


def test_missing_direct_id_is_rejected_without_using_an_event_oracle():
    first = identity("C1")
    # Both physical sends declare one logical operation. A missing retry ID cannot
    # be treated as a second unrelated root even though all supplied IDs exist.
    second = identity("C2", previous="C1")
    graph = IdTraceReconstructor().reconstruct([first, second])
    assert ["unlinked_physical_attempts", "SRC:C2"] in graph["issues"]
    assert not graph["references_resolved"]


@pytest.mark.parametrize("mutation", ["hash", "source", "prose"])
async def test_malformed_id_extension_is_stripped_without_breaking_model(tmp_path, mutation):
    from opensquilla.observability.piggyback.protocol import make_carrier

    row = identity("C1")
    carrier = make_carrier({"id": "call_ids:C1", "type": "call_ids", "value": row}, None)
    if mutation == "hash":
        carrier["call_ids_sha256"] = "0" * 64
    elif mutation == "source":
        carrier["source_id"] = "different-source"
    else:
        carrier["call_ids"]["messages"] = ["not an ID"]
    scenario = WireScenario(tmp_path / "platform.db")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=scenario.app), base_url="https://platform.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                json={"model": "p2", "messages": [], FIELD: carrier},
            )
        await scenario.app.drain()
        assert response.status_code == 200
        assert IDS_ACK not in response.headers
        assert scenario.requests == [{"model": "p2", "messages": []}]
        assert not scenario.store.call_identities("tenant")
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_session_turn_predecessor_survives_capture_restart(recording, tmp_path):
    scenario = WireScenario(tmp_path / "platform.db")
    wire = scenario.app = ObservedWire(scenario.app)
    try:
        agent = Agent(
            provider=scenario.provider(_member("p2").provider_config),
            config=AgentConfig(max_iterations=2),
            session_key="session",
        )
        assert [e async for e in agent.run_turn("before restart")]
        replacement = Capture(tmp_path / "client", recording.destination)
        token = _active.set(replacement)
        try:
            assert [e async for e in agent.run_turn("after restart")]
        finally:
            _active.reset(token)
            replacement.journal.close()
        rows = collected(wire)
        assert rows[0]["source_id"] == rows[1]["source_id"]
        assert rows[1]["previous_user_turn_id"] == rows[0]["user_turn_id"]
        assert IdTraceReconstructor().reconstruct(rows)["references_resolved"]
    finally:
        await wire.drain()
        scenario.store.close()


async def test_five_real_provider_calls_reconstruct_from_platform_ids_only(
    recording, tmp_path, monkeypatch
):
    """Two turns, native child run and actual Provider fallback in a single session."""
    from dataclasses import replace

    from opensquilla.observability.piggyback.platform import PlatformIngest, token_authenticator
    from opensquilla.provider.compat_policy import compat_policy_for_kind
    from opensquilla.provider.openai import OpenAIProvider

    original_client = httpx.AsyncClient
    requests, attempts, connections = [], {}, []
    store = PlatformIngest(tmp_path / "e2e-platform.db")

    def deny_connect(*args, **kwargs):
        connections.append(True)
        raise AssertionError("ID trace fixture attempted an external socket")

    monkeypatch.setattr("socket.socket.connect", deny_connect)

    async def model(scope, receive, send):
        body = json.loads((await receive())["body"])
        assert FIELD not in body
        # Simulated normal model latency gives the independent durable ingester time.
        await asyncio.sleep(0.01)
        tool = body["model"] == "main" and attempts["main"] == 1
        text = "child result" if body["model"] == "child" else "accepted answer"
        if body.get("stream"):
            delta = (
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "tool-1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ],
                }
                if tool
                else {"content": text}
            )
            frames = [
                {
                    "id": "response",
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            ]
            response = (
                "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            )
            media = b"text/event-stream"
        else:
            response = json.dumps(
                {
                    "id": "response",
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            )
            media = b"application/json"
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", media)]}
        )
        await send({"type": "http.response.body", "body": response.encode()})

    app = TraceMiddleware(model, store, token_authenticator({digest(b"key"): "tenant"}))
    asgi = httpx.ASGITransport(app=app)

    async def dispatch(request):
        body = json.loads(request.content)
        requests.append(body)
        attempts[body["model"]] = attempts.get(body["model"], 0) + 1
        if body["model"] == "main" and attempts["main"] == 2:
            # This request's IDs are not received now; only a later business call can carry them.
            raise httpx.ReadTimeout("scripted stream failure", request=request)
        return await asgi.handle_async_request(request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(dispatch)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.delenv("OPENSQUILLA_TRACE_CONFIG", raising=False)

    async def execute():
        manager = SubagentManager()

        def provider(model):
            return OpenAIProvider(
                api_key="key",
                model=model,
                base_url="https://platform.test",
                compat=replace(compat_policy_for_kind("openai"), stream_timeout_fallback=True),
            )

        def factory(spec, depth, run_id):
            return Agent(
                provider=provider("child"), config=AgentConfig(max_iterations=2), session_key="e2e"
            )

        async def tool(call):
            handle = await manager.spawn(SubagentSpec(task="inspect the task"), factory)
            return ToolResult(call.tool_use_id, call.tool_name, await handle.task)

        agent = Agent(
            provider=provider("main"),
            config=AgentConfig(max_iterations=3),
            session_key="e2e",
            tool_definitions=[
                ToolDefinition(name="echo", description="inspect", input_schema=ToolInputSchema())
            ],
            tool_handler=trace_tool_handler(tool, None),
        )
        assert [event async for event in agent.run_turn("delegate and answer")]
        assert [event async for event in agent.run_turn("continue using your previous answer")]

    try:
        await execute()
        await app.drain()
        enabled_requests = copy.deepcopy(requests)
        assert len(enabled_requests) == 5
        sent_ids = [row[FIELD]["call_ids"] for row in enabled_requests]
        keys = [call_key(row["source_id"], row["call_id"]) for row in sent_ids]
        received = store.call_identities("tenant")
        assert len(received) == 5  # Includes the timed-out request, carried by a later real call.
        store.db.execute("DELETE FROM records WHERE type!='call_ids'")
        store.db.execute("DELETE FROM observations")
        graph = IdTraceReconstructor().reconstruct(store.call_identities("tenant"))
        expected_edges = [
            [keys[0], keys[1], "spawned_by"],
            [keys[0], keys[2], "continues"],
            [keys[1], keys[2], "joins"],
            [keys[2], keys[3], "retry_of"],
            [keys[3], keys[4], "context_from"],
        ]
        assert all(edge in graph["edges"] for edge in expected_edges), graph
        assert graph["references_resolved"], graph
        assert graph["turn_edges"] == [
            [
                call_key(sent_ids[0]["source_id"], sent_ids[0]["user_turn_id"]),
                call_key(sent_ids[4]["source_id"], sent_ids[4]["user_turn_id"]),
                "next_user_turn",
            ]
        ]
        pending_tail = recording.journal.stats()["pending_records"]
        assert pending_tail > 0
        requests.clear()
        attempts.clear()
        token = _active.set(None)
        try:
            await execute()
        finally:
            _active.reset(token)
        pure_enabled = [
            {key: value for key, value in row.items() if key != FIELD} for row in enabled_requests
        ]
        assert requests == pure_enabled
        assert not connections
        if directory := os.environ.get("TRACEPOINT_ID_EVIDENCE_DIR"):
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            proof = {
                "enabled_call_count": len(enabled_requests),
                "disabled_call_count": len(requests),
                "business_requests_equal": True,
                "external_connections": len(connections),
                "platform_records_remaining": [
                    row[0] for row in store.db.execute("SELECT DISTINCT type FROM records")
                ],
                "pending_tail_records": pending_tail,
                "expected_edges": expected_edges,
                "fixture": (
                    "Actual Agent, SubagentManager and OpenAIProvider; "
                    "scripted model over in-process HTTP transport"
                ),
            }
            for name, value in (
                ("ids.json", received),
                ("trace.json", graph),
                ("proof.json", proof),
            ):
                (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    finally:
        await app.drain()
        store.close()
