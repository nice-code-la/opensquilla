from __future__ import annotations

import asyncio
import copy
import uuid
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.observability.piggyback.capture import (
    Capture,
    _active,
    child_task_context,
    context,
    trace_run,
)
from opensquilla.observability.piggyback.http import post_llm
from opensquilla.observability.piggyback.protocol import CALL, digest, encode
from opensquilla.observability.piggyback.reconstruct import TraceReconstructor
from opensquilla.observability.piggyback.server import create_platform
from opensquilla.observability.piggyback.transport import Destination
from tests.test_piggyback_causality import WireScenario
from tests.test_provider_ensemble import _member


@pytest.fixture
def recording(tmp_path):
    cap = Capture(
        tmp_path / "client", Destination("https://platform.test", "tenant", digest(b"key"))
    )
    active, identity = _active.set(cap), context.set({})
    yield cap
    context.reset(identity)
    _active.reset(active)
    cap.journal.close()


def make_agent(scenario):
    return Agent(
        provider=scenario.provider(_member("p2").provider_config),
        config=AgentConfig(max_iterations=2),
        session_key="same-session",
    )


@pytest.mark.parametrize("parallel", [False, True])
async def test_separate_user_inputs_have_unique_turns_and_call_locations(
    recording, tmp_path, parallel
):
    scenario = WireScenario(tmp_path / "platform.db")
    agent = make_agent(scenario)

    async def consume(current, message):
        return [e async for e in current.run_turn(message)]

    try:
        if parallel:
            await asyncio.gather(
                consume(agent, "first input"), consume(make_agent(scenario), "second input")
            )
        else:
            await consume(agent, "first input")
            await consume(agent, "second input")
        bundle = TraceReconstructor().reconstruct(recording.journal.records())
        assert len(scenario.requests) == len(bundle["call_index"]) == 2
        assert len(bundle["user_turns"]) == 2
        assert all(t["complete"] for t in bundle["user_turns"].values())
        assert len({t["session_id"] for t in bundle["user_turns"].values()}) == 1
        for call_id, entry in bundle["call_index"].items():
            location = entry["location"]
            assert entry["position_status"] == "located"
            assert location["call_id"] == call_id
            assert location["iteration_index"] == "1"  # Iteration resets do not collide.
            assert location["outer_attempt_id"] and location["trace_id"]
            turn = bundle["user_turns"][location["user_turn_id"]]
            assert turn["call_ids"] == [call_id]
            assert location["run_id"] in turn["run_ids"]
            assert (
                bundle["input_contexts"][entry["input_context_id"]]["request"]
                == (bundle["model_contexts"][call_id]["request"])
            )
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_user_turn_propagates_to_nested_and_delegated_runs_despite_provider_turn_ids(
    recording,
):
    observed_headers = []

    def upstream(request):
        observed_headers.append(dict(request.headers))
        return httpx.Response(200, json={"text": "ok"})

    @trace_run
    async def run_turn(message, *, agent_id="main"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            await post_llm(
                client,
                "https://platform.test/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                correlation=SimpleNamespace(
                    session_id="native-session",
                    turn_id="provider-turn-" + agent_id,
                    execution_id="reused-native-id",
                    call_kind="agent",
                ),
                json={"model": "fake", "messages": [{"role": "user", "content": message}]},
            )
        yield {"kind": "done"}

    @trace_run
    async def run(message, *, root_turn_id=None):
        assert [e async for e in run_turn(message)]

        async def child(name):
            return [e async for e in run_turn("delegated " + name, agent_id=name)]

        await asyncio.gather(
            *(
                asyncio.create_task(
                    child(name), context=child_task_context(uuid.uuid4().hex, {"name": name})
                )
                for name in ("worker-a", "worker-b")
            )
        )
        yield {"kind": "done"}

    assert [e async for e in run("actual user input")]
    bundle = TraceReconstructor().reconstruct(recording.journal.records())
    assert len(bundle["user_turns"]) == 1
    turn = next(iter(bundle["user_turns"].values()))
    assert turn["complete"] and len(turn["run_ids"]) == 4 and len(turn["call_ids"]) == 3
    assert {h[CALL] for h in observed_headers} == set(turn["call_ids"])
    assert len({c["identity"]["turn_id"] for c in bundle["model_contexts"].values()}) == 3
    assert {c["identity"]["user_turn_id"] for c in bundle["model_contexts"].values()} == {
        turn["user_turn_id"]
    }


