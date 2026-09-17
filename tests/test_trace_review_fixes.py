"""Regressions from the 2026-09-17 independent review.

Expectations come from the protocol contracts (HANDOFF section 5/10,
CORRECTNESS_REVISION section 4/5) and from the test driver's own knowledge of
which Task sent which request. Nothing is derived from the graph under test.
"""

import asyncio
import json
import os
from contextlib import contextmanager

import httpx
import pytest

from opensquilla.observability.piggyback import identity_runtime as runtime
from opensquilla.observability.piggyback.capture import Capture, _active, context
from opensquilla.observability.piggyback.http import post_llm
from opensquilla.observability.piggyback.identity import kind
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.journal import atomic_write, native_path
from opensquilla.observability.piggyback.platform import (
    PlatformIngest,
    TraceMiddleware,
    token_authenticator,
)
from opensquilla.observability.piggyback.protocol import CALL, FIELD, digest
from opensquilla.observability.piggyback.transport import Destination
from opensquilla.provider.types import Message

JSON_HEADERS = [(b"content-type", b"application/json")]


@pytest.fixture
def cap(tmp_path):
    capture = Capture(tmp_path / "c", Destination("https://platform.test", "t", digest(b"k")))
    token = _active.set(capture)
    yield capture
    _active.reset(token)
    capture.journal.close()


@contextmanager
def run(cap, identity="R1", session="S1", turn="U1"):
    token = context.set({"run_id": identity, "session_id": session, "user_turn_id": turn})
    try:
        scope = runtime.start_run(context.get(), {})
        context.set({**context.get(), "_execution_ids": scope})
        runtime.iteration_start("I" + identity)
        context.set({**context.get(), "logical_call_id": "O" + identity, "outer_attempt_id": None})
        yield scope
    finally:
        context.reset(token)


def graph(cap):
    return ExecutionIdReconstructor().reconstruct(cap.identities.declarations())


def by_kind(g, typ):
    return [v for k, v in g["nodes"].items() if kind(k) == typ]


def test_atomic_write_works_on_windows_and_beyond_max_path(tmp_path):
    deep = tmp_path.joinpath(*(["d" * 60] * 4)) / ("f" * 64)
    atomic_write(deep, b"payload")
    assert os.path.exists(native_path(deep))
    with open(native_path(deep), "rb") as stored:
        assert stored.read() == b"payload"
    assert not [n for n in os.listdir(native_path(deep.parent)) if n.startswith(".")]


def test_content_store_round_trip_under_long_root(tmp_path):
    # Journal root stays under MAX_PATH; the content file and its temp name exceed it.
    root = tmp_path.joinpath(*(["r" * 50] * 2))
    capture = Capture(root, Destination("https://platform.test", "t", digest(b"k")))
    try:
        data = bytes(range(256)) * 100
        ref = capture.journal.content.put(data)
        assert capture.journal.content.get(ref) == data
        assert capture.journal.content.put(data) == ref
    finally:
        capture.journal.close()


async def test_parallel_attempt_start_does_not_attribute_call_to_other_attempt(cap):
    """Order: A.attempt_start, B.attempt_start, A.send, B.send on one inherited Scope.
    Contract: a Call never belongs to an Attempt another Task registered, and no
    serial predecessor is invented; the entry is isolated with a coverage gap."""
    with run(cap) as scope:
        mA, mB = Message(role="user", content="A input"), Message(role="user", content="B input")
        gate = {i: asyncio.Event() for i in "ab"}
        out = {}

        async def worker(name, message):
            runtime.attempt_start([message])
            out[name + "_attempt"] = scope.attempt
            gate[name].set()
            await gate["b" if name == "a" else "a"].wait()
            if name == "b":
                await asyncio.sleep(0)
            provider = runtime.provider_enter([message])
            binding = runtime.call_start("C" + name, {"messages": [message.business_dump()]})
            out[name] = binding[1]
            runtime.call_received(binding)
            runtime.call_end(binding, complete=True)
            runtime.provider_exit(provider)

        await asyncio.gather(worker("a", mA), worker("b", mB))
        r = cap.identities
        callA, callB = r.get(out["a"]), r.get(out["b"])
        assert out["a_attempt"] != out["b_attempt"]
        for call, message in ((callA, mA), (callB, mB)):
            wire = r.get(r.get(call["context_id"])["message_ids"][0])
            assert r.get(wire["source_ids"][0])["input_ids"] == [
                message.trace_ids.execution_message_id
            ]
        assert callA["attempt_id"] != out["b_attempt"]
        assert callB["attempt_id"] == out["b_attempt"]
        assert callA["previous_id"] is None and callB["previous_id"] is None
        runtime.attempt_end(success=True)
        runtime.end_run("R1", "completed")
    g = graph(cap)
    assert by_kind(g, "gap")
    assert not g["scopes"][scope.run]["complete"]


