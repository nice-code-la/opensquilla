"""V4 migration must preserve identity, negative evidence, and durable delivery."""

import copy
import random

import pytest

from opensquilla.observability.piggyback.identity import SCHEMAS, declaration, kind, record, ref
from opensquilla.observability.piggyback.identity_facts import (
    FACT_SPECS,
    fact_key,
    to_declaration,
    to_fact,
    wire_record,
)
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.journal import TraceJournal
from opensquilla.observability.piggyback.platform import PlatformIngest
from opensquilla.observability.piggyback.protocol import (
    EXECUTION_ACK,
    ProtocolError,
    canonical,
    decode,
    digest,
    encode,
    make_carrier,
    split_carrier,
)
from opensquilla.observability.piggyback.transport import Destination, PiggybackTransport
from tests.test_identity_complete import recorded_run
from tests.test_identity_complete import registry as registry


@pytest.mark.parametrize("typ", sorted(FACT_SPECS))
def test_every_lifecycle_kind_roundtrips_exactly(typ):
    fields = {name: ref("N", target.rstrip("?"), "target") for name, target in SCHEMAS[typ].items()}
    node = declaration(ref("N", typ, "occurrence"), ref("N", "run", "run"), **fields)
    fact = to_fact(node)
    assert fact["version"] == 4
    assert to_declaration(fact) == node
    assert decode(encode("N", "B", [wire_record(record(node))]))[0]["value"] == fact
    altered = {**fact, "outcome": "invented"}
    with pytest.raises(ProtocolError, match="fact_semantics"):
        to_declaration(altered)


def test_mixed_arrival_duplicates_and_all_required_deletions(registry):
    run, _, _ = recorded_run(registry, extra_branch=True)
    old = registry.declarations()
    new = [to_fact(row) for row in old]
    expected = ExecutionIdReconstructor().reconstruct(old)
    assert expected["scopes"][run]["complete"]
    for seed in range(10):
        rows = copy.deepcopy(old + new + new)
        random.Random(seed).shuffle(rows)
        assert ExecutionIdReconstructor().reconstruct(rows) == expected
    for omitted in new:
        graph = ExecutionIdReconstructor().reconstruct([r for r in new if r is not omitted])
        assert not graph["scopes"].get(run, {}).get("complete", False), omitted
    for call, locator in expected["call_locators"].items():
        assert set(locator) == {
            "session_id",
            "user_turn_id",
            "run_id",
            "iteration_id",
            "operation_id",
            "call_id",
            "context_id",
        }
        assert locator["call_id"] == call
        assert expected["call_relations"][call]["location_status"] == "unique"


def test_cancelled_tool_observation_remains_addressable(registry):
    run, _, _ = recorded_run(registry)
    rows = registry.declarations()
    attempt = ref(registry.namespace, "tool_attempt", "cancel-test")
    execution = ref(registry.namespace, "tool_execution", "cancel-test")
    terminal = ref(registry.namespace, "tool_cancelled", "cancel-test")
    result = ref(registry.namespace, "result", "cancel-test")
    extra = [
        declaration(attempt, run, run_id=run, action_id=None),
        declaration(execution, run, attempt_id=attempt),
        declaration(terminal, run, execution_id=execution),
        declaration(result, run, producer_id=terminal, source_ids=[]),
    ]
    part = next(r for r in rows if kind(r["id"]) == "closure_part")
    part["required_ids"].extend(r["id"] for r in extra)
    rows.extend(extra)
    graph = ExecutionIdReconstructor().reconstruct([to_fact(r) for r in rows])
    assert graph["scopes"][run]["complete"], graph["scopes"]
    assert graph["entities"][result]["producer_id"] == terminal
    assert graph["facts"][terminal]["subject_id"] == execution
    assert graph["facts"][terminal]["outcome"] == "cancelled"
    missing = ExecutionIdReconstructor().reconstruct(
        [to_fact(r) for r in rows if r["id"] != terminal]
    )
    assert terminal in missing["missing_ids"]
    assert not missing["scopes"][run]["complete"]


@pytest.mark.parametrize("other_kind", ["call_success", "call_failure"])
def test_different_terminal_ids_never_hide_conflicts(registry, other_kind):
    run, _, _ = recorded_run(registry)
    rows = registry.declarations()
    original = next(r for r in rows if kind(r["id"]) == "call_success")
    second = {**original, "id": ref(registry.namespace, other_kind, "duplicate")}
    assert fact_key(to_fact(original)) == fact_key(to_fact(second))
    graph = ExecutionIdReconstructor().reconstruct([to_fact(r) for r in rows + [second]])
    assert not graph["scopes"][run]["complete"]
    assert any(name == "conflicting_lifecycle_fact" for name, _ in graph["issues"])


def test_invalid_fact_and_unresolved_input_do_not_gain_unique_location(registry):
    run, _, _ = recorded_run(registry)
    rows = [to_fact(r) for r in registry.declarations()]
    fact = next(r for r in rows if r["version"] == 4)
    fact["subject_id"] = ref(registry.namespace, "definition", "wrong-type")
    graph = ExecutionIdReconstructor().reconstruct(rows)
    assert graph["invalid_declarations"]
    assert not graph["scopes"][run]["complete"]
    rows = [r for r in registry.declarations() if kind(r["id"]) != "input_group"]
    graph = ExecutionIdReconstructor().reconstruct(rows)
    assert all(r["location_status"] == "pending" for r in graph["call_relations"].values())


