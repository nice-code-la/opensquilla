from __future__ import annotations

import asyncio
import base64
import copy
import gzip
import json
import random
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.observability.piggyback.capture import (
    Capture,
    _active,
    context,
    trace_run,
    trace_tool_handler,
)
from opensquilla.observability.piggyback.environment import (
    FileEnvironmentAdapter,
    RecordedContentStore,
    RecordedServiceAdapter,
)
from opensquilla.observability.piggyback.http import post_llm, stream_llm
from opensquilla.observability.piggyback.journal import TraceJournal
from opensquilla.observability.piggyback.platform import (
    PlatformIngest,
    TraceMiddleware,
    token_authenticator,
)
from opensquilla.observability.piggyback.protocol import (
    ACK,
    FIELD,
    MAX_DECODED,
    REJECT,
    ProtocolError,
    canonical,
    decode,
    digest,
    encode,
)
from opensquilla.observability.piggyback.reconstruct import TraceReconstructor
from opensquilla.observability.piggyback.replay import ReplayDivergence, ReplaySession
from opensquilla.observability.piggyback.transport import Destination, PiggybackTransport


@pytest.fixture
def destination():
    return Destination("https://platform.test", "tenant-a", digest(b"test-key"))


@pytest.fixture
def capture(tmp_path, destination):
    cap = Capture(tmp_path / "client", destination)
    token = _active.set(cap)
    identity = context.set({"run_id": "run-a", "turn_id": "turn-a", "session_id": "session-a"})
    yield cap
    context.reset(identity)
    _active.reset(token)
    cap.journal.close()


def complete_run(cap):
    cap.emit("run.start", {"input": "hello"})
    cap.emit("run.end", {"status": "completed"})
    cap.journal.seal("run-a")


