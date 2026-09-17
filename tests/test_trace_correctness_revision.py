"""Independent expected edges and missing-fact/queue regressions from the review."""

import asyncio
import hashlib
from contextlib import contextmanager

import pytest

from opensquilla.observability.piggyback import identity_runtime as runtime
from opensquilla.observability.piggyback.capture import Capture, _active, context
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.platform import PlatformIngest
from opensquilla.observability.piggyback.protocol import ACK, canonical, digest
from opensquilla.observability.piggyback.transport import Destination
from opensquilla.provider.types import Message, MessageTraceIds


@pytest.fixture
def cap(tmp_path):
    capture = Capture(tmp_path / "client", Destination("https://platform.test", "t", digest(b"k")))
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
        context.set(
            {**context.get(), "logical_call_id": "O" + identity, "outer_attempt_id": "A" + identity}
        )
        yield scope
    finally:
        context.reset(token)


def finish(cap, identity="R1", terminal=True):
    if terminal:
        runtime.attempt_end(success=True)
    runtime.end_run(identity, "completed")
    assert not cap.failed_runs
    return ExecutionIdReconstructor().reconstruct(cap.identities.declarations())


@pytest.mark.parametrize("turns", [("U2",), ("U2", "U1", "U2")])
def test_explicit_origins_override_execution_anchor(cap, turns):
    with run(cap) as scope:
        r = cap.identities
        branch = r.get(scope.run)["branch_id"]
        r.accept_local_turn(branch, "U2")
        message = Message(
            role="user",
            content="input",
            trace_ids=MessageTraceIds(message_id="M2", input_turn_ids=turns),
        )
        runtime.attempt_start([message])
        expected = [r.get(r.id("turn", turn))["input_id"] for turn in turns]
        assert r.get(message.trace_ids.execution_message_id)["source_ids"] == expected


def test_unknown_origin_is_gap_not_invented_turn(cap):
    with run(cap) as scope:
        message = Message(
            role="user",
            content="input",
            trace_ids=MessageTraceIds(message_id="M", input_turn_ids=("unknown",)),
        )
        runtime.attempt_start([message])
        graph = finish(cap)
        assert not cap.identities.get(cap.identities.id("turn", "unknown"))
        assert not graph["scopes"][scope.run]["complete"]


@pytest.mark.parametrize("reverse", [False, True])
async def test_parallel_provider_inputs_never_alias_or_invent_previous(cap, reverse):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="seed")])
        messages = [runtime.context_message(Message(role="user", content=c)) for c in "AB"]
        ready = [asyncio.Event(), asyncio.Event()]
        bindings = {}

        async def worker(i):
            provider = runtime.provider_enter([messages[i]])
            ready[i].set()
            await ready[1 - i].wait()
            if bool(i) == reverse:
                await asyncio.sleep(0)
            bindings[i] = runtime.call_start(str(i), {"messages": [messages[i].business_dump()]})
            runtime.call_received(bindings[i])
            runtime.call_end(bindings[i], complete=True)
            runtime.provider_exit(provider)

        await asyncio.gather(worker(0), worker(1))
        r = cap.identities
        calls = [r.get(bindings[i][1]) for i in (0, 1)]
        for i, call in enumerate(calls):
            wire = r.get(r.get(call["context_id"])["message_ids"][0])
            source = r.get(wire["source_ids"][0])["input_ids"]
            assert source == [messages[i].trace_ids.execution_message_id]
            assert call["previous_id"] is None
        graph = finish(cap)
        # This generic inherited parallel entry has no authoritative fan-out adapter.
        assert not graph["scopes"][scope.run]["complete"]


def test_serial_provider_snapshots_preserve_predecessor(cap):
    with run(cap):
        message = Message(role="user", content="input")
        runtime.attempt_start([message])
        calls = []
        for i in (0, 1):
            provider = runtime.provider_enter([message])
            binding = runtime.call_start(str(i), {"messages": [message.business_dump()]})
            calls.append(binding[1])
            runtime.call_received(binding)
            runtime.call_end(binding, complete=True)
            runtime.provider_exit(provider)
        graph = finish(cap)
        assert graph["nodes"][calls[1]]["previous_id"] == calls[0]
        assert all(s["complete"] for s in graph["scopes"].values())


