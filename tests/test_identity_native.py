"""Native transaction, authoritative input order, replay and receipt failure proofs."""

from types import SimpleNamespace

import pytest

from opensquilla.observability.piggyback.identity import kind
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.identity_native import activation_inputs, sync
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.test_piggyback_id_graph import recording as recording
from tests.test_piggyback_id_v2 import offline as offline


@pytest.fixture
async def native(tmp_path):
    storage = SessionStorage(str(tmp_path / "native.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create("agent:main:main")
    yield storage, manager, session
    await storage.close()


async def test_native_user_acceptance_order_uses_transaction_heads_and_unique_message_ids(
    recording, native
):
    storage, manager, session = native
    first = await manager.append_message(session.session_key, "user", "same text", message_id="M1")
    second = await manager.append_message(session.session_key, "user", "same text", message_id="M2")
    first_id, second_id = (
        entry.turn_context["execution_user_turn_id"] for entry in (first, second)
    )
    assert first_id != second_id
    nodes = {row["id"]: row for row in recording.identities.declarations()}
    assert nodes[first_id]["previous_id"] is None
    assert nodes[second_id]["previous_id"] == first_id
    ids = await activation_inputs(
        SimpleNamespace(_session_manager=manager),
        {"session_key": session.session_key, "bound_user_message_id": "M2"},
    )
    assert ids["_execution_turn_ids"] == [second_id]
    assert ids["_execution_session_id"] == session.session_id
    # An actual upsert of the same native identity does not advance the head.
    entry, epoch = await manager.prepare_message(
        session.session_key, "user", "revised content", message_id="M2"
    )
    async with storage._write_transaction("test_upsert") as conn:
        assert not await storage._upsert_transcript_entry(conn, entry, expected_epoch=epoch)
    assert entry.turn_context["execution_user_turn_id"] == second_id
    assert (
        len([row for row in recording.identities.declarations() if kind(row["id"]) == "turn"]) == 2
    )


async def test_native_rollback_does_not_publish_identity_or_advance_turn_head(recording, native):
    storage, manager, session = native
    entry, epoch = await manager.prepare_message(
        session.session_key, "user", "never committed", message_id="rollback"
    )
    with pytest.raises(RuntimeError, match="injected"):
        async with storage._write_transaction("test_rollback") as conn:
            await storage._insert_transcript_entry(conn, entry, expected_epoch=epoch)
            raise RuntimeError("injected")
    assert recording.identities.declarations() == []
    next_entry = await manager.append_message(
        session.session_key, "user", "committed", message_id="next"
    )
    turn = recording.identities.get(next_entry.turn_context["execution_user_turn_id"])
    assert turn["previous_id"] is None
    transcript = await manager.read_transcript(session.session_key)
    assert [row["message_id"] for row in transcript] == ["next"]


async def test_native_copy_failure_keeps_business_row_and_retries_only_local_copy(
    recording, native, monkeypatch
):
    storage, manager, session = native
    original = recording.identities._put
    monkeypatch.setattr(
        recording.identities, "_put", lambda row: (_ for _ in ()).throw(OSError("disk unavailable"))
    )
    entry = await manager.append_message(session.session_key, "user", "durable", message_id="M1")
    assert entry.turn_context["execution_user_turn_id"]
    assert recording.identities.declarations() == []
    transcript = await manager.read_transcript(session.session_key)
    assert len(transcript) == 1
    monkeypatch.setattr(recording.identities, "_put", original)
    await sync(storage.conn)
    assert recording.identities.get(entry.turn_context["execution_user_turn_id"])
    before = recording.identities.declarations()
    await sync(storage.conn)
    assert recording.identities.declarations() == before


async def test_native_source_boundaries_do_not_merge_different_session_inputs(recording, native):
    storage, manager, first = native
    second = await manager.create("agent:main:other")
    a = await manager.append_message(first.session_key, "user", "same", message_id="M1")
    b = await manager.append_message(second.session_key, "user", "same", message_id="M1")
    nodes = {row["id"]: row for row in recording.identities.declarations()}
    ta, tb = (nodes[entry.turn_context["execution_user_turn_id"]] for entry in (a, b))
    assert ta["id"] != tb["id"] and ta["branch_id"] != tb["branch_id"]
    assert ta["previous_id"] is tb["previous_id"] is None
    restored = ExecutionIdReconstructor().reconstruct(list(nodes.values()))
    assert len(restored["sessions"]) == 2
    assert restored["session_complete"] is None


async def test_real_native_runtime_binds_original_input_and_commit_to_the_same_graph(
    recording, native, tmp_path
):
    from opensquilla.observability.piggyback.capture import trace_run
    from tests.test_piggyback_causality import WireScenario
    from tests.test_piggyback_id_v2 import agent

    storage, manager, session = native
    wire = WireScenario(tmp_path / "platform.db")

    class Runtime:
        _session_manager = manager

        @trace_run
        async def run(self, message, session_key, persist_input=True):
            await manager.append_message(session_key, "user", message)
            runner = agent(wire)
            async for event in runner.run_turn(message):
                yield event
            await manager.append_message(session_key, "assistant", "persisted result")

    try:
        assert [event async for event in Runtime().run("question", session.session_key)]
        rebuilt = ExecutionIdReconstructor().reconstruct(recording.identities.declarations())
        assert not recording.failed_runs
        assert all(scope["complete"] for scope in rebuilt["scopes"].values()), (
            rebuilt["scopes"],
            rebuilt["issues"],
        )
        assert len(rebuilt["positions"]) == len(wire.requests) == 1
        position = next(iter(rebuilt["positions"].values()))
        assert rebuilt["nodes"][position["session_id"]]["id"].endswith(session.session_id)
        rows = await manager.read_transcript(session.session_key)
        native_turn = rows[0]["turn_context"]["execution_user_turn_id"]
        group = rebuilt["nodes"][rebuilt["nodes"][position["activation_id"]]["input_group_id"]]
        assert group["turn_ids"] == [native_turn]
        receipt = rows[-1]["turn_context"]["execution_commit_id"]
        assert receipt in rebuilt["nodes"]
        assert len(wire.requests) == 1
    finally:
        await wire.app.drain()
        wire.store.close()


async def test_native_fork_links_checkpoint_without_accepting_copied_user_inputs_again(
    recording, native
):
    _, manager, session = native
    original = await manager.append_message(
        session.session_key, "user", "original", message_id="M1"
    )
    child = await manager.branch(session.session_key, "agent:main:branch", fork_transcript=True)
    rows = await manager.read_transcript(child.session_key)
    assert (
        rows[0]["turn_context"]["execution_user_turn_id"]
        == original.turn_context["execution_user_turn_id"]
    )
    fresh = await manager.append_message(child.session_key, "user", "new branch task")
    nodes = {row["id"]: row for row in recording.identities.declarations()}
    turn = nodes[fresh.turn_context["execution_user_turn_id"]]
    assert turn["previous_id"] is None
    fork = nodes[nodes[turn["branch_id"]]["fork_id"]]
    assert nodes[fork["branch_id"]]["session_id"].endswith(session.session_id)
    assert fork["message_ids"]
    assert len([row for row in nodes.values() if kind(row["id"]) == "turn"]) == 2


async def test_native_multi_input_activation_uses_explicit_ordered_ids(recording, native):
    from opensquilla.observability.piggyback.capture import context

    _, manager, session = native
    entries = [
        await manager.append_message(session.session_key, "user", value, message_id=value)
        for value in ("M1", "M2", "M3")
    ]
    token = context.set({"_execution_input_ids": ["M1", "M2", "M3"]})
    try:
        result = await activation_inputs(
            SimpleNamespace(_session_manager=manager), {"session_key": session.session_key}
        )
    finally:
        context.reset(token)
    assert result["_execution_turn_ids"] == [
        entry.turn_context["execution_user_turn_id"] for entry in entries
    ]