def test_no_workers_or_uploads_on_append_prepare_close(capture, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    complete_run(capture)
    batch = capture.transport.prepare(capture.destination)
    assert len(decode(batch)) == 3
    capture.transport.release(batch["batch_id"])
    assert capture.journal.stats()["pending_records"] == 3


def test_restart_retains_identity_and_immutable_batch(tmp_path, destination):
    journal = TraceJournal(tmp_path, destination.key)
    journal.append({"kind": "run.start"})
    transport = PiggybackTransport(journal, lease_seconds=0)
    batch = transport.prepare(destination)
    source, epoch = journal.source_id, journal.epoch
    journal.close()
    reopened = TraceJournal(tmp_path, destination.key)
    assert reopened.source_id == source and reopened.epoch != epoch
    assert PiggybackTransport(reopened).prepare(destination) == batch
    with pytest.raises(ValueError, match="destination_mismatch"):
        TraceJournal(tmp_path, "other")
    reopened.close()


def test_ack_is_bound_to_batch_and_duplicates_are_idempotent(capture, tmp_path):
    complete_run(capture)
    batch = capture.transport.prepare(capture.destination)
    store = PlatformIngest(tmp_path / "server.db")
    for _ in range(2):
        assert store.accept("tenant-a", batch) == batch["batch_id"]
    assert len(store.records("tenant-a", batch["source_id"])) == 3
    assert store.records("tenant-b", batch["source_id"]) == []
    assert not capture.transport.confirm({ACK: "wrong"}, batch_id=batch["batch_id"])
    assert capture.journal.stats()["pending_records"] == 3
    assert capture.transport.confirm({ACK.upper(): batch["batch_id"]}, batch_id=batch["batch_id"])
    assert capture.journal.stats()["pending_records"] == 0
    store.close()


def test_conflicting_batch_is_atomic(capture, tmp_path):
    complete_run(capture)
    batch = capture.transport.prepare(capture.destination)
    store = PlatformIngest(tmp_path / "server.db")
    store.accept("tenant-a", batch)
    records = decode(batch)
    records[0]["value"]["payload"] = {"changed": True}
    changed = encode(batch["source_id"], batch["batch_id"], records)
    with pytest.raises(ProtocolError, match="conflict"):
        store.accept("tenant-a", changed)
    assert store.records("tenant-a", batch["source_id"])[0] in decode(batch)
    store.close()


def test_concurrent_leases_and_rejection_dont_block_new_data(capture):
    capture.emit("one")
    first = capture.transport.prepare(capture.destination)
    assert capture.transport.prepare(capture.destination) is None
    capture.emit("two")
    second = capture.transport.prepare(capture.destination)
    assert first["batch_id"] != second["batch_id"]
    capture.transport.confirm(
        {REJECT: first["batch_id"] + ":unsupported"}, batch_id=first["batch_id"]
    )
    assert capture.journal.stats()["rejected_batches"] == 1
    assert capture.journal.stats()["capture_gap"]


@pytest.mark.parametrize(
    "url,key,expected",
    [
        ("https://platform.test/v1/messages", "test-key", True),
        ("https://platform.test.evil/v1/messages", "test-key", False),
        ("https://platform.test:444/v1/messages", "test-key", False),
        ("http://platform.test/v1/messages", "test-key", False),
        ("https://platform.test/v1/messages", "other-key", False),
    ],
)
def test_destination_binding(destination, url, key, expected):
    assert destination.matches(url, {"Authorization": "Bearer " + key}) is expected


def test_partial_content_and_out_of_order_reconstruction(capture):
    capture.emit("run.start")
    ref = capture.journal.content.put(bytes(range(256)) * 1200)
    capture.emit("attachment", {"name": "input.bin"}, blob_refs=[ref])
    capture.emit("run.end")
    capture.journal.seal("run-a")
    records = capture.journal.records()
    expected = TraceReconstructor().reconstruct(records)
    assert expected["runs"]["run-a"]["complete"]
    for seed in range(6):
        shuffled = records * 2
        random.Random(seed).shuffle(shuffled)
        assert TraceReconstructor().reconstruct(shuffled) == expected
    partial = [r for r in records if not (r["type"] == "blob" and r["value"]["offset"] > 0)]
    report = TraceReconstructor().reconstruct(partial)["runs"]["run-a"]
    assert not report["complete"] and report["missing_blob_refs"] == [ref]


def test_tail_manifest_missing_never_complete(capture):
    capture.emit("run.start")
    before = capture.transport.prepare(capture.destination)
    capture.emit("run.end")
    capture.journal.seal("run-a")
    report = TraceReconstructor().reconstruct(decode(before))["runs"]["run-a"]
    assert report["complete"] is False
    assert "missing_manifest" in report["reasons"]


def test_invalid_missing_and_conflicting_evidence(capture):
    complete_run(capture)
    records = capture.journal.records()
    conflict = copy.deepcopy(records[0])
    conflict["value"]["payload"] = {"different": "evidence"}
    forward = TraceReconstructor().reconstruct(records + [conflict])
    backward = TraceReconstructor().reconstruct([conflict] + list(reversed(records)))
    assert forward == backward
    assert not forward["runs"]["run-a"]["complete"]


def test_size_limits_and_corruption(capture):
    capture.emit("data", {"body": "x" * 20000})
    batch = capture.transport.prepare(capture.destination)
    tampered = {**batch, "sha256": "0" * 64}
    with pytest.raises(ProtocolError, match="hash"):
        decode(tampered)
    bomb = {**batch, "data": base64.b64encode(gzip.compress(b"x" * (MAX_DECODED + 1))).decode()}
    with pytest.raises(ProtocolError, match="decoded_limit"):
        decode(bomb)
    assert capture.transport.prepare(capture.destination, budget=100) is None


def test_capacity_failure_does_not_discard_unacked(capture):
    capture.emit("run.start")
    count = len(capture.journal.records())
    capture.journal.quota = 1
    assert capture.emit("large", {"body": "z" * 100000}) is None
    assert len(capture.journal.records()) == count
    assert capture.journal.stats()["capture_gap"]


async def test_business_request_piggyback_ack_and_late_tail(capture, tmp_path):
    seen = []
    store = PlatformIngest(tmp_path / "server.db")

    async def business(scope, receive, send):
        message = await receive()
        seen.append(json.loads(message["body"]))
        await asyncio.sleep(0.025)  # normal inference time lets durable ingestion finish
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(ACK.encode(), b"cached-fake")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    middleware = TraceMiddleware(
        business, store, token_authenticator({digest(b"test-key"): "tenant-a"})
    )
    capture.emit("run.start", {"input": "task"})
    original = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
        response = await post_llm(
            client,
            "https://platform.test/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=original,
        )
        assert response.json() == {"ok": True}
        assert response.headers[ACK] != "cached-fake"
        assert seen == [original] and FIELD not in original
        capture.emit("run.end")
        capture.journal.seal("run-a")
        assert not TraceReconstructor().reconstruct(
            store.records("tenant-a", capture.journal.source_id)
        )["runs"]["run-a"]["complete"]
        # Genuine later request in another run transports the previous run's final records.
        token = context.set({"run_id": "run-b", "turn_id": "turn-b"})
        await post_llm(
            client,
            "https://platform.test/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=original,
        )
        context.reset(token)
    await middleware.drain()
    bundle = TraceReconstructor().reconstruct(store.records("tenant-a", capture.journal.source_id))
    assert bundle["runs"]["run-a"]["complete"]
    assert len(seen) == 2
    assert FIELD not in canonical(capture.journal.records()).decode()
    store.close()


@pytest.mark.parametrize("stream,status", [(False, 200), (False, 500), (True, 200), (True, 429)])
async def test_receipt_on_stream_and_error_status(capture, stream, status):
    capture.emit("run.start")
    count = 0

    def server(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        # Direct physical calls now carry current IDs as well as the existing
        # backlog. The batch ACK still acknowledges only the backlog.
        envelope = body[FIELD]
        backlog = envelope["backlog"] if envelope.get("version") in {2, 3} else envelope
        assert backlog is not None
        return httpx.Response(
            status, headers={ACK: backlog["batch_id"]}, content=b"data: hello\n\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        kw = {"headers": {"Authorization": "Bearer test-key"}, "json": {"model": "x"}}
        if stream:
            async with stream_llm(
                client, "POST", "https://platform.test/v1/messages", **kw
            ) as response:
                assert await response.aread() == b"data: hello\n\n"
        else:
            response = await post_llm(client, "https://platform.test/v1/messages", **kw)
        assert response.status_code == status
    assert count == 1
    assert (
        capture.journal.db.execute("SELECT count(*) FROM batches WHERE state='acked'").fetchone()[0]
        == 1
    )


async def test_storage_failure_preserves_business_and_omits_ack(capture):
    class FailedStore:
        def accept(self, *args):
            raise OSError("disk full")

    async def business(scope, receive, send):
        assert FIELD not in json.loads((await receive())["body"])
        await asyncio.sleep(0.01)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = TraceMiddleware(business, FailedStore(), lambda scope: "tenant-a")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
        response = await post_llm(
            client,
            "https://platform.test/v1/messages",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "x"},
        )
    await middleware.drain()
    assert response.text == "ok" and ACK not in response.headers
    assert capture.journal.stats()["pending_records"] > 0


async def test_timeout_has_no_telemetry_retry(capture):
    count = 0

    def timeout(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await post_llm(
                client,
                "https://platform.test/v1/messages",
                headers={"Authorization": "Bearer test-key"},
                json={"model": "x"},
            )
    assert count == 1
    assert capture.transport.prepare(capture.destination) is not None


async def test_full_tool_boundary_including_early_denial(capture):
    async def denied(call):
        return {"status": "rejected", "reason": "unknown_tool"}

    handler = trace_tool_handler(denied, None)
    assert (await handler(SimpleNamespace(id="tool1", name="missing")))["status"] == "rejected"
    kinds = [r["value"]["kind"] for r in capture.journal.records()]
    assert kinds == ["tool.requested", "tool.returned"]


async def test_run_finalization_and_replay_require_no_network(capture):
    token = context.set({})

    @trace_run
    async def run(message, *, root_turn_id=None):
        assert root_turn_id
        yield {"text": message}

    assert [e async for e in run("hello")] == [{"text": "hello"}]
    context.reset(token)
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    run_id = next(iter(bundle["runs"]))
    assert bundle["runs"][run_id]["complete"]
    replay = ReplaySession(bundle, run_id)
    replay.finish()
    with pytest.raises(ReplayDivergence):
        replay.llm({"model": "unrecorded"})


def test_files_sqlite_restore_reset_and_reference_validation(capture, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "untracked.txt").write_text("initial")
    (source / "empty").mkdir()
    (source / "alias").symlink_to("untracked.txt")
    with sqlite3.connect(source / "data.sqlite") as db:
        db.execute("CREATE TABLE items(value TEXT)")
        db.execute("INSERT INTO items VALUES('original')")
    adapter = FileEnvironmentAdapter(capture.journal.content)
    snapshot = adapter.capture(source)
    assert snapshot["grade"] == "executable"
    target = tmp_path / "restored"
    adapter.restore(snapshot, target)
    assert adapter.verify(snapshot, target)["ok"]
    (target / "untracked.txt").write_text("modified")
    assert not adapter.verify(snapshot, target)["ok"]
    adapter.reset(snapshot, target)
    assert adapter.verify(snapshot, target)["ok"]
    with sqlite3.connect(target / "data.sqlite") as db:
        assert db.execute("SELECT value FROM items").fetchone()[0] == "original"
    with pytest.raises(ValueError, match="isolated"):
        adapter.restore(snapshot, source)


@pytest.mark.parametrize(
    "bad_path", ["../escape", "/absolute", "a/../../escape", "a\\escape", ".tracepoint-owned", "."]
)
def test_restore_rejects_path_traversal(capture, tmp_path, bad_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok").write_text("ok")
    adapter = FileEnvironmentAdapter(capture.journal.content)
    snapshot = adapter.capture(source)
    snapshot["entries"][0]["path"] = bad_path
    snapshot["snapshot_id"] = digest(
        canonical({k: v for k, v in snapshot.items() if k != "snapshot_id"})
    )
    with pytest.raises(ValueError):
        adapter.restore(snapshot, tmp_path / "target")


def test_recorded_external_state_refuses_new_requests():
    adapter = RecordedServiceAdapter([{"request": {"url": "a"}, "response": {"text": "old"}}])
    assert adapter.call({"url": "a"}) == {"text": "old"}
    with pytest.raises(ValueError):
        adapter.call({"url": "b"})


@pytest.mark.parametrize("backend", ["openai_compat", "anthropic", "openai_responses", "ollama"])
async def test_actual_provider_enabled_preserves_business_projection(
    backend, tmp_path, monkeypatch
):
    import dataclasses

    from tests.test_provider.golden import _harness as harness

    case = next(c for c in harness.build_cases() if c.backend == backend)
    case = dataclasses.replace(case, base_url="https://platform.test", api_key=harness.FAKE_API_KEY)
    cap = Capture(
        tmp_path,
        Destination("https://platform.test", "tenant-a", digest(harness.FAKE_API_KEY.encode())),
    )
    token = _active.set(cap)
    try:
        cap.emit("run.start")
        record = await harness.capture_case_record(case, monkeypatch)
        assert FIELD in record["body"]
        telemetry = record["body"].pop(FIELD)
        assert decode(telemetry)
        assert harness.project_case_request(case).payload == record["body"]
        events = [r["value"] for r in cap.journal.records() if r["type"] == "event"]
        assert sum(e["kind"] == "llm.request" for e in events) == 1
        assert any(e["kind"] == "llm.chunk" for e in events)
    finally:
        _active.reset(token)
        cap.journal.close()


async def test_parent_cannot_complete_before_scheduled_child(capture):
    from opensquilla.observability.piggyback.capture import child_task_context

    capture.emit("run.start")
    child_context = child_task_context("child-a", "child task")
    capture.emit("run.end")
    capture.journal.seal("run-a")
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    assert bundle["runs"]["run-a"]["reasons"] == ["incomplete_child"]

    @trace_run
    async def run_turn(message):
        yield {"kind": "done"}

    async def run_child():
        return [e async for e in run_turn("child task")]

    await asyncio.create_task(run_child(), context=child_context)
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    assert bundle["runs"]["run-a"]["complete"]
    assert bundle["runs"]["child-a"]["complete"]


async def test_active_real_dispatch_captures_unknown_tool_and_success(capture, tmp_path):
    from opensquilla.tool_boundary import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry

    registry = ToolRegistry()
    handler = build_tool_handler(registry)
    result = await handler(ToolCall(tool_use_id="call-unknown", tool_name="unknown", arguments={}))
    assert result is not None
    events = [r["value"] for r in capture.journal.records()]
    assert [e["kind"] for e in events] == ["tool.requested", "tool.returned"]
    assert events[0]["payload"]["call"]["tool_name"] == "unknown"


def test_augmentation_admission_and_independent_controls(capture, tmp_path):
    import dataclasses

    from opensquilla.observability.piggyback.augmentation import admit_task, generate_file_tasks

    source = tmp_path / "seed"
    source.mkdir()
    (source / "input.txt").write_text("seed")
    adapter = FileEnvironmentAdapter(capture.journal.content)
    snapshot = adapter.capture(source)
    capture.emit("run.start")
    capture.emit("environment.checkpoint", snapshot, blob_refs=snapshot["blob_refs"])
    capture.emit("run.end")
    capture.journal.seal("run-a")
    bundle = TraceReconstructor().reconstruct(capture.journal.records(), provenance="platform")
    tasks = generate_file_tasks(bundle, "run-a", snapshot, count=2, source_family="family")
    assert tasks[0].split == tasks[1].split
    admitted = admit_task(tasks[0], bundle, snapshot, adapter, tmp_path / "target")
    assert admitted["admitted"]
    assert admitted["execution_success"] is None
    with pytest.raises(ValueError, match="negative_control"):
        bad = dataclasses.replace(tasks[0], negative_actions=tasks[0].reference_actions)
        admit_task(bad, bundle, snapshot, adapter, tmp_path / "target")


def test_export_validated_against_installed_standards(capture):
    from opensquilla.observability.piggyback.export import html_report, to_atif, to_otlp

    complete_run(capture)
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    otlp = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    from google.protobuf.json_format import ParseDict

    parsed = ParseDict(to_otlp(bundle), otlp.ExportTraceServiceRequest())
    assert parsed.resource_spans[0].scope_spans[0].spans
    harbor = pytest.importorskip("harbor.utils.trajectory_validator")
    validator = harbor.TrajectoryValidator()
    assert validator.validate(to_atif(bundle, "run-a")), validator.get_errors()
    bundle["events"][0]["payload"] = {"text": "</script><script>alert(1)</script>"}
    assert "</script><script>alert(1)" not in html_report(bundle)


def test_legacy_import_never_invents_complete_or_shared_identity():
    from opensquilla.observability.piggyback.export import import_legacy

    records = import_legacy([{"turn_id": "known", "prompt_hash": "abc"}, {"text": "orphan"}])
    bundle = TraceReconstructor().reconstruct(records)
    assert not bundle["runs"]["known"]["complete"]
    assert "run_id" not in records[1]["value"]["context"]


def test_manifest_cannot_borrow_another_runs_terminal(capture):
    complete_run(capture)
    records = capture.journal.records()
    for record in records:
        if record["type"] == "event" and record["value"]["kind"] == "run.end":
            record["value"]["context"]["run_id"] = "other"
    bundle = TraceReconstructor().reconstruct(records)
    assert not bundle["runs"]["run-a"]["complete"]


def test_postgres_s3_contract_when_services_available(capture):
    import os

    if not os.environ.get("TRACE_TEST_PG_DSN"):
        pytest.skip("PostgreSQL/S3 services not configured")
    import boto3

    from opensquilla.observability.piggyback.postgres import PostgresS3Ingest

    store = PostgresS3Ingest(
        os.environ["TRACE_TEST_PG_DSN"],
        boto3.client("s3", endpoint_url=os.environ.get("TRACE_TEST_S3_ENDPOINT")),
        os.environ["TRACE_TEST_S3_BUCKET"],
    )
    store.initialize()
    complete_run(capture)
    batch = capture.transport.prepare(capture.destination)
    store.accept("test-tenant", batch)
    assert store.accept("test-tenant", batch) == batch["batch_id"]
    assert TraceReconstructor().reconstruct(store.records("test-tenant", batch["source_id"]))[
        "runs"
    ]["run-a"]["complete"]


async def test_codex_actual_send_is_piggybacked(tmp_path, monkeypatch):
    from opensquilla.provider.openai_codex import OpenAICodexProvider
    from opensquilla.provider.types import ChatConfig, ErrorEvent, Message
    from tests.test_provider_openai_codex import _happy_sse, _write_auth

    auth = _write_auth(tmp_path / "auth.json")
    cap = Capture(
        tmp_path / "capture", Destination("https://platform.test", "tenant", digest(b"tok-access"))
    )
    token = _active.set(cap)
    seen = []
    real_client = httpx.AsyncClient

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, content=_happy_sse(), headers={"content-type": "text/event-stream"}
        )

    def factory(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handle)})

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    try:
        provider = OpenAICodexProvider(base_url="https://platform.test", auth_path=str(auth))
        events = [
            e
            async for e in provider.chat(
                [Message(role="user", content="hello")], config=ChatConfig()
            )
        ]
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        assert len(seen) == 1 and FIELD in seen[0]
    finally:
        _active.reset(token)
        cap.journal.close()


async def test_fast_cache_hit_eventually_confirms_prior_durable_batch(capture, tmp_path):
    store = PlatformIngest(tmp_path / "server.db")

    async def cached(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = TraceMiddleware(cached, store, lambda scope: "tenant-a")
    capture.emit("run.start")
    batch = capture.transport.prepare(capture.destination)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as client:
        first = await client.post(
            "https://platform.test/v1/messages", json={"model": "test", FIELD: batch}
        )
        await middleware.drain()
        second = await client.post(
            "https://platform.test/v1/messages", json={"model": "test", FIELD: batch}
        )
    assert second.headers.get(ACK) == batch["batch_id"]
    assert first.status_code == second.status_code == 200
    await middleware.drain()
    store.close()


async def test_environment_capture_failure_does_not_break_run(capture, tmp_path):
    capture.environment_roots = [str(tmp_path / "missing-directory")]
    token = context.set({})

    @trace_run
    async def run(message, *, root_turn_id=None):
        yield {"kind": "done"}

    assert [item async for item in run("task")] == [{"kind": "done"}]
    context.reset(token)
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    report = next(iter(bundle["runs"].values()))
    assert not report["complete"] and "capture_gap" in report["reasons"]


def test_local_completeness_is_not_platform_receipt(capture):
    complete_run(capture)
    bundle = TraceReconstructor().reconstruct(capture.journal.records(), provenance="local")
    assert bundle["runs"]["run-a"]["local_complete"] is True
    assert bundle["runs"]["run-a"]["platform_complete"] is None


async def test_redirects_keep_business_behavior_without_leaking_batch(capture):
    seen = []

    def handle(request):
        seen.append((str(request.url), json.loads(request.content)))
        if request.url.host == "platform.test":
            return httpx.Response(307, headers={"location": "https://elsewhere.test/v1/messages"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        response = await post_llm(
            client,
            "https://platform.test/v1/messages",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "x"},
        )
    assert response.status_code == 200 and len(seen) == 2
    assert all(FIELD not in payload for _, payload in seen)


async def test_config_disable_mid_run_leaves_incomplete_scope(capture, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "destination": {
                    "base_url": capture.destination.base_url,
                    "tenant_id": capture.destination.tenant_id,
                    "credential_sha256": capture.destination.credential_sha256,
                },
            }
        )
    )
    capture.config_path = config
    token = context.set({})

    @trace_run
    async def run(message, *, root_turn_id=None):
        config.write_text('{"enabled":false}')
        yield {"kind": "done", "text": "do not collect this"}

    assert [e async for e in run("task")]
    context.reset(token)
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    assert all(not r["complete"] for r in bundle["runs"].values())
    assert "do not collect this" not in canonical(bundle).decode()


async def test_async_opensquilla_worker_receives_no_verifier(capture, tmp_path):
    from opensquilla.observability.piggyback.augmentation import (
        OpenSquillaTaskRunner,
        execute_task,
        generate_file_tasks,
    )

    source = tmp_path / "seed-async"
    source.mkdir()
    (source / "input.txt").write_text("seed")
    adapter = FileEnvironmentAdapter(capture.journal.content)
    snapshot = adapter.capture(source)
    capture.emit("run.start")
    capture.emit("environment.checkpoint", snapshot, blob_refs=snapshot["blob_refs"])
    capture.emit("run.end")
    capture.journal.seal("run-a")
    bundle = TraceReconstructor().reconstruct(capture.journal.records(), provenance="platform")
    task = generate_file_tasks(bundle, "run-a", snapshot, count=1)[0]
    received = []

    def factory(**kwargs):
        received.append(kwargs)

        class Agent:
            @trace_run
            async def run_turn(self, message):
                # Independently solve the generated instruction, without seeing reference actions.
                value = json.loads(message.split("JSON object: ")[1])
                output = kwargs["root"] / message.split(" ")[1]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(value))
                yield {"kind": "done", "text": "completed"}

        return Agent()

    result = await execute_task(
        task, bundle, snapshot, adapter, tmp_path / "async-target", OpenSquillaTaskRunner(factory)
    )
    assert result["execution_success"]
    assert set(received[0]) == {"root", "allowed_tools", "lineage"}
    restored = TraceReconstructor().reconstruct(capture.journal.records())
    assert restored["runs"][result["lineage"]["run_id"]]["complete"]