async def test_auxiliary_string_api_call_records_inputs_instead_of_gap(cap):
    """call_compaction_llm sends through post_llm(auxiliary=True) with wire messages."""
    ingest = PlatformIngest(cap.journal.root / "api.db")

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
        await send({"type": "http.response.body", "body": b'{"choices":[]}'})

    middleware = TraceMiddleware(app, ingest, token_authenticator({digest(b"k"): "t"}))
    try:
        with run(cap) as scope:
            runtime.attempt_start([Message(role="user", content="task")])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
                await post_llm(
                    client,
                    "https://platform.test/v1/chat/completions",
                    headers={"Authorization": "Bearer k"},
                    json={
                        "model": "m",
                        "messages": [
                            {"role": "system", "content": "summarize"},
                            {"role": "user", "content": "chunk text"},
                        ],
                    },
                    auxiliary=True,
                )
            runtime.attempt_end(success=True)
            runtime.end_run("R1", "completed")
        g = graph(cap)
        assert not cap.failed_runs
        assert not by_kind(g, "gap")
        assert g["scopes"][scope.run]["complete"], g["scopes"][scope.run]
        calls = by_kind(g, "call")
        assert len(calls) == 1
        wire = g["nodes"][g["nodes"][calls[0]["context_id"]]["message_ids"][0]]
        prepared = g["nodes"][wire["source_ids"][0]]["input_ids"]
        assert len(prepared) == 1  # the user chunk, with explicit unknown native origin
        assert kind(g["nodes"][g["nodes"][prepared[0]]["source_ids"][0]]["id"]) == "input"
    finally:
        await middleware.drain()
        ingest.close()


def test_call_success_without_received_is_not_complete(cap):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="q")])
        binding = runtime.call_start("C", {"messages": []})
        runtime.call_end(binding, complete=True)  # response_received never observed
        runtime.attempt_end(success=True)
        runtime.end_run("R1", "completed")
    g = graph(cap)
    assert not by_kind(g, "received")
    report = g["scopes"][scope.run]
    assert not report["complete"]
    assert "call_not_received" in report["reasons"]
    assert report["state"] == "incomplete"


@pytest.mark.parametrize("omitted", ["tool_success", "call_success", "run_success"])
def test_omitted_terminal_with_complete_inventory_is_incomplete_not_pending(cap, omitted):
    """Platform rule on its own: every declared member arrived, the terminal was never
    produced. That is producer omission, not a pending arrival."""
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="q")])
        message = cap.identities.put("message", scope.run, source_ids=[])
        scope.actions["t1"] = cap.identities.put("action", scope.run, message_id=message)
        binding = runtime.tool_start("t1", "X1")
        runtime.tool_end(binding, result="done")
        call = runtime.call_start("C", {"messages": []})
        runtime.call_received(call)
        runtime.call_end(call, complete=True)
        runtime.attempt_end(success=True)
        runtime.end_run("R1", "completed")
    full = graph(cap)
    assert full["scopes"][scope.run]["complete"], full["scopes"][scope.run]
    rows = list(full["nodes"].values())
    removed = {row["id"] for row in rows if kind(row["id"]) == omitted}
    assert removed
    rows = [
        {**row, "required_ids": [i for i in row["required_ids"] if i not in removed]}
        if kind(row["id"]) == "closure_part"
        else row
        for row in rows
        if row["id"] not in removed
    ]
    strict = ExecutionIdReconstructor().reconstruct(rows)
    report = strict["scopes"][scope.run]
    assert not strict["missing_ids"]
    assert not report["complete"]
    assert report["state"] == "incomplete", report


