"""ID closure contracts with independently enumerated expected execution relationships."""

import copy
import random

import pytest

from opensquilla.observability.piggyback.identity import LOOP, PROFILE, ref
from opensquilla.observability.piggyback.identity_graph import ExecutionIdReconstructor
from opensquilla.observability.piggyback.identity_registry import IdentityRegistry
from opensquilla.observability.piggyback.journal import TraceJournal
from opensquilla.observability.piggyback.protocol import ProtocolError, decode, encode


@pytest.fixture
def registry(tmp_path):
    journal = TraceJournal(tmp_path / "journal", "destination")
    yield IdentityRegistry(journal)
    journal.close()


def recorded_run(registry, *, calls=2, extra_branch=False):
    r = registry
    run = r.id("run")
    r.open(run)
    authority = r.put("authority")
    session = r.put("session", authority_id=authority)
    branch = r.put("branch", session_id=session, fork_id=None)
    user_input = r.put("input", native_id=None)
    turn = r.put("turn", branch_id=branch, previous_id=None, input_id=user_input)
    group = r.put("input_group", turn_ids=[turn])
    activation = r.put("activation", input_group_id=group, trigger_id=None)
    r.put(
        "run",
        run,
        key=run.split(":")[-1],
        branch_id=branch,
        activation_id=activation,
        dispatch_id=None,
    )
    phase = r.put("phase", run, run_id=run, trigger_id=None, profile_id=LOOP)
    lane = r.put("lane", run, phase_id=phase, fork_id=None)
    message = r.put("message", run, source_ids=[user_input])
    previous_iteration = previous_call = None
    expected = []
    for _ in range(calls):
        iteration = r.put("iteration", run, phase_id=phase, previous_id=previous_iteration)
        operation = r.put(
            "operation", run, lane_id=lane, iteration_id=iteration, parent_id=None, trigger_id=None
        )
        attempt = r.put("attempt", run, operation_id=operation, previous_id=None, outer_id=None)
        context = r.put("context", run, message_ids=[message], selection_id=None, definition_ids=[])
        call = r.put(
            "call",
            run,
            attempt_id=attempt,
            context_id=context,
            previous_id=previous_call,
            retry_id=None,
            fallback_id=None,
        )
        # A complete response claim carries its response_received observation.
        r.put("received", run, call_id=call)
        r.put("call_success", run, call_id=call)
        r.put("attempt_success", run, attempt_id=attempt)
        result = r.put("result", run, producer_id=call, source_ids=[])
        accepted = r.put("acceptance", run, operation_id=operation, result_id=result)
        message = r.put("message", run, source_ids=[accepted])
        if previous_call:
            expected.append([previous_call, call, "call.previous_id"])
        previous_iteration, previous_call = iteration, call
    if extra_branch:
        r.put("definition", run)
    closure = r.seal(run)
    return run, closure, expected


def test_unique_position_closure_and_arrival_invariance(registry):
    run, closure, expected = recorded_run(registry)
    rows = registry.declarations()
    graph = ExecutionIdReconstructor().reconstruct(rows)
    assert graph["references_closed"], graph
    assert graph["scopes"][run]["complete"], graph["scopes"]
    assert all(edge in graph["edges"] for edge in expected)
    assert graph["scopes"][run]["closure_id"] == closure
    for seed in range(25):
        shuffled = copy.deepcopy(rows + rows)
        random.Random(seed).shuffle(shuffled)
        assert ExecutionIdReconstructor().reconstruct(shuffled) == graph


def test_deleting_every_required_declaration_never_reports_complete(registry):
    run, _, _ = recorded_run(registry, extra_branch=True)
    rows = registry.declarations()
    for omitted in rows:
        graph = ExecutionIdReconstructor().reconstruct([row for row in rows if row is not omitted])
        assert not graph["scopes"].get(run, {}).get("complete", False), omitted


def test_no_closure_is_not_an_empty_completed_run(registry):
    run, _, _ = recorded_run(registry)
    rows = [row for row in registry.declarations() if ":closure:" not in row["id"]]
    assert not ExecutionIdReconstructor().reconstruct(rows)["scopes"][run]["complete"]


def test_sealed_scope_cannot_be_mutated_and_restart_preserves_it(registry):
    run, closure, _ = recorded_run(registry)
    with pytest.raises(ValueError, match="scope_not_open"):
        registry.put("definition", run)
    reopened = IdentityRegistry(registry.journal)
    assert reopened.seal(run) == closure
    assert ExecutionIdReconstructor().reconstruct(reopened.declarations())["scopes"][run][
        "complete"
    ]


@pytest.mark.parametrize("mutation", ["source", "prose", "version", "target_type"])
def test_wire_rejects_invalid_id_declarations(registry, mutation):
    recorded_run(registry)
    node = next(row for row in registry.declarations() if ":call:" in row["id"])
    if mutation == "source":
        source = "OTHER"
    else:
        source = registry.namespace
    if mutation == "prose":
        node["text"] = "forbidden"
    if mutation == "version":
        node["version"] = True
    if mutation == "target_type":
        node["context_id"] = ref(source, "call", "wrong")
    batch = encode(source, "B", [{"type": "id_node", "id": node["id"], "value": node}])
    with pytest.raises(ProtocolError):
        decode(batch)


def test_conflicting_owner_never_selects_first_arrival(registry):
    run, _, _ = recorded_run(registry)
    rows = registry.declarations()
    original = next(row for row in rows if ":call:" in row["id"])
    conflict = {**original, "scope_id": ref(registry.namespace, "run", "other")}
    before = ExecutionIdReconstructor().reconstruct([conflict, *rows])
    after = ExecutionIdReconstructor().reconstruct([*rows, conflict])
    assert before == after
    assert not before["scopes"][run]["complete"]
    assert original["id"] in before["conflicting_ids"]


def test_unknown_profile_cannot_certify_complete(registry):
    run, _, _ = recorded_run(registry)
    rows = [
        {**row, "profile_id": ref("opensquilla", "profile", "unknown")}
        if row.get("profile_id") == PROFILE
        else row
        for row in registry.declarations()
    ]
    assert not ExecutionIdReconstructor().reconstruct(rows)["scopes"][run]["complete"]


@pytest.mark.parametrize(
    "missing_kind", ["attempt", "operation", "lane", "phase", "branch", "session"]
)
def test_missing_owner_declaration_is_pending_not_a_wrong_owner_conflict(registry, missing_kind):
    run, _, _ = recorded_run(registry, calls=1)
    values = registry.declarations()
    omitted = next(row["id"] for row in values if row["id"].split(":")[1] == missing_kind)
    call = next(row["id"] for row in values if row["id"].split(":")[1] == "call")
    rebuilt = ExecutionIdReconstructor().reconstruct(
        [row for row in values if row["id"] != omitted]
    )
    assert rebuilt["position_status"][call] == "pending"
    assert rebuilt["scopes"][run]["state"] == "pending"


def test_external_identity_aliases_use_exact_keys_and_do_not_merge_collisions(
    registry, monkeypatch
):
    from types import SimpleNamespace

    import opensquilla.observability.piggyback.identity as identity

    monkeypatch.setattr(identity.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    first = registry.id("session", "native:session/one")
    assert registry.id("session", "native:session/one") == first
    with pytest.raises(ValueError, match="allocation_collision"):
        registry.id("session", "native:session/two")
