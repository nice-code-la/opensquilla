"""V4 lifecycle facts, with stable v3 reference anchors during migration.

The logical fact key is (subject_id, dimension). Keeping the original record ID
preserves references and closure inventories, including references to cancellation
observations. No body, clock, or model name participates in reconstruction.
"""

from __future__ import annotations

from .identity import kind
from .identity import validate as validate_declaration
from .protocol import ProtocolError, canonical

# old kind -> (subject field, one-shot fact dimension, outcome)
FACT_SPECS = {
    "attempt_success": ("attempt_id", "attempt_terminal", "success"),
    "attempt_failure": ("attempt_id", "attempt_terminal", "failure"),
    "attempt_interrupted": ("attempt_id", "attempt_terminal", "interrupted"),
    "received": ("call_id", "response_received", "received"),
    "call_success": ("call_id", "transport_terminal", "success"),
    "call_failure": ("call_id", "transport_terminal", "failure"),
    "call_interrupted": ("call_id", "transport_terminal", "interrupted"),
    "tool_success": ("execution_id", "execution_terminal", "success"),
    "tool_failure": ("execution_id", "execution_terminal", "failure"),
    "tool_cancelled": ("execution_id", "execution_terminal", "cancelled"),
    "tool_rejection": ("attempt_id", "validation_rejection", "rejected"),
    "candidate_failure": ("candidate_id", "candidate_terminal", "failure"),
    "candidate_cancelled": ("candidate_id", "candidate_terminal", "cancelled"),
    "generation_discarded": ("generation_id", "generation_discarded", "discarded"),
    "dispatch_cancelled": ("dispatch_id", "dispatch_cancelled", "cancelled"),
    "delivered": ("delivery_id", "delivery_terminal", "success"),
    "delivery_failed": ("delivery_id", "delivery_terminal", "failure"),
    "run_success": ("run_id", "run_terminal", "success"),
    "run_failure": ("run_id", "run_terminal", "failure"),
    "run_cancelled": ("run_id", "run_terminal", "cancelled"),
}


def fact_key(fact):
    """Canonical tuple encoding; the outcome is deliberately absent from the key."""
    return canonical([fact["subject_id"], fact["dimension"]]).decode()


def to_fact(value):
    validate_declaration(value)
    spec = FACT_SPECS.get(kind(value["id"]))
    if spec is None:
        return value
    subject_field, dimension, outcome = spec
    fact = {
        "version": 4,
        "id": value["id"],
        "scope_id": value["scope_id"],
        "subject_id": value[subject_field],
        "dimension": dimension,
        "outcome": outcome,
    }
    if "result_id" in value:
        fact["result_id"] = value["result_id"]
    return fact


def to_declaration(value):
    """Normalize the versioned representation before the existing strict checker."""
    if not isinstance(value, dict) or value.get("version") != 4:
        validate_declaration(value)
        return value
    if type(value["version"]) is not int:
        raise ProtocolError("fact_version")
    typ = kind(value.get("id"))
    spec = FACT_SPECS.get(typ)
    if spec is None:
        raise ProtocolError("fact_kind")
    subject_field, dimension, outcome = spec
    fields = {"version", "id", "scope_id", "subject_id", "dimension", "outcome"}
    has_result = typ in {"tool_success", "tool_failure"}
    if has_result:
        fields.add("result_id")
    if set(value) != fields:
        raise ProtocolError("fact_fields")
    if value["dimension"] != dimension or value["outcome"] != outcome:
        raise ProtocolError("fact_semantics")
    declaration = {
        "version": 3,
        "id": value["id"],
        "scope_id": value["scope_id"],
        subject_field: value["subject_id"],
    }
    if has_result:
        declaration["result_id"] = value["result_id"]
    validate_declaration(declaration)
    return declaration


def wire_record(record, version=4):
    """Represent newly batched records; never mutate an existing sealed batch."""
    if version == 3 or record["type"] != "id_node":
        return record
    value = to_fact(record["value"])
    if value["version"] != 4:
        return record
    return {"type": "id_fact", "id": record["id"], "value": value}


def equivalent_records(left, right):
    """Only the two validated encodings of the very same declaration are equal."""
    if left.get("type") not in {"id_node", "id_fact"} or right.get("type") not in {
        "id_node",
        "id_fact",
    }:
        return False
    if left.get("id") != right.get("id"):
        return False
    return canonical(to_declaration(left["value"])) == canonical(to_declaration(right["value"]))