def test_local_seal_records_gap_for_call_without_terminal(cap):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="q")])
        runtime.call_start("C", {"messages": []})  # never ends
        runtime.attempt_end(success=True)
        runtime.end_run("R1", "completed")
    g = graph(cap)
    closure = by_kind(g, "closure")[0]
    assert closure["gap_ids"]
    assert g["scopes"][scope.run]["state"] == "incomplete"


def test_capture_failure_in_auxiliary_context_reaches_the_run_closure(cap):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="q")])
        token = context.set({**context.get(), "run_id": "auxiliary-never-sealed"})
        try:
            cap.fail()
        finally:
            context.reset(token)
        runtime.attempt_end(success=True)
        runtime.end_run("R1", "completed")
    g = graph(cap)
    assert by_kind(g, "closure")[0]["gap_ids"]
    assert not g["scopes"][scope.run]["complete"]


async def _post_with_correlation(cap, correlation, headers=None, env=None):
    from opensquilla.observability.piggyback.server import create_platform

    ingest = PlatformIngest(cap.journal.root / "api.db")
    seen = {}

    async def app(scope, receive, send):
        await receive()
        seen["headers"] = dict(scope["headers"])
        await asyncio.sleep(0.05)  # model latency lets durable ingestion finish before headers
        await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
        await send({"type": "http.response.body", "body": b'{"choices":[]}'})

    middleware = TraceMiddleware(app, ingest, token_authenticator({digest(b"k"): "t"}))
    url = "https://platform.test/v1/chat/completions"
    request_headers = {"Authorization": "Bearer k", **(headers or {})}
    try:
        with run(cap) as scope:
            runtime.attempt_start([Message(role="user", content="q")])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
                await post_llm(client, url, headers=request_headers, json={"model": "m"}, correlation=correlation)
                # A second business request carries the correlation record in its backlog.
                await post_llm(client, url, headers=request_headers, json={"model": "m"}, correlation=correlation)
            runtime.attempt_end(success=True)
            runtime.end_run("R1", "completed")
        await middleware.drain()
        rows = ingest.correlation_identities("t")
        observations = ingest.observations("t", [r["call_id"].split(":")[-1] for r in rows])
        ingest.close()
        app_platform = create_platform(
            {
                "token_hash_to_tenant": {digest(b"k"): "t"},
                "sqlite_path": str(cap.journal.root / "api.db"),
                "upstream_base_url": "https://unused.test",
            }
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_platform), base_url="http://query"
        ) as client:
            reply = await client.get("/execution-id-traces", headers={"Authorization": "Bearer k"})
        await app_platform.drain()
        assert reply.status_code == 200, reply.text
        return scope, rows, observations, reply.json(), seen
    finally:
        try:
            ingest.close()
        except Exception:
            pass


async def test_call_correlation_binds_upstream_values_to_the_call(cap):
    from opensquilla.provider.types import ProviderRequestCorrelation

    correlation = ProviderRequestCorrelation("native-session", "native-turn", "exec-1", "agent.chat")
    upstream_headers = {"X-OpenSquilla-Session-Id": "native-session", "X-OpenSquilla-Call-Kind": "agent.chat"}
    scope, rows, observations, graph, seen = await _post_with_correlation(
        cap, correlation, headers=upstream_headers
    )
    calls = {i for i in graph["positions"] if graph["positions"][i]["run_id"] == scope.run}
    assert len(calls) == 2
    by_call = {r["call_id"]: r for r in rows}
    # Records bind only to this run's Calls. When an ACK misses the response headers
    # (slow storage), the last record waits in the outbox for the next business request.
    assert by_call and set(by_call) <= calls
    for record in by_call.values():
        assert record["original"] == {
            "install_id": None,
            "session_id": "native-session",
            "turn_id": "native-turn",
            "execution_id": "exec-1",
            "call_kind": "agent.chat",
        }
        assert record["field_status"] == {
            "install_id": "unavailable",
            "session_id": "present",
            "turn_id": "present",
            "execution_id": "present",
            "call_kind": "present",
        }
    bound = next(iter(by_call))
    assert graph["original_correlation"][bound]["original"]["session_id"] == "native-session"
    # The reconstruction itself never depends on the record.
    assert graph["positions"][bound]["run_id"] == scope.run
    # Inbound upstream headers are kept beside the request for cross-checking.
    request_observations = [o for o in observations if o["kind"] == "server.request"]
    assert request_observations
    assert request_observations[0]["payload"]["original_headers"] == {
        "x-opensquilla-session-id": "native-session",
        "x-opensquilla-call-kind": "agent.chat",
    }
    # The upstream headers themselves are forwarded untouched.
    assert seen["headers"][b"x-opensquilla-session-id"] == b"native-session"


