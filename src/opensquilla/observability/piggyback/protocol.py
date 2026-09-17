"""Versioned, bounded wire contract. No provider SDK or network dependencies."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
from typing import Any

FIELD = "_opensquilla_trace"
ACK = "x-opensquilla-trace-ack"
REJECT = "x-opensquilla-trace-reject"
CALL = "x-opensquilla-call-id"
IDS_ACK = "x-opensquilla-call-ids-ack"
EXECUTION_ACK = "x-opensquilla-execution-ids-ack"
MAX_ENCODED = 1024 * 1024
MAX_DECODED = 8 * 1024 * 1024
CHUNK_SIZE = 128 * 1024
HEX = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
CONTEXT_KEYS = (
    "trace_id",
    "session_id",
    "turn_id",
    "user_turn_id",
    "task_id",
    "run_id",
    "parent_run_id",
    "agent_id",
    "logical_call_id",
    "execution_id",
    "call_id",
    "input_context_id",
    "tool_call_id",
    "tool_execution_id",
    "provider_execution_id",
    "call_kind",
    "iteration_id",
    "iteration_index",
    "outer_attempt_id",
    "outer_attempt_index",
    "agent_call_id",
    "ensemble_round_id",
    "candidate_id",
    "candidate_index",
    "sample_index",
    "aggregation_id",
    "selection_event_id",
    "role",
    "member_attempt_id",
    "member_attempt_index",
    "generation_epoch",
)


class ProtocolError(ValueError):
    """A permanent, safe-to-report protocol error (never include captured prose)."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProtocolError("invalid_identifier")
    return value


def encode(source_id: str, batch_id: str, records: list[dict]) -> dict:
    raw = canonical({"version": 1, "records": records})
    if len(raw) > MAX_DECODED:
        raise ProtocolError("decoded_limit")
    return {
        "version": 1,
        "source_id": identifier(source_id),
        "batch_id": identifier(batch_id),
        "sha256": digest(raw),
        "codec": "gzip+base64",
        "data": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii"),
    }


def validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise ProtocolError("invalid_record")
    identifier(record.get("id"))
    kind, value = record.get("type"), record.get("value")
    if not isinstance(value, dict):
        raise ProtocolError("invalid_value")
    if kind == "event":
        if value.get("schema_version") != 2 or value.get("event_id") != record["id"]:
            raise ProtocolError("event_version_or_identity")
        for name in ("event_id", "producer_id", "producer_epoch", "kind"):
            identifier(value.get(name))
        if type(value.get("seq")) is not int or value["seq"] < 1:
            raise ProtocolError("invalid_sequence")
        if not isinstance(value.get("context"), dict) or not isinstance(value.get("payload"), dict):
            raise ProtocolError("invalid_event")
        ctx = value["context"]
        for name in ("call_id", "user_turn_id", "input_context_id"):
            if name in ctx:
                identifier(ctx[name])
        if "call_id" in ctx and ctx["call_id"] != ctx.get("execution_id"):
            raise ProtocolError("call_identity_mismatch")
        if value["kind"].startswith("user_turn.") and not ctx.get("user_turn_id"):
            raise ProtocolError("missing_user_turn_identity")
        for name in (
            "iteration_index",
            "outer_attempt_index",
            "candidate_index",
            "sample_index",
            "member_attempt_index",
            "generation_epoch",
        ):
            if name in value["context"] and not re.fullmatch(
                r"[0-9]{1,20}", str(value["context"][name])
            ):
                raise ProtocolError("invalid_causal_index")
        for ref in value.get("blob_refs", []):
            if not isinstance(ref, str) or not HEX.fullmatch(ref):
                raise ProtocolError("invalid_blob_ref")
    elif kind == "blob":
        ref = value.get("sha256")
        if not isinstance(ref, str) or not HEX.fullmatch(ref):
            raise ProtocolError("invalid_blob_hash")
        offset, total = value.get("offset"), value.get("total")
        if type(offset) is not int or type(total) is not int or not 0 <= offset <= total:
            raise ProtocolError("invalid_blob_offset")
        try:
            data = base64.b64decode(value["data"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError("invalid_blob_encoding") from exc
        if len(data) > CHUNK_SIZE or offset + len(data) > total:
            raise ProtocolError("invalid_blob_size")
        if value.get("chunk_sha256") != digest(data) or record["id"] != f"{ref}:{offset}":
            raise ProtocolError("invalid_chunk_hash")
    elif kind == "record_fragment":
        from .fragments import validate_fragment

        validate_fragment(record)
    elif kind == "id_fact":
        from .identity_facts import to_declaration

        if type(value.get("version")) is not int or value["version"] != 4:
            raise ProtocolError("fact_version")
        to_declaration(value)
        if record["id"] != value["id"]:
            raise ProtocolError("identity_record_id")
    elif kind == "id_node":
        from .identity import validate

        validate(value)
        if record["id"] != value["id"]:
            raise ProtocolError("identity_record_id")
    elif kind == "turn_ids":
        from .id_graph import validate_turn_ids

        validate_turn_ids(value)
        if record["id"] != "turn_ids:" + value["user_turn_id"]:
            raise ProtocolError("turn_ids_record_identity")
    elif kind == "call_ids":
        from .id_graph import validate_call_ids

        validate_call_ids(value)
        if record["id"] != "call_ids:" + value["call_id"]:
            raise ProtocolError("call_ids_record_identity")
    elif kind == "call_correlation":
        from .original_correlation import validate as validate_correlation

        validate_correlation(record)
    elif kind == "manifest":
        identifier(value.get("run_id"))
        if record["id"] != "manifest:" + value["run_id"]:
            raise ProtocolError("manifest_identity_mismatch")
        ids = value.get("event_ids")
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            raise ProtocolError("invalid_manifest")
        for event_id in ids:
            identifier(event_id)
        for child in value.get("child_run_ids", []):
            identifier(child)
        if value.get("events_sha256") != digest(canonical(ids)):
            raise ProtocolError("invalid_manifest_hash")
    else:
        raise ProtocolError("unknown_record_type")


def decode(envelope: dict, max_encoded: int = MAX_ENCODED) -> list[dict]:
    if not isinstance(envelope, dict) or len(canonical(envelope)) > max_encoded:
        raise ProtocolError("encoded_limit")
    if envelope.get("version") in {2, 3, 4}:
        current, backlog = split_carrier(envelope)
        return decode(current) + (decode(backlog) if backlog else [])
    if envelope.get("version") != 1 or envelope.get("codec") != "gzip+base64":
        raise ProtocolError("unsupported_version_or_codec")
    identifier(envelope.get("source_id"))
    identifier(envelope.get("batch_id"))
    try:
        compressed = base64.b64decode(envelope["data"], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_DECODED + 1)
        if len(raw) > MAX_DECODED:
            raise ProtocolError("decoded_limit")
        if digest(raw) != envelope.get("sha256"):
            raise ProtocolError("batch_hash_mismatch")
        payload = json.loads(raw)
    except (ValueError, KeyError, TypeError, OSError, EOFError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_encoding") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ProtocolError("invalid_batch")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ProtocolError("empty_batch")
    seen = set()
    for record in records:
        validate_record(record)
        if record["type"] in {"id_node", "id_fact"}:
            from .identity import parts

            if parts(record["id"])[0] != envelope["source_id"]:
                raise ProtocolError("identity_source_mismatch")
        if (
            record["type"] in {"call_ids", "turn_ids", "call_correlation"}
            and record["value"]["source_id"] != envelope["source_id"]
        ):
            raise ProtocolError("record_source_mismatch")
        if record["id"] in seen:
            raise ProtocolError("duplicate_record")
        seen.add(record["id"])
    return records


def make_carrier(
    record: dict | None,
    backlog: dict | None,
    execution: list[dict] | None = None,
    *,
    execution_call_id: str | None = None,
    execution_version: int = 3,
) -> dict:
    if type(execution_version) is not int or execution_version not in {3, 4}:
        raise ProtocolError("execution_version")
    if execution:
        from .identity import parts

        source = parts(execution[0]["id"])[0]
        result = {
            "version": execution_version,
            "source_id": source,
            "call_ids": record["value"] if record else None,
            "call_ids_sha256": digest(canonical(record)) if record else None,
            "execution_ids": execution,
            "execution_call_id": execution_call_id,
            "execution_ids_sha256": digest(canonical(execution)),
            "backlog": backlog,
        }
        return result
    validate_record(record)
    return {
        "version": 2,
        "source_id": record["value"]["source_id"],
        "call_ids": record["value"],
        "call_ids_sha256": digest(canonical(record)),
        "backlog": backlog,
    }


def split_carrier(envelope: dict) -> tuple[dict, dict | None]:
    """Current IDs have an independent durable receipt; backlog keeps its v1 receipt."""
    if len(canonical(envelope)) > MAX_ENCODED:
        raise ProtocolError("encoded_limit")
    if envelope.get("version") in {3, 4}:
        if set(envelope) != {
            "version",
            "source_id",
            "call_ids",
            "call_ids_sha256",
            "execution_ids",
            "execution_ids_sha256",
            "execution_call_id",
            "backlog",
        }:
            raise ProtocolError("carrier_fields")
        records = envelope["execution_ids"]
        if (
            not isinstance(records, list)
            or not records
            or len(records) > 128
            or any(
                not isinstance(row, dict)
                or row.get("type")
                not in ({"id_node", "id_fact"} if envelope["version"] == 4 else {"id_node"})
                for row in records
            )
        ):
            raise ProtocolError("execution_ids_records")
        if digest(canonical(records)) != envelope["execution_ids_sha256"]:
            raise ProtocolError("execution_ids_hash_mismatch")
        records = list(records)
        if envelope["call_ids"] is not None:
            legacy = {
                key: envelope[key]
                for key in ("source_id", "call_ids", "call_ids_sha256", "backlog")
            }
            legacy["version"] = 2
            first, _ = split_carrier(legacy)
            records.extend(decode(first))
        elif envelope["call_ids_sha256"] is not None:
            raise ProtocolError("call_ids_hash_mismatch")
        source = identifier(envelope["source_id"])
        from .identity import parts

        call_namespace, call_kind, call_key = parts(envelope["execution_call_id"])
        if (
            call_namespace != source
            or call_kind != "call"
            or not any(row["id"] == envelope["execution_call_id"] for row in records)
        ):
            raise ProtocolError("carrier_call_identity")
        if envelope["call_ids"] and call_key != envelope["call_ids"]["call_id"]:
            raise ProtocolError("carrier_call_identity")
        current = encode(source, f"ids{envelope['version']}:" + digest(canonical(records)), records)
        decode(current)
        backlog = envelope["backlog"]
        if backlog is not None and (
            not isinstance(backlog, dict)
            or backlog.get("version") != 1
            or backlog.get("source_id") != source
        ):
            raise ProtocolError("carrier_backlog_mismatch")
        return current, backlog
    if set(envelope) != {"version", "source_id", "call_ids", "call_ids_sha256", "backlog"}:
        raise ProtocolError("carrier_fields")
    value = envelope["call_ids"]
    if not isinstance(value, dict):
        raise ProtocolError("call_ids_value")
    record = {
        "id": "call_ids:" + identifier(value.get("call_id")),
        "type": "call_ids",
        "value": value,
    }
    validate_record(record)
    if value["source_id"] != envelope["source_id"]:
        raise ProtocolError("carrier_source_mismatch")
    if digest(canonical(record)) != envelope["call_ids_sha256"]:
        raise ProtocolError("call_ids_hash_mismatch")
    backlog = envelope["backlog"]
    if backlog is not None and (
        not isinstance(backlog, dict)
        or backlog.get("version") != 1
        or backlog.get("source_id") != value["source_id"]
    ):
        raise ProtocolError("carrier_backlog_mismatch")
    current = encode(value["source_id"], "ids:" + value["call_id"], [record])
    return current, backlog
