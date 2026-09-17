"""Strict ID-only execution declarations. Values never contain captured prose or clocks."""

from __future__ import annotations

import re
import uuid

from .protocol import ProtocolError

VERSION = 3
PROFILE = "opensquilla:profile:execution_v3"
LOOP = "opensquilla:profile:loop_v3"
AUXILIARY = "opensquilla:profile:auxiliary_v3"
CONSTANTS = {PROFILE, LOOP, AUXILIARY}
REF = re.compile(r"^([A-Za-z0-9_.-]{1,64}):([a-z_]{1,32}):([A-Za-z0-9_.-]{1,96})$")
# Fields with a trailing * are ordered arrays. A ? permits null, never an unknown target.
# Each reference has a type; '*' as a type means any registered declaration.
SCHEMAS = {
    "session": {"authority_id": "authority"},
    "authority": {},
    "branch": {"session_id": "session", "fork_id": "checkpoint?"},
    "turn": {"branch_id": "branch", "previous_id": "turn?", "input_id": "input"},
    "input": {"native_id": "native_input?"},
    "native_input": {},
    "input_group": {"turn_ids*": "turn"},
    "activation": {"input_group_id": "input_group", "trigger_id": "*?"},
    "run": {"branch_id": "branch", "activation_id": "activation", "dispatch_id": "dispatch?"},
    "phase": {"run_id": "run", "trigger_id": "*?", "profile_id": "profile"},
    "lane": {"phase_id": "phase", "fork_id": "*?"},
    "iteration": {"phase_id": "phase", "previous_id": "iteration?"},
    "operation": {
        "lane_id": "lane",
        "iteration_id": "iteration?",
        "parent_id": "operation?",
        "trigger_id": "*?",
    },
    "attempt": {"operation_id": "operation", "previous_id": "attempt?", "outer_id": "attempt?"},
    "attempt_success": {"attempt_id": "attempt"},
    "attempt_failure": {"attempt_id": "attempt"},
    "attempt_interrupted": {"attempt_id": "attempt"},
    "fallback_transition": {"from_attempt_id": "attempt", "to_attempt_id": "attempt"},
    "context": {
        "message_ids*": "message",
        "selection_id": "selection?",
        "definition_ids*": "definition",
    },
    "definition": {},
    "call": {
        "attempt_id": "attempt",
        "context_id": "context",
        "previous_id": "call?",
        "retry_id": "call?",
        "fallback_id": "call?",
    },
    "received": {"call_id": "call"},
    "call_success": {"call_id": "call"},
    "call_failure": {"call_id": "call"},
    "call_interrupted": {"call_id": "call"},
    "result": {"producer_id": "*", "source_ids*": "*"},
    "acceptance": {"operation_id": "operation", "result_id": "result"},
    "message": {"source_ids*": "*"},
    "transform": {"input_ids*": "*"},
    "action": {"message_id": "message"},
    "tool_attempt": {"run_id": "run", "action_id": "action?"},
    "tool_execution": {"attempt_id": "tool_attempt"},
    "tool_success": {"execution_id": "tool_execution", "result_id": "result"},
    "tool_failure": {"execution_id": "tool_execution", "result_id": "result?"},
    "tool_cancelled": {"execution_id": "tool_execution"},
    "tool_rejection": {"attempt_id": "tool_attempt"},
    "dispatch": {
        "run_id": "run",
        "call_id": "call?",
        "tool_id": "tool_execution?",
        "completion_id": "dispatch_completion",
    },
    "dispatch_completion": {
        "dispatch_id": "dispatch",
        "child_closure_id": "closure?",
        "cancelled_id": "dispatch_cancelled?",
    },
    "dispatch_cancelled": {"dispatch_id": "dispatch"},
    "wait": {"operation_id": "*", "result_ids*": "*"},
    "ensemble": {"operation_id": "operation"},
    "candidate": {"ensemble_id": "ensemble", "lane_id": "lane"},
    "candidate_result": {"candidate_id": "candidate", "result_id": "result"},
    "candidate_failure": {"candidate_id": "candidate"},
    "candidate_cancelled": {"candidate_id": "candidate"},
    "projection": {"candidate_result_id": "candidate_result"},
    "selection": {"ensemble_id": "ensemble", "projection_ids*": "projection"},
    "selection_reuse": {"selection_id": "selection", "source_ensemble_id": "ensemble"},
    "aggregation": {
        "ensemble_id": "ensemble",
        "operation_id": "operation",
        "selection_id": "selection?",
        "fallback_id": "aggregation?",
    },
    "generation": {"attempt_id": "attempt"},
    "generation_output": {"generation_id": "generation", "result_id": "result"},
    "generation_discarded": {"generation_id": "generation"},
    "replacement": {"previous_id": "generation", "next_id": "generation"},
    "commit": {"message_ids*": "message", "native_id": "native_input?"},
    "delivery": {"message_ids*": "message"},
    "delivered": {"delivery_id": "delivery"},
    "delivery_failed": {"delivery_id": "delivery"},
    "checkpoint": {"run_id": "run?", "branch_id": "branch", "message_ids*": "message"},
    "run_success": {"run_id": "run"},
    "run_failure": {"run_id": "run"},
    "run_cancelled": {"run_id": "run"},
    "gap": {"affected_id": "*?"},
    "closure": {
        "run_id": "run",
        "profile_id": "profile",
        "part_ids*": "closure_part",
        "child_ids*": "closure",
        "gap_ids*": "gap",
    },
    "closure_part": {"closure_id": "closure", "required_ids*": "*"},
}