async def test_call_correlation_reports_unavailable_and_invalid_without_inventing(cap):
    from opensquilla.provider.types import ProviderRequestCorrelation

    invalid = ProviderRequestCorrelation("has space", "", "exec-2", "not.a.known.kind")
    scope, rows, _, graph, _ = await _post_with_correlation(cap, invalid)
    calls = {i for i in graph["positions"] if graph["positions"][i]["run_id"] == scope.run}
    assert rows and {r["call_id"] for r in rows} <= calls
    first = rows[0]
    assert first["original"] == {
        "install_id": None,
        "session_id": None,
        "turn_id": None,
        "execution_id": "exec-2",
        "call_kind": None,
    }
    assert first["field_status"] == {
        "install_id": "unavailable",
        "session_id": "invalid",
        "turn_id": "unavailable",
        "execution_id": "present",
        "call_kind": "invalid",
    }


async def test_upstream_privacy_switch_disables_extension_and_marks_disabled(cap, monkeypatch):
    from opensquilla.observability.piggyback.original_correlation import build
    from opensquilla.provider.types import ProviderRequestCorrelation

    monkeypatch.setenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", "1")
    correlation = ProviderRequestCorrelation("s", "t", "e", "agent.chat")
    record = build(cap.journal.source_id, cap.identities.id("call", "C"), correlation, disabled=True)
    assert set(record["value"]["field_status"].values()) == {"disabled"}
    assert set(record["value"]["original"].values()) == {None}
    seen = {}

    async def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    with run(cap):
        runtime.attempt_start([Message(role="user", content="q")])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await post_llm(
                client,
                "https://platform.test/v1/chat/completions",
                headers={"Authorization": "Bearer k"},
                json={"model": "m"},
                correlation=correlation,
            )
    assert FIELD not in seen["body"]
    assert CALL not in {k.lower() for k in seen["headers"]}


def test_call_correlation_record_validation_rejects_inconsistent_payloads(cap):
    from opensquilla.observability.piggyback.original_correlation import build
    from opensquilla.observability.piggyback.protocol import ProtocolError, validate_record
    from opensquilla.provider.types import ProviderRequestCorrelation

    call = cap.identities.id("call", "C")
    record = build(cap.journal.source_id, call, ProviderRequestCorrelation("s", "t", "e", "agent.chat"))
    validate_record(record)
    bad_value = json.loads(json.dumps(record))
    bad_value["value"]["original"]["turn_id"] = None  # present but null
    with pytest.raises(ProtocolError):
        validate_record(bad_value)
    bad_status = json.loads(json.dumps(record))
    bad_status["value"]["field_status"]["turn_id"] = "unavailable"  # unavailable but valued
    with pytest.raises(ProtocolError):
        validate_record(bad_status)
    bad_id = json.loads(json.dumps(record))
    bad_id["id"] = "call_correlation:" + cap.identities.id("call", "other")
    with pytest.raises(ProtocolError):
        validate_record(bad_id)