@pytest.mark.parametrize("order", [(3, 4), (4, 3)])
def test_platform_accepts_equivalent_versions_without_overwriting(tmp_path, order):
    store = PlatformIngest(tmp_path / "platform.db")
    try:
        node = declaration(ref("N", "call_success", "end"), call_id=ref("N", "call", "call"))
        for index, version in enumerate(order):
            rec = wire_record(record(node), version)
            assert store.accept("tenant", encode("N", f"B{index}", [rec])) == f"B{index}"
        assert len(store.execution_identities("tenant")) == 1
        assert store.records("tenant", "N")[0] == wire_record(record(node), order[0])
        conflict = wire_record(record({**node, "call_id": ref("N", "call", "wrong")}), 4)
        with pytest.raises(ProtocolError, match="record_identity_conflict"):
            store.accept("tenant", encode("N", "B-conflict", [conflict]))
        graph = ExecutionIdReconstructor().reconstruct(store.execution_identities("tenant"))
        assert node["id"] in graph["conflicting_ids"]
        assert store.execution_identities("other-tenant") == []
    finally:
        store.close()


def test_sealed_batch_survives_version_switch_and_ack(tmp_path):
    destination = Destination("http://127.0.0.1:12345", "tenant", digest(b"key"))
    journal = TraceJournal(tmp_path / "journal", destination.key)
    try:
        from opensquilla.observability.piggyback.identity_registry import IdentityRegistry

        registry = IdentityRegistry(journal)
        recorded_run(registry)
        transport = PiggybackTransport(journal, execution_version=4)
        batch = transport.prepare(destination)
        assert any(r["type"] == "id_fact" for r in decode(batch))
        transport.release(batch["batch_id"])
        rolled_back = PiggybackTransport(journal, execution_version=3)
        assert rolled_back.prepare(destination) == batch
        assert not rolled_back.confirm({}, batch_id=batch["batch_id"])
        assert rolled_back.confirm(
            {"x-opensquilla-trace-ack": batch["batch_id"]}, batch_id=batch["batch_id"]
        )
        assert rolled_back.prepare(destination) is None
    finally:
        journal.close()


def test_v4_current_prefix_preserves_receipt_and_rejects_wrong_namespace(registry):
    recorded_run(registry)
    call = next(r["id"] for r in registry.declarations() if kind(r["id"]) == "call")
    records = [wire_record(r) for r in registry.call_bundle(call)]
    carrier = make_carrier(None, None, records, execution_call_id=call, execution_version=4)
    current, backlog = split_carrier(carrier)
    assert backlog is None
    assert decode(current) == records
    assert carrier["execution_ids_sha256"] == digest(canonical(records))
    carrier["source_id"] = "other-source"
    with pytest.raises(ProtocolError):
        split_carrier(carrier)


async def test_real_agent_v4_acks_fact_batches_and_leaves_tail(recording, scenario):
    from tests.test_piggyback_id_v2 import agent

    recording.execution_version = recording.transport.execution_version = 4
    runner = agent(scenario)
    assert [e async for e in runner.run_turn("first task")]
    assert [e async for e in runner.run_turn("second task")]
    assert len(scenario.requests) == 2
    assert not recording.failed_runs
    stored = [
        r for s in scenario.store.sources("tenant") for r in scenario.store.records("tenant", s)
    ]
    assert any(r["type"] == "id_fact" for r in stored)
    ids = scenario.store.execution_identities("tenant")
    graph = ExecutionIdReconstructor().reconstruct(ids)
    assert len(graph["call_locators"]) == 2
    assert any(s["complete"] for s in graph["scopes"].values())
    assert any(not s["complete"] for s in graph["scopes"].values())
    assert any(EXECUTION_ACK.encode() in h for h in scenario.app.response_headers)


async def test_legacy_compaction_records_call_and_marks_unknown_source(recording, monkeypatch):
    import httpx

    from opensquilla.session.compaction import call_compaction_llm

    recording.execution_version = recording.transport.execution_version = 4
    requests = []

    async def model(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "summary"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(model), **kw)
    )
    result = await call_compaction_llm(
        "history", "keep identifiers", "mock", "key", "https://platform.test"
    )
    assert result == "summary"
    assert len(requests) == 1
    assert requests[0]["_opensquilla_trace"]["version"] == 4
    graph = ExecutionIdReconstructor().reconstruct(recording.identities.declarations())
    assert len(graph["call_locators"]) == 1
    assert all(p["iteration_id"] is None for p in graph["call_locators"].values())
    # The legacy string API has no native provenance DTO. Since the 2026-09-17 review
    # fix the auxiliary Call records its projected wire input with an explicit unknown
    # native origin (input.native_id is null) instead of an unconditional coverage gap.
    assert not any(kind(n) == "gap" for n in graph["nodes"])
    call = next(iter(graph["call_locators"]))
    context = graph["nodes"][graph["nodes"][call]["context_id"]]
    wire = graph["nodes"][context["message_ids"][0]]
    prepared = graph["nodes"][wire["source_ids"][0]]["input_ids"]
    assert len(prepared) == 1
    origin = graph["nodes"][graph["nodes"][prepared[0]]["source_ids"][0]]
    assert kind(origin["id"]) == "input" and origin["native_id"] is None
    assert all(scope["complete"] for scope in graph["scopes"].values())


# Existing fixtures use the real Agent and Provider with an ASGI mock platform.
from tests.test_piggyback_id_graph import recording as recording  # noqa: E402, F401
from tests.test_piggyback_id_v2 import offline as offline  # noqa: E402, F401
from tests.test_piggyback_id_v2 import scenario as scenario  # noqa: E402, F401