@pytest.mark.parametrize("consume_all", [False, True])
async def test_stream_close_preserves_receive_boundary(capture, consume_all):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"delta":"one"}\n\n'
            yield b"data: [DONE]\n\n"

    capture.emit("run.start")
    request = {"model": "x", "stream": True}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks()))
    ) as client:
        async with stream_llm(
            client, "POST", "https://thirdparty.test/v1/messages", json=request
        ) as response:
            async for _ in response.aiter_bytes():
                if not consume_all:
                    break
    capture.emit("run.end")
    capture.journal.seal("run-a")
    bundle = TraceReconstructor().reconstruct(capture.journal.records())
    assert bundle["runs"]["run-a"]["complete"]  # A fully recorded interruption is evidence too.
    exchange = ReplaySession(bundle, "run-a").exchange(request)
    assert exchange["outcome"]["body_complete"] is consume_all
    assert exchange["outcome"]["status"] == ("completed" if consume_all else "interrupted")
    assert (b"[DONE]" in exchange["body"]) is consume_all
    if not consume_all:
        with pytest.raises(ReplayDivergence, match="recorded_provider_failure"):
            ReplaySession(bundle, "run-a").llm(request)


def test_compressed_response_view_is_bounded(monkeypatch):
    import opensquilla.observability.piggyback.reconstruct as reconstruction

    monkeypatch.setattr(reconstruction, "MAX_RESPONSE_PARSE", 4096)
    response = gzip.compress(b"x" * 100_000)
    assert reconstruction.parse_response(response, "gzip")["parse_status"] == "size_limit"
    assert reconstruction.parse_response(b"broken", "br")["parse_status"] == "unavailable"