def test_trace_destination_limited_to_tokenrhythm_like_upstream_headers(monkeypatch):
    from opensquilla.observability.piggyback.transport import TOKENRHYTHM_PLATFORM_HOSTS
    from opensquilla.provider.tokenrhythm_correlation import _TOKENRHYTHM_CORRELATION_HOSTS

    assert TOKENRHYTHM_PLATFORM_HOSTS == frozenset(_TOKENRHYTHM_CORRELATION_HOSTS)
    key = digest(b"k")
    for host in TOKENRHYTHM_PLATFORM_HOSTS:
        assert Destination(f"https://{host}", "t", key).base_url == f"https://{host}"
        with pytest.raises(ValueError, match="requires_tls"):
            Destination(f"http://{host}", "t", key)
    monkeypatch.delenv("OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS", raising=False)
    for other in ("https://platform.test", "https://api.openai.com", "http://127.0.0.1:8769"):
        with pytest.raises(ValueError, match="not_tokenrhythm"):
            Destination(other, "t", key)
    monkeypatch.setenv("OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS", "127.0.0.1, Platform.Test")
    assert Destination("http://127.0.0.1:8769", "t", key)
    assert Destination("https://platform.test", "t", key)
    with pytest.raises(ValueError, match="not_tokenrhythm"):
        Destination("https://api.openai.com", "t", key)


def test_config_pointing_at_other_platform_disables_capture(tmp_path, monkeypatch):
    from opensquilla.observability.piggyback.capture import get_capture

    monkeypatch.delenv("OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS", raising=False)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "enabled": True,
                "root": str(tmp_path / "root"),
                "destination": {
                    "base_url": "https://other.example",
                    "tenant_id": "t",
                    "credential_sha256": digest(b"k"),
                },
            }
        )
    )
    monkeypatch.setenv("OPENSQUILLA_TRACE_CONFIG", str(cfg))
    assert get_capture() is None


async def test_identity_record_scope_keeps_only_declarations_and_still_rebuilds(tmp_path):
    """record_scope="identity": no events, bodies, blobs or legacy records are stored or
    uploaded; the platform still positions the Call and closes the scope."""
    from opensquilla.observability.piggyback.capture import trace_run
    from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor

    capture = Capture(
        tmp_path / "c",
        Destination("https://platform.test", "t", digest(b"k")),
        record_scope="identity",
    )
    token = _active.set(capture)
    ingest = PlatformIngest(tmp_path / "api.db")
    bodies = []

    async def app(scope, receive, send):
        bodies.append(json.loads((await receive())["body"]))
        await asyncio.sleep(0.05)
        await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
        await send({"type": "http.response.body", "body": b'{"choices":[]}'})

    middleware = TraceMiddleware(app, ingest, token_authenticator({digest(b"k"): "t"}))
    try:
        # A zero-call local run must not need an event manifest to close.
        @trace_run
        async def local(message, session_key):
            yield {"kind": "done"}

        assert [e async for e in local("local only", "S")]
        for index in range(2):
            with run(capture, identity=f"R{index}", turn=f"U{index}"):
                runtime.attempt_start([Message(role="user", content="q" * 5000)])
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as c:
                    await post_llm(
                        c,
                        "https://platform.test/v1/chat/completions",
                        headers={"Authorization": "Bearer k"},
                        json={"model": "m", "messages": [{"role": "user", "content": "q" * 5000}]},
                    )
                runtime.attempt_end(success=True)
                runtime.end_run(f"R{index}", "completed")
        await middleware.drain()
        assert not capture.failed_runs
        types = {r["type"] for r in capture.journal.records()}
        assert types <= {"id_node", "id_fact", "call_correlation"}, types
        assert "q" * 100 not in "".join(json.dumps(r) for r in capture.journal.records())
        local_graph = ExecutionIdReconstructor().reconstruct(capture.identities.declarations())
        assert all(s["complete"] for s in local_graph["scopes"].values()), local_graph["scopes"]
        received = ExecutionIdReconstructor().reconstruct(ingest.execution_identities("t"))
        assert len(received["positions"]) == 2
        assert all(v == "unique" for v in received["position_status"].values())
        # everything except the tail of the last request has reached the platform
        assert sum(s["complete"] for s in received["scopes"].values()) >= 2
        assert ingest.db.execute(
            "SELECT COUNT(*) FROM records WHERE type IN ('event','blob','call_ids','turn_ids','manifest')"
        ).fetchone()[0] == 0
        assert all(FIELD not in b for b in bodies)
    finally:
        await middleware.drain()
        ingest.close()
        _active.reset(token)
        capture.journal.close()