def test_never_emitted_attempt_terminal_is_not_complete(cap):
    with run(cap) as scope:
        message = Message(role="user", content="input")
        runtime.attempt_start([message])
        binding = runtime.call_start("C", {"messages": [message.business_dump()]})
        runtime.call_received(binding)
        runtime.call_end(binding, complete=True)
        graph = finish(cap, terminal=False)
        assert not graph["scopes"][scope.run]["complete"]
        assert "attempt_not_terminal" in graph["scopes"][scope.run]["reasons"]


def test_reduced_budget_delivers_existing_large_record(cap, tmp_path):
    data = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(2048))
    content = cap.journal.content.put(data)
    old = cap.transport.prepare(cap.destination)
    assert len(canonical(old)) > 8192
    cap.transport.release(old["batch_id"])
    store = PlatformIngest(tmp_path / "api.db")
    try:
        for _ in range(32):
            batch = cap.transport.prepare(cap.destination, 8192)
            if batch is None:
                break
            assert len(canonical(batch)) <= 8192
            receipt = store.accept("t", batch)
            assert cap.transport.confirm({ACK: receipt}, batch_id=receipt)
        assert (
            cap.journal.db.execute(
                "SELECT acknowledged FROM records WHERE id=?", (content + ":0",)
            ).fetchone()[0]
            == 1
        )
        assert any(row["id"] == content + ":0" for row in store.records("t", cap.journal.source_id))
        assert bytes(
            cap.journal.db.execute(
                "SELECT envelope FROM batches WHERE id=?", (old["batch_id"],)
            ).fetchone()[0]
        ) == canonical(old)
    finally:
        store.close()


def test_same_legacy_key_in_distinct_sessions_resolves_by_branch(cap):
    r = cap.identities
    with run(cap) as first:
        b1 = r.get(first.run)["branch_id"]
        t1 = r.accept_local_turn(b1, "same")
    with run(cap, "R2", "S2", "U2") as second:
        b2 = r.get(second.run)["branch_id"]
        t2 = r.accept_local_turn(b2, "same")
        assert t1 != t2
        assert r.resolve_turn(b1, "same")["id"] == t1
        assert r.resolve_turn(b2, "same")["id"] == t2
        message = Message(
            role="user",
            content="same text",
            trace_ids=MessageTraceIds(message_id="M", input_turn_ids=("same",)),
        )
        runtime.attempt_start([message])
        assert r.get(message.trace_ids.execution_message_id)["source_ids"] == [
            r.get(t2)["input_id"]
        ]


def test_cross_session_child_only_resolves_explicit_activation_inputs(cap):
    r = cap.identities
    with run(cap) as parent:
        origin = parent.turn
        parent_context = context.get()
        child = runtime.start_run(
            {
                "run_id": "child",
                "session_id": "child-session",
                "_execution_session_id": "child-session",
            },
            parent_context,
        )
        token = context.set(
            {
                **parent_context,
                "_execution_ids": child,
                "logical_call_id": "child-op",
                "outer_attempt_id": "child-attempt",
            }
        )
        try:
            message = Message(
                role="user",
                content="forwarded",
                trace_ids=MessageTraceIds(message_id="child-message", input_turn_ids=("U1",)),
            )
            runtime.attempt_start([message])
            assert r.get(message.trace_ids.execution_message_id)["source_ids"] == [
                r.get(origin)["input_id"]
            ]
        finally:
            context.reset(token)


@pytest.mark.parametrize("status", [200, 500])
def test_owned_provider_attempt_uses_observed_http_outcome(cap, status):
    with run(cap):
        provider = runtime.provider_enter([], auxiliary=True)
        scope = runtime.current()[2]
        call = runtime.call_start("C", {})
        runtime.call_received(call, status)
        runtime.call_end(call, complete=True)
        runtime.provider_exit(provider)
        terminals = [
            r
            for r in cap.identities.declarations()
            if r.get("attempt_id") == scope.attempt and ":attempt_" in r["id"]
        ]
        assert len(terminals) == 1
        expected = ":attempt_success:" if status == 200 else ":attempt_failure:"
        assert expected in terminals[0]["id"]