def parts(value):
    match = REF.fullmatch(value) if isinstance(value, str) else None
    if not match or len(value) > 160:
        raise ProtocolError("identity_reference")
    return match.groups()


def ref(namespace, kind, key=None):
    value = f"{namespace}:{kind}:{key or uuid.uuid4().hex}"
    parts(value)
    return value


def kind(value):
    return parts(value)[1]


def declaration(identity, scope_id=None, **fields):
    value = {"version": VERSION, "id": identity, "scope_id": scope_id, **fields}
    validate(value)
    return value


def validate(value):
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VERSION
    ):
        raise ProtocolError("identity_version")
    namespace, typ, _ = parts(value.get("id"))
    schema = SCHEMAS.get(typ)
    if schema is None:
        raise ProtocolError("identity_kind")
    expected = {"version", "id", "scope_id", *(field.rstrip("*") for field in schema)}
    if set(value) != expected:
        raise ProtocolError("identity_fields")
    scope = value["scope_id"]
    if scope is not None and (kind(scope) != "run" or parts(scope)[0] != namespace):
        raise ProtocolError("identity_scope")
    for field, target in schema.items():
        item = value[field.rstrip("*")]
        optional = target.endswith("?")
        target = target.rstrip("?")
        values = item if field.endswith("*") else [item]
        if field.endswith("*") and (not isinstance(item, list) or len(item) > 4096):
            raise ProtocolError("identity_array")
        for identity in values:
            if identity is None and optional:
                continue
            _, actual, _ = parts(identity)
            if actual not in SCHEMAS and actual != "profile":
                raise ProtocolError("identity_unknown_reference_kind")
            if target != "*" and actual != target:
                raise ProtocolError("identity_reference_kind")
    return value


def references(value):
    """All references; schema constants are verified independently, never fetched remotely."""
    if value.get("version") == 4:
        from .identity_facts import to_declaration

        value = to_declaration(value)
    if value["scope_id"]:
        yield "scope_id", value["scope_id"]
    for field in SCHEMAS[kind(value["id"])]:
        name = field.rstrip("*")
        values = value[name] if field.endswith("*") else [value[name]]
        for identity in values:
            if identity:
                yield name, identity


def record(value):
    validate(value)
    return {"type": "id_node", "id": value["id"], "value": value}