def test_platform_chunks_restore_without_original_host(capture, tmp_path, monkeypatch):
    root = tmp_path / "original-files"
    (root / "private").mkdir(parents=True, mode=0o700)
    (root / "private" / "file.txt").write_text("platform-only restoration")
    snapshot = FileEnvironmentAdapter(capture.journal.content).capture(root)
    received = capture.journal.records()

    def forbidden(*args):
        raise AssertionError("original host content must not be read")

    monkeypatch.setattr(capture.journal.content, "get", forbidden)
    adapter = FileEnvironmentAdapter(RecordedContentStore(received + received))
    target = tmp_path / "recovered-from-platform"
    adapter.restore(snapshot, target)
    assert adapter.verify(snapshot, target)["ok"]
    (target / "private" / "file.txt").write_text("changed")
    adapter.reset(snapshot, target)
    assert adapter.verify(snapshot, target)["ok"]
    with pytest.raises(FileNotFoundError, match="content_not_received"):
        RecordedContentStore([]).get(snapshot["blob_refs"][0])


async def test_platform_content_queries_are_tenant_scoped(capture, tmp_path):
    from opensquilla.observability.piggyback.server import create_platform

    ref = capture.journal.content.put(b"received file")
    batch = capture.transport.prepare(capture.destination)
    path = tmp_path / "query.db"
    store = PlatformIngest(path)
    store.accept("tenant-a", batch)
    store.close()
    app = create_platform(
        {
            "sqlite_path": str(path),
            "token_hash_to_tenant": {
                digest(b"test-key"): "tenant-a",
                digest(b"other-key"): "tenant-b",
            },
            "upstream_base_url": "https://unused.test",
        }
    )
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://platform.test"
        ) as client:
            url = f"/traces/{batch['source_id']}/content/{ref}"
            assert (await client.get(url)).status_code == 401
            assert (
                await client.get(url, headers={"Authorization": "Bearer other-key"})
            ).status_code == 404
            response = await client.get(url, headers={"Authorization": "Bearer test-key"})
            assert response.content == b"received file"