def test_conflicting_native_alias_is_not_last_writer_wins(cap):
    r = cap.identities
    with run(cap) as scope:
        branch = r.get(scope.run)["branch_id"]
        t2 = r.accept_local_turn(branch, "U2")
        with cap.journal.transaction():
            r.bind_turn_alias(branch, "legacy", scope.turn)
            r.bind_turn_alias(branch, "legacy", t2)
        assert r.resolve_turn(branch, "legacy") is None
        before = r.declarations()
        with pytest.raises(ValueError, match="turn_alias_conflict"):
            r.accept_local_turn(branch, "legacy")
        assert r.resolve_turn(branch, "legacy") is None
        assert r.declarations() == before


def test_call_binding_remains_frozen_after_scope_changes(cap):
    from dataclasses import FrozenInstanceError

    with run(cap) as scope:
        message = Message(role="user", content="input")
        runtime.attempt_start([message])
        call = runtime.call_start("C", {"messages": [message.business_dump()]})
        snapshot = call[0]
        expected = snapshot.attempt, snapshot.message_ids
        scope.message_ids = ["different"]
        scope.attempt = "different"
        assert (snapshot.attempt, snapshot.message_ids) == expected
        with pytest.raises(FrozenInstanceError):
            snapshot.attempt = "rewrite"
        runtime.call_end(call, complete=True)
        assert cap.identities.get(call[1])["attempt_id"] == expected[0]


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupted"])
def test_zero_call_attempt_can_close_with_observed_outcome(cap, outcome):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="preflight")])
        runtime.attempt_end(success=outcome == "success", failure=outcome == "failure")
        graph = finish(cap, terminal=False)
        assert not graph["positions"]
        assert graph["scopes"][scope.run]["complete"]


def test_platform_catches_terminal_absent_even_from_self_reported_inventory(cap):
    from opensquilla.observability.piggyback.identity import kind

    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="input")])
        graph = finish(cap)
        rows = list(graph["nodes"].values())
        removed = {row["id"] for row in rows if kind(row["id"]) == "attempt_success"}
        rows = [
            {**row, "required_ids": [i for i in row["required_ids"] if i not in removed]}
            if kind(row["id"]) == "closure_part"
            else row
            for row in rows
            if row["id"] not in removed
        ]
        strict = ExecutionIdReconstructor().reconstruct(rows)
        assert strict["references_closed"]
        assert "attempt_not_terminal" in strict["scopes"][scope.run]["reasons"]
        assert not strict["scopes"][scope.run]["complete"]


@pytest.mark.parametrize("duplicate", [True, False])
def test_terminal_duplicate_is_idempotent_but_contradiction_is_not(cap, duplicate):
    with run(cap) as scope:
        runtime.attempt_start([Message(role="user", content="input")])
        runtime.attempt_end(success=True)
        if not duplicate:
            runtime.attempt_end(failure=True)
        graph = finish(cap, terminal=False)
        rows = list(graph["nodes"].values())
        rebuilt = ExecutionIdReconstructor().reconstruct(rows + rows if duplicate else rows)
        assert rebuilt == graph
        assert graph["scopes"][scope.run]["complete"] == duplicate


@pytest.mark.parametrize("old_first", [True, False])
def test_late_ack_confirms_exact_members_only(cap, tmp_path, old_first):
    content = cap.journal.content.put(bytes(range(256)) * 256)
    # Use high entropy to force a fragmented retry.
    content = cap.journal.content.put(
        b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(2048))
    )
    old = cap.transport.prepare(cap.destination)
    cap.transport.release(old["batch_id"])
    small = cap.transport.prepare(cap.destination, 4096)
    new = cap.journal.append({"kind": "later", "payload": {"new": True}})
    store = PlatformIngest(tmp_path / "api.db")
    try:
        for batch in [old, small] if old_first else [small, old]:
            receipt = store.accept("t", batch)
            assert cap.transport.confirm({ACK: receipt}, batch_id=receipt)
            if batch is small and not old_first:
                assert not cap.journal.db.execute(
                    "SELECT acknowledged FROM records WHERE id=?", (content + ":0",)
                ).fetchone()[0]
        assert cap.journal.db.execute(
            "SELECT acknowledged FROM records WHERE id=?", (content + ":0",)
        ).fetchone()[0]
        assert not cap.journal.db.execute(
            "SELECT acknowledged FROM records WHERE id=?", (new["event_id"],)
        ).fetchone()[0]
    finally:
        store.close()


