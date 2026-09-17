from __future__ import annotations

import asyncio
import copy
import json
import os
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.observability.piggyback.capture import (
    Capture,
    _active,
    context,
    trace_tool_handler,
)
from opensquilla.observability.piggyback.export import html_report, write_bundle
from opensquilla.observability.piggyback.http import post_llm
from opensquilla.observability.piggyback.platform import (
    PlatformIngest,
    TraceMiddleware,
    token_authenticator,
)
from opensquilla.observability.piggyback.protocol import FIELD, digest
from opensquilla.observability.piggyback.reconstruct import TraceReconstructor
from opensquilla.observability.piggyback.transport import Destination
from opensquilla.provider import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider.ensemble import EnsembleProvider
from tests.test_provider_ensemble import _FakeProvider, _member


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)


@pytest.fixture
def recording(tmp_path):
    cap = Capture(
        tmp_path / "client", Destination("https://platform.test", "tenant", digest(b"key"))
    )
    active = _active.set(cap)
    identity = context.set({})
    yield cap
    context.reset(identity)
    _active.reset(active)
    cap.journal.close()


class WireScenario:
    """Real Agent/Ensemble coordinators, offline HTTP model responses and piggyback ingestion."""

    def __init__(self, path, *, fallback=False):
        self.counts = Counter()
        self.requests = []
        self.fallback = fallback
        self.started = asyncio.Event()
        self.block = None
        self.store = PlatformIngest(path)
        self.app = TraceMiddleware(
            self.model, self.store, token_authenticator({digest(b"key"): "tenant"})
        )

    async def model(self, scope, receive, send):
        body = json.loads((await receive())["body"])
        assert FIELD not in body
        self.requests.append(body)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        model = body["model"]
        self.counts[model] += 1
        attempt = self.counts[model]
        await asyncio.sleep(0.005 if model == "p2" else 0.015)
        if model == "p1" and attempt == 1:
            response = {"error": "upstream overloaded", "code": "503"}
        elif model.startswith("p"):
            response = {"text": f"draft-{model}-{attempt} " * 40}
        elif model == "next":
            response = {"text": "answer to the next user question"}
        elif model == "agg" and self.fallback:
            response = {"error": "upstream overloaded", "code": "503"}
        elif "tool_result" not in json.dumps(body["messages"]):
            response = {"tool": "echo", "tool_id": "tool-1"}
        else:
            response = {"text": "final answer"}
        response["usage"] = {"prompt_tokens": 7, "completion_tokens": 3}
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(response).encode()})

    def provider(self, cfg):
        scenario = self

        class WireProvider(_FakeProvider):
            async def _chat(self, messages, *, tools=None, config=None):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=scenario.app)
                ) as client:
                    response = await post_llm(
                        client,
                        "https://platform.test/v1/chat/completions",
                        headers={"Authorization": "Bearer key"},
                        correlation=config.provider_request_correlation if config else None,
                        json={
                            "model": self._cfg.model,
                            "messages": [
                                m.model_dump(mode="json", exclude_none=True) for m in messages
                            ],
                        },
                    )
                result = response.json()
                if result.get("error"):
                    yield ErrorEvent(message=result["error"], code=result["code"])
                    return
                if result.get("tool"):
                    yield ToolUseStartEvent(tool_use_id=result["tool_id"], tool_name=result["tool"])
                    yield ToolUseEndEvent(
                        tool_use_id=result["tool_id"], tool_name=result["tool"], arguments={}
                    )
                if result.get("text"):
                    yield TextDeltaEvent(text=result["text"])
                yield DoneEvent(
                    stop_reason="tool_use" if result.get("tool") else "stop",
                    model=self._cfg.model,
                    input_tokens=7,
                    output_tokens=3,
                )

        return WireProvider(cfg, SimpleNamespace(calls=[]))