async def test_current_bundle_omits_nodes_the_platform_already_acknowledged(tmp_path):
    """The per-request current bundle only repeats unacknowledged declarations; the
    platform still positions every Call uniquely."""
    from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor

    capture = Capture(tmp_path / "c", Destination("https://platform.test", "t", digest(b"k")), record_scope="identity")
    token = _active.set(capture)
    ingest = PlatformIngest(tmp_path / "api.db")
    currents = []

    async def app(scope, receive, send):
        body = json.loads((await receive())["body"])
        await asyncio.sleep(0.05)
        await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
        await send({"type": "http.response.body", "body": b'{"choices":[]}'})

    middleware = TraceMiddleware(app, ingest, token_authenticator({digest(b"k"): "t"}))

    async def handler_wrapper(request):
        currents.append(json.loads(request.content)[FIELD]["execution_ids"])
        transport = httpx.ASGITransport(app=middleware)
        return await transport.handle_async_request(request)

    try:
        with run(capture):
            runtime.attempt_start([Message(role="user", content="q")])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler_wrapper)) as c:
                for _ in range(3):
                    await post_llm(c, "https://platform.test/v1/chat/completions",
                                   headers={"Authorization": "Bearer k"}, json={"model": "m"})
            runtime.attempt_end(success=True)
            runtime.end_run("R1", "completed")
        await middleware.drain()
        kinds = [sorted({kind(r["id"]) for r in current}) for current in currents]
        assert "session" in kinds[0] and "run" in kinds[0] and "call" in kinds[0]
        # after the first ACK the session/branch/run/phase/lane chain is not repeated
        assert "session" not in kinds[2] and "run" not in kinds[2]
        assert "call" in kinds[2]
        assert len(currents[2]) < len(currents[0])
        received = ExecutionIdReconstructor().reconstruct(ingest.execution_identities("t"))
        assert len(received["positions"]) == 3
        assert all(v == "unique" for v in received["position_status"].values())
    finally:
        await middleware.drain()
        ingest.close()
        _active.reset(token)
        capture.journal.close()


@pytest.mark.parametrize("scope_mode", ["full", "identity"])
async def test_zero_retention_keeps_only_unconfirmed_tail_and_anchors(tmp_path, scope_mode):
    """retain_acknowledged_seconds=0: acknowledged data is deleted right after the
    receipt; the unconfirmed tail and cross-run anchors stay; the platform still
    rebuilds and later Runs still link to earlier turns."""
    from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor

    capture = Capture(
        tmp_path / "c",
        Destination("https://platform.test", "t", digest(b"k")),
        record_scope=scope_mode,
        retain_acknowledged_seconds=0,
    )
    token = _active.set(capture)
    ingest = PlatformIngest(tmp_path / "api.db")

    async def app(scope, receive, send):
        await receive()
        await asyncio.sleep(0.05)
        await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
        await send({"type": "http.response.body", "body": b'{"choices":[]}'})

    middleware = TraceMiddleware(app, ingest, token_authenticator({digest(b"k"): "t"}))
    try:
        turns = []
        for index in range(3):
            with run(capture, identity=f"R{index}", turn=f"U{index}") as scope:
                turns.append(scope.turn)
                runtime.attempt_start([Message(role="user", content="q" * 3000)])
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as c:
                    await post_llm(
                        c,
                        "https://platform.test/v1/chat/completions",
                        headers={"Authorization": "Bearer k"},
                        json={"model": "m", "messages": [{"role": "user", "content": "q" * 3000}]},
                    )
                runtime.attempt_end(success=True)
                runtime.end_run(f"R{index}", "completed")
            if scope_mode == "full":
                capture.journal.seal(f"R{index}")
        await middleware.drain()
        assert not capture.failed_runs
        stats = capture.journal.stats()
        # Pruning runs at the end of each business request. Everything from the
        # earlier Runs (sealed before the last request) is gone except anchors; the
        # last Run's data waits for the next business request as usual.
        from opensquilla.observability.piggyback.transport import PiggybackTransport

        anchors = set(PiggybackTransport._ANCHOR_KINDS)
        earlier = {capture.identities.id("run", "R0"), capture.identities.id("run", "R1")}
        rows = capture.journal.db.execute(
            "SELECT id, type, run_id, acknowledged FROM records"
        ).fetchall()
        leftovers = [
            (row[0], row[1])
            for row in rows
            if row[2] in earlier | {"R0", "R1"}
            and not (row[1] == "id_node" and kind(row[0]) in anchors)
        ]
        assert leftovers == [], leftovers
        assert stats["pending_records"] > 0  # the last request's tail is still local
        assert all(row[3] == 0 or (row[1] == "id_node" and kind(row[0]) in anchors) or row[2] not in earlier for row in rows)
        # anchors remain resolvable: the third turn still chains to the second
        assert capture.identities.get(turns[1]) is not None
        received = ExecutionIdReconstructor().reconstruct(ingest.execution_identities("t"))
        assert len(received["positions"]) == 3
        assert all(v == "unique" for v in received["position_status"].values())
        assert sum(s["complete"] for s in received["scopes"].values()) >= 2
    finally:
        await middleware.drain()
        ingest.close()
        _active.reset(token)
        capture.journal.close()