async def test_call_lookup_is_direct_tenant_scoped_and_tracks_late_tail(recording, tmp_path):
    scenario = WireScenario(tmp_path / "platform.db")
    app = create_platform(
        {
            "sqlite_path": str(tmp_path / "platform.db"),
            "token_hash_to_tenant": {digest(b"key"): "tenant", digest(b"other"): "other-tenant"},
            "upstream_base_url": "https://unused.test",
        }
    )
    auth = {"Authorization": "Bearer key"}
    try:
        agent = make_agent(scenario)
        assert [e async for e in agent.run_turn("first actual user task")]
        local = TraceReconstructor().reconstruct(recording.journal.records())
        call_id = next(iter(local["call_index"]))
        await scenario.app.drain()
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://platform.test"
            ) as client:
                url = "/calls/" + call_id
                assert (await client.get(url)).status_code == 401
                assert (
                    await client.get(url, headers={"Authorization": "Bearer other"})
                ).status_code == 404
                before = (await client.get(url, headers=auth)).json()
                assert before["location"] == local["call_index"][call_id]["location"]
                assert not before["run_complete"]
                assert not before["user_turn"]["complete"]
                # Only a second actual user input carries the previous task's tail.
                assert [e async for e in agent.run_turn("second actual user task")]
                await scenario.app.drain()
                after_response = await client.get(url, headers=auth)
                assert after_response.status_code == 200
                after = after_response.json()
                assert after["location"] == before["location"]
                assert after["run_complete"] and after["user_turn"]["complete"]
                assert after["input_context"]["request"] == scenario.requests[0]
                source, turn_id = after["source_id"], after["location"]["user_turn_id"]
                assert (await client.get(f"/traces/{source}/turns/{turn_id}", headers=auth)).json()[
                    "call_ids"
                ] == [call_id]
                assert (await client.get("/calls/not-received", headers=auth)).status_code == 404
                # Same ID in a different source is evidence of ambiguity, never a winner.
                request_record = next(
                    r
                    for r in recording.journal.records()
                    if r["type"] == "event"
                    and r["value"]["kind"] == "llm.request"
                    and r["value"]["context"]["execution_id"] == call_id
                )
                app.store.accept(
                    "tenant", encode("collision-source", uuid.uuid4().hex, [request_record])
                )
                assert (await client.get(url, headers=auth)).status_code == 409
                assert len(scenario.requests) == 2
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_duplicate_call_identity_and_missing_context_never_guess_position(
    recording, tmp_path
):
    scenario = WireScenario(tmp_path / "platform.db")
    try:
        assert [e async for e in make_agent(scenario).run_turn("large input " * 2000)]
        records = recording.journal.records()
        original = next(
            r for r in records if r["type"] == "event" and r["value"]["kind"] == "llm.request"
        )
        call_id = original["value"]["context"]["call_id"]
        duplicate = copy.deepcopy(original)
        duplicate["id"] = duplicate["value"]["event_id"] = uuid.uuid4().hex
        duplicate["value"]["seq"] += 10000
        ambiguous = TraceReconstructor().reconstruct([*records, duplicate])
        assert ambiguous["call_index"][call_id]["position_status"] == "ambiguous"
        assert ambiguous["call_index"][call_id]["location"] is None
        assert not all(r["complete"] for r in ambiguous["runs"].values())
        partial = TraceReconstructor().reconstruct([r for r in records if r["type"] != "blob"])
        entry = partial["call_index"][call_id]
        assert entry["position_status"] == "located"
        assert entry["input_context_id"] not in partial["input_contexts"]
        assert not entry["run_complete"]
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_identical_request_snapshots_do_not_merge_user_turns_or_calls(recording):
    @trace_run
    async def run(message):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": "ok"}))
        ) as client:
            await post_llm(
                client,
                "https://platform.test/v1/chat/completions",
                headers={"Authorization": "Bearer key"},
                json={"model": "same", "messages": [{"role": "user", "content": message}]},
            )
        yield {"kind": "done"}

    assert [e async for e in run("identical input")]
    assert [e async for e in run("identical input")]
    records = recording.journal.records()
    bundle = TraceReconstructor().reconstruct(records)
    assert len(bundle["input_contexts"]) == 1
    assert len(bundle["user_turns"]) == len(bundle["call_index"]) == 2
    assert all(t["complete"] for t in bundle["user_turns"].values())
    collision = copy.deepcopy(records)
    same_turn = next(iter(bundle["user_turns"]))
    for r in collision:
        if r["type"] == "event" and r["value"]["context"].get("user_turn_id"):
            r["value"]["context"]["user_turn_id"] = same_turn
    ambiguous = TraceReconstructor().reconstruct(collision)
    assert all(c["position_status"] == "ambiguous" for c in ambiguous["call_index"].values())
    assert not any(t["complete"] for t in ambiguous["user_turns"].values())