@pytest.mark.parametrize("fallback", [False, True])
async def test_real_loop_parallel_ensemble_http_causality(
    recording, tmp_path, monkeypatch, fallback
):
    import opensquilla.provider.ensemble as ensemble

    scenario = WireScenario(tmp_path / "platform.sqlite3", fallback=fallback)
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
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded tool result"
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=trace_tool_handler(tool, None),
    )
    try:
        output = [e async for e in agent.run_turn("use echo and answer")]
        assert any(e.kind == "done" and "final answer" in e.text for e in output)
        records = recording.journal.records()
        bundle = TraceReconstructor().reconstruct(records, provenance="local")
        assert all(r["complete"] for r in bundle["runs"].values()), bundle["runs"]
        iterations = sorted(bundle["loop_iterations"].values(), key=lambda row: row["index"])
        assert [r["index"] for r in iterations] == [1, 2]
        rounds = sorted(
            bundle["ensemble_rounds"].values(),
            key=lambda row: int(row["identity"]["iteration_index"]),
        )
        assert len(rounds) == 2
        assert len(rounds[0]["scheduled_candidates"]) == 3
        assert (
            len(rounds[1]["scheduled_candidates"]) == 0
        )  # Sticky continuation reuses earlier drafts.
        first_candidates = list(rounds[0]["candidates"].values())
        assert sorted(len(c["payload"]["member_attempt_ids"]) for c in first_candidates) == [
            1,
            1,
            2,
        ]
        calls = bundle["model_contexts"]
        assert len(calls) == len(scenario.requests)
        assert len(bundle["user_turns"]) == 1
        user_turn = next(iter(bundle["user_turns"].values()))
        assert user_turn["complete"]
        assert set(user_turn["call_ids"]) == set(calls)
        for call_id, entry in bundle["call_index"].items():
            assert entry["position_status"] == "located"
            assert entry["location"]["call_id"] == call_id
            assert entry["location"]["user_turn_id"] == user_turn["user_turn_id"]
            assert entry["location"]["member_attempt_id"]
            assert entry["causality_status"] == "resolved"
        # Parallel peers are not predecessors; retries and fan-in inputs are explicit.
        member_calls = {
            m["identity"]["member_attempt_id"]: m["physical_call_ids"]
            for r in rounds
            for m in r["members"].values()
        }
        for round_view in rounds:
            for member in round_view["members"].values():
                if previous := member["start"].get("previous_attempt_id"):
                    current = member["physical_call_ids"][0]
                    assert (
                        bundle["call_index"][current]["predecessor_call_ids"]
                        == member_calls[previous]
                    )
        first_selection = rounds[0]["selections"][0]
        successful_inputs = {
            call
            for chosen in first_selection["payload"]["input_candidates"]
            for call in member_calls[chosen["accepted_member_attempt_id"]]
        }
        primary = next(
            m
            for m in rounds[0]["members"].values()
            if m["identity"]["role"] == "aggregator"
            and m["identity"]["member_attempt_index"] == "0"
        )
        assert (
            set(bundle["call_index"][primary["physical_call_ids"][0]]["predecessor_call_ids"])
            == successful_inputs
        )
        assert all(
            c["identity"].get("iteration_id") and c["identity"].get("member_attempt_id")
            for c in calls.values()
        )
        assert (
            len(
                {
                    c["identity"]["candidate_id"]
                    for c in calls.values()
                    if c["identity"]["role"] == "proposer"
                }
            )
            == 3
        )
        expected_role = "fixed_aggregator" if fallback else "aggregator"
        assert rounds[-1]["end"]["final_role"] == expected_role
        if fallback:
            assert scenario.counts["agg"] == 2
            selections = rounds[0]["selections"]
            assert any(s["payload"].get("fallback_from_attempt_id") for s in selections)
        for round_view in rounds:
            for selection in round_view["selections"]:
                chosen = selection["payload"]["input_candidates"]
                assert chosen and [c["position"] for c in chosen] == list(range(1, len(chosen) + 1))
                assert [c["index"] for c in chosen] == [2, 1, 0]
                assert all(c["candidate_id"] in rounds[0]["candidates"] for c in chosen)
                projected_texts = []
                for candidate in chosen:
                    projected = recording.journal.content.get(
                        candidate["projected_content_ref"]
                    ).decode()
                    source = rounds[0]["candidates"][candidate["candidate_id"]]["payload"]["text"]
                    assert len(projected) < len(source)
                    projected_texts.append(projected)
                aggregation_calls = [
                    c
                    for c in calls.values()
                    if c["identity"].get("aggregation_id") == selection["context"]["aggregation_id"]
                ]
                assert aggregation_calls
                for call in aggregation_calls:
                    # Compare recorded selection to the actual forwarded model messages.
                    wire_text = "\n".join(_text_values(call["request"]["messages"]))
                    positions = [wire_text.index(text) for text in projected_texts]
                    assert positions == sorted(positions)
        assert any(e["relation"] == "next_iteration" for e in bundle["causal_graph"])
        assert all(e["relation"] != "producer_order" for e in bundle["causal_graph"])
        # A successful raw call connects to the next aggregation via candidate evidence.
        events_by_id = {e["event_id"]: e for e in bundle["events"]}
        assert any(
            events_by_id[e["from"]]["kind"] == "llm.end"
            and events_by_id[e["to"]]["kind"] == "ensemble.member.end"
            for e in bundle["causal_graph"]
        )
        assert any(
            events_by_id[e["from"]]["kind"] == "ensemble.round.end"
            and events_by_id[e["to"]]["kind"] == "loop.attempt.end"
            for e in bundle["causal_graph"]
        )
        for seed in range(3):
            shuffled = records * 2
            random.Random(seed).shuffle(shuffled)
            assert TraceReconstructor().reconstruct(shuffled, provenance="local") == bundle
        # A present but wrong attempt reference must not be accepted by identity guessing.
        changed = copy.deepcopy(records)
        selection = next(
            r["value"]
            for r in changed
            if r["type"] == "event" and r["value"]["kind"] == "ensemble.selection"
        )
        selected_inputs = selection["payload"]["input_candidates"]
        selected_inputs[0]["accepted_member_attempt_id"] = selected_inputs[1][
            "accepted_member_attempt_id"
        ]
        broken = TraceReconstructor().reconstruct(changed)
        assert any("candidate_attempt_mismatch" in r["reasons"] for r in broken["runs"].values())
        invalid = copy.deepcopy(records)
        next(
            r["value"]
            for r in invalid
            if r["type"] == "event" and r["value"]["kind"] == "loop.iteration.start"
        )["context"]["iteration_index"] = "NaN"
        assert any(
            "invalid_records" in r["reasons"]
            for r in TraceReconstructor().reconstruct(invalid)["runs"].values()
        )
        # One ordinary subsequent task request carries the prior run's final tail.
        next_agent = Agent(
            provider=scenario.provider(_member("next").provider_config),
            config=AgentConfig(max_iterations=1),
        )
        assert [event async for event in next_agent.run_turn("Answer my next user question")]
        await scenario.app.drain()
        platform = TraceReconstructor().reconstruct(
            scenario.store.records("tenant", recording.journal.source_id), provenance="platform"
        )
        assert all(platform["runs"][run]["platform_complete"] for run in bundle["runs"])
        assert len(platform["user_turns"]) == 2
        assert all(entry["location"]["user_turn_id"] for entry in platform["call_index"].values())
        assert len(scenario.requests) == len(calls) + 1
        if directory := os.environ.get("TRACEPOINT_REPORT_DIR"):
            target = Path(directory) / ("fallback" if fallback else "parallel")
            target.mkdir(parents=True, exist_ok=True)
            write_bundle(platform, target / "trace.json")
            (target / "report.html").write_text(html_report(platform))
            (target / "summary.json").write_text(
                json.dumps(
                    {
                        "business_requests": len(scenario.requests),
                        "model_requests": dict(scenario.counts),
                        "iterations": len(iterations),
                        "rounds": len(rounds),
                        "source_runs_complete": True,
                        "model_backend": "offline ASGI scripted responses",
                    },
                    indent=2,
                )
            )
    finally:
        await scenario.app.drain()
        scenario.store.close()