def test_config_defaults_to_identity_scope_and_zero_retention(tmp_path, monkeypatch):
    from opensquilla.observability.piggyback.capture import get_capture

    monkeypatch.setenv("OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS", "platform.test")
    destination = {
        "base_url": "https://platform.test",
        "tenant_id": "t",
        "credential_sha256": digest(b"k"),
    }
    minimal = tmp_path / "minimal.json"
    minimal.write_text(json.dumps({"enabled": True, "root": str(tmp_path / "a"), "destination": destination}))
    monkeypatch.setenv("OPENSQUILLA_TRACE_CONFIG", str(minimal))
    capture = get_capture()
    assert capture.record_scope == "identity"
    assert capture.retain_acknowledged_seconds == 0
    capture.journal.close()
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "enabled": True, "root": str(tmp_path / "b"), "destination": destination,
        "record_scope": "full", "retain_acknowledged_seconds": None,
    }))
    monkeypatch.setenv("OPENSQUILLA_TRACE_CONFIG", str(explicit))
    capture = get_capture()
    assert capture.record_scope == "full"
    assert capture.retain_acknowledged_seconds is None
    capture.journal.close()


def test_dispatch_level_fallback_records_transition_across_runs(cap):
    """Gateway ProviderSelector fallback: the failed call lives in a dispatched child
    Run; the next dispatched Run's first Attempt must reference it."""
    with run(cap, identity="RT") as runtime_scope:
        parent_ctx = context.get()
        attempts = {}
        for index, outcome in enumerate(("failure", "success")):
            child = runtime.start_run(
                {"run_id": f"child{index}", "session_id": "S1", "user_turn_id": None},
                parent_ctx,
            )
            token = context.set(
                {
                    **parent_ctx,
                    "_execution_ids": child,
                    "logical_call_id": f"op{index}",
                    "outer_attempt_id": f"att{index}",
                }
            )
            try:
                runtime.attempt_start([Message(role="user", content="q")])
                attempts[index] = child.attempt
                binding = runtime.call_start(f"C{index}", {"messages": []})
                runtime.call_received(binding, 503 if outcome == "failure" else 200)
                runtime.call_end(
                    binding,
                    error=RuntimeError("503") if outcome == "failure" else None,
                    complete=outcome == "success",
                )
                runtime.attempt_end(success=outcome == "success", failure=outcome == "failure")
                runtime.end_run(f"child{index}", "error" if outcome == "failure" else "completed")
            finally:
                context.reset(token)
            if outcome == "failure":
                assert runtime_scope.last_child_attempt == attempts[0]
                runtime.mark_fallback()
        runtime.attempt_end(success=True)
        runtime.end_run("RT", "completed")
    g = graph(cap)
    transitions = by_kind(g, "fallback_transition")
    assert [(t["from_attempt_id"], t["to_attempt_id"]) for t in transitions] == [
        (attempts[0], attempts[1])
    ]
    assert not g["issues"]
