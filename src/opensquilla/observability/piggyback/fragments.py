"""Versioned transport fragments of immutable canonical records, not new graph IDs."""

from __future__ import annotations

import base64
import json

from .protocol import CHUNK_SIZE, HEX, MAX_DECODED, ProtocolError, canonical, digest, identifier


def fragment(record: dict, start: int, length: int) -> dict:
    raw = canonical(record)
    data = raw[start : start + length]
    sha = digest(raw)
    return {
        "type": "record_fragment",
        "id": f"fragment:{sha}:{start}:{len(data)}",
        "value": {
            "version": 1,
            "record_id": record["id"],
            "record_sha256": sha,
            "offset": start,
            "total": len(raw),
            "length": len(data),
            "chunk_sha256": digest(data),
            "data": base64.b64encode(data).decode(),
        },
    }


def validate_fragment(record: dict) -> bytes:
    value = record["value"]
    if (
        set(value)
        != {
            "version",
            "record_id",
            "record_sha256",
            "offset",
            "total",
            "length",
            "chunk_sha256",
            "data",
        }
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ProtocolError("fragment_version_or_fields")
    identifier(value["record_id"])
    if not isinstance(value["record_sha256"], str) or not HEX.fullmatch(value["record_sha256"]):
        raise ProtocolError("fragment_hash")
    start, total, length = value["offset"], value["total"], value["length"]
    if any(type(n) is not int for n in (start, total, length)) or not (
        0 <= start < total <= MAX_DECODED and 0 < length <= CHUNK_SIZE and start + length <= total
    ):
        raise ProtocolError("fragment_range")
    try:
        data = base64.b64decode(value["data"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("fragment_encoding") from exc
    if (
        len(data) != length
        or digest(data) != value["chunk_sha256"]
        or record["id"] != f"fragment:{value['record_sha256']}:{start}:{length}"
    ):
        raise ProtocolError("fragment_integrity")
    return data


def assemble(records: list[dict], source: str) -> dict | None:
    """Only return a whole, source-validated original record; never guess missing bytes."""
    from .protocol import decode, encode

    if not records:
        return None
    first = records[0]["value"]
    pieces = []
    for record in records:
        data = validate_fragment(record)
        value = record["value"]
        if any(value[k] != first[k] for k in ("record_id", "record_sha256", "total")):
            raise ProtocolError("fragment_identity_conflict")
        pieces.append((value["offset"], data))
    raw = bytearray()
    for start, data in sorted(pieces):
        if start > len(raw):
            return None
        overlap = min(len(data), len(raw) - start)
        if raw[start : start + overlap] != data[:overlap]:
            raise ProtocolError("fragment_overlap_conflict")
        raw.extend(data[overlap:])
    if len(raw) != first["total"]:
        return None
    if digest(raw) != first["record_sha256"]:
        raise ProtocolError("fragment_record_hash")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("fragment_record_encoding") from exc
    if (
        not isinstance(record, dict)
        or record.get("type") == "record_fragment"
        or record.get("id") != first["record_id"]
        or canonical(record) != raw
    ):
        raise ProtocolError("fragment_record_identity")
    # Full original schema and source validation, including v3/v4 fact equivalence.
    return decode(encode(source, "fragment-validation", [record]), max_encoded=MAX_DECODED * 2)[0]


def uncovered(intervals, total):
    """First unconfirmed interval of one exact canonical representation."""
    end = 0
    for start, stop in sorted(intervals):
        if start > end:
            return end, start
        end = max(end, stop)
    return (end, total) if end < total else None