async def test_outer_retry_stays_within_iteration_and_request_count_is_unchanged(
    recording, tmp_path
):
    counts = []
    for enabled in (False, True):
        scenario = WireScenario(tmp_path / f"retry-{enabled}.sqlite3")
        token = _active.set(recording if enabled else None)
        agent = Agent(
            provider=scenario.provider(_member("p1").provider_config),
            config=AgentConfig(max_iterations=2, retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        )
        try:
            assert any(
                e.kind == "done" and e.text
                for e in [e async for e in agent.run_turn("answer the question")]
            )
            counts.append(len(scenario.requests))
        finally:
            _active.reset(token)
            await scenario.app.drain()
            scenario.store.close()
    assert counts == [2, 2]
    bundle = TraceReconstructor().reconstruct(recording.journal.records())
    assert all(r["complete"] for r in bundle["runs"].values()), bundle["runs"]
    assert len(bundle["loop_iterations"]) == 1
    attempts = next(iter(bundle["loop_iterations"].values()))["attempts"]
    assert len(attempts) == 2
    assert len({a["identity"]["logical_call_id"] for a in attempts}) == 1
    assert len({a["identity"]["outer_attempt_id"] for a in attempts}) == 2
    assert [int(a["identity"]["outer_attempt_index"]) for a in attempts] == [0, 1]


async def test_parallel_cancellation_closes_scopes_without_upload_flush(
    recording, tmp_path, monkeypatch
):
    import opensquilla.provider.ensemble as ensemble

    scenario = WireScenario(tmp_path / "cancel.sqlite3")
    scenario.block = asyncio.Event()
    monkeypatch.setattr(ensemble, "_build_provider", scenario.provider)
    provider = EnsembleProvider(
        profile_name="cancel",
        proposers=[_member("p1"), _member("p2")],
        aggregator=_member("agg"),
        all_failed_policy="error",
    )
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=2))

    async def consume():
        return [e async for e in agent.run_turn("answer")]

    task = asyncio.create_task(consume())
    try:
        await scenario.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        count = len(scenario.requests)
        await scenario.app.drain()
        bundle = TraceReconstructor().reconstruct(recording.journal.records())
        assert all(r["complete"] for r in bundle["runs"].values()), bundle["runs"]
        assert all(r["end"]["status"] == "interrupted" for r in bundle["ensemble_rounds"].values())
        assert not scenario.counts["agg"]
        assert len(scenario.requests) == count
    finally:
        await scenario.app.drain()
        scenario.store.close()