@pytest.mark.parametrize("execution_version", [3, 4])
def test_fragment_restart_and_legacy_membership_migration(cap, tmp_path, execution_version):
    from opensquilla.observability.piggyback.transport import PiggybackTransport

    cap.transport = PiggybackTransport(cap.journal, execution_version=execution_version)
    value = cap.journal.append(
        {
            "kind": "large",
            "payload": {
                "values": [hashlib.sha256(str(i).encode()).hexdigest() for i in range(1800)]
            },
        }
    )
    old = cap.transport.prepare(cap.destination)
    cap.transport.release(old["batch_id"])
    # Emulate an existing database with only the old record->batch cursor.
    with cap.journal.transaction():
        cap.journal.db.execute("DELETE FROM batch_members")
    cap.transport = PiggybackTransport(cap.journal, execution_version=execution_version)
    store = PlatformIngest(tmp_path / "api.db")
    try:
        for index in range(64):
            batch = cap.transport.prepare(cap.destination, 4096)
            if batch is None:
                break
            receipt = store.accept("t", batch)
            assert cap.transport.confirm({ACK: receipt}, batch_id=receipt)
            if index == 1:
                cap.transport = PiggybackTransport(cap.journal, execution_version=execution_version)
        originals = [r for r in store.records("t", cap.journal.source_id) if r["type"] == "event"]
        assert [r["value"] for r in originals] == [value]
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["ordered", "reverse", "duplicate", "overlap"])
def test_fragments_restore_exact_record_without_arrival_order(tmp_path, mode):
    import json

    from opensquilla.observability.piggyback.fragments import fragment
    from opensquilla.observability.piggyback.identity import declaration, record, ref
    from opensquilla.observability.piggyback.protocol import encode

    original = record(declaration(ref("S", "authority", "A")))
    size = len(canonical(original))
    pieces = [fragment(original, 0, size // 2), fragment(original, size // 2, size - size // 2)]
    if mode == "reverse":
        pieces.reverse()
    elif mode == "duplicate":
        pieces *= 2
    elif mode == "overlap":
        pieces.insert(1, fragment(original, size // 3, size // 2))
    store = PlatformIngest(tmp_path / "api.db")
    try:
        for i, piece in enumerate(pieces):
            store.accept("t", encode("S", str(i), [piece]))
        # API-only query returns restored identity, not its raw fragment bytes.
        assert store.execution_identities("t") == [original["value"]]
        row = store.db.execute("SELECT body FROM records WHERE id=?", (original["id"],)).fetchone()
        assert json.loads(row[0]) == original
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["overlap", "total", "source", "version", "nested"])
def test_bad_fragments_cannot_produce_complete_identity(tmp_path, mutation):
    import base64

    from opensquilla.observability.piggyback.fragments import fragment
    from opensquilla.observability.piggyback.identity import declaration, record, ref
    from opensquilla.observability.piggyback.protocol import ProtocolError, encode

    original = record(declaration(ref("S", "authority", "A")))
    size = len(canonical(original))
    first = fragment(original, 0, size // 2)
    last = fragment(original, size // 3, size - size // 3)
    source = "S"
    if mutation == "overlap":
        raw = bytearray(base64.b64decode(last["value"]["data"]))
        raw[0] ^= 1
        last["value"]["data"] = base64.b64encode(raw).decode()
        last["value"]["chunk_sha256"] = digest(raw)
    elif mutation == "total":
        last["value"]["total"] += 1
    elif mutation == "version":
        last["value"]["version"] = True
    elif mutation == "source":
        source = "OTHER"
    else:
        first = fragment(first, 0, len(canonical(first)))
        last = first
    store = PlatformIngest(tmp_path / "api.db")
    try:
        with pytest.raises(ProtocolError):
            store.accept("t", encode(source, "batch", [first, last] if first != last else [last]))
        assert store.execution_identities("t") == []
    finally:
        store.close()
