"""Original OpenSquilla request-correlation values bound to one physical Call.

Upstream correlation headers
(X-OpenSquilla-Session-Id / Turn-Id / Execution-Id / Call-Kind) are only sent to
TokenRhythm origins, so a self-hosted trace platform never sees them on the wire.
This record carries the very same values next to the TracePoint ``call`` identity
so the two identity systems can be joined on the platform without guessing.

It is an external identity mapping record, not a new graph identity class: the
ID reconstruction never reads it, and the platform exposes it as a separate view.
"""

from __future__ import annotations

from .protocol import IDENTIFIER, ProtocolError, identifier

TYPE = "call_correlation"
FIELDS = ("install_id", "session_id", "turn_id", "execution_id", "call_kind")
STATUSES = frozenset({"present", "unavailable", "disabled", "redacted", "invalid"})


def build(source_id: str, call_id: str, correlation, *, disabled: bool = False) -> dict:
    """Freeze the correlation actually available at this Call's send boundary.

    ``disabled`` mirrors the upstream network-observability privacy switch: the
    original headers are suppressed, so the JSON copy must not bypass that policy.
    """
    from opensquilla.provider.tokenrhythm_correlation import (
        _safe_call_kind,
        _safe_correlation_id,
    )

    original: dict[str, str | None] = dict.fromkeys(FIELDS)
    status: dict[str, str] = {}
    for name in FIELDS:
        if disabled:
            status[name] = "disabled"
            continue
        if name == "install_id":
            # Upstream retired the installation identity header (returns {}).
            # Report it as unavailable; never substitute source_id or a new value.
            status[name] = "unavailable"
            continue
        raw = getattr(correlation, name, None) if correlation is not None else None
        if raw is None or not str(raw).strip():
            status[name] = "unavailable"
            continue
        safe = _safe_call_kind(raw) if name == "call_kind" else _safe_correlation_id(raw)
        if not safe or not IDENTIFIER.fullmatch(safe):
            status[name] = "invalid"
            continue
        original[name] = safe
        status[name] = "present"
    value = {
        "version": 1,
        "source_id": source_id,
        "call_id": call_id,
        "original": original,
        "field_status": status,
    }
    return {"type": TYPE, "id": f"{TYPE}:{call_id}", "value": value}


def validate(record: dict) -> None:
    from .identity import parts

    value = record["value"]
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ProtocolError("correlation_version")
    if set(value) != {"version", "source_id", "call_id", "original", "field_status"}:
        raise ProtocolError("correlation_fields")
    identifier(value["source_id"])
    namespace, kind, _ = parts(value["call_id"])
    if kind != "call" or namespace != value["source_id"]:
        raise ProtocolError("correlation_call")
    original, status = value["original"], value["field_status"]
    if (
        not isinstance(original, dict)
        or not isinstance(status, dict)
        or set(original) != set(FIELDS)
        or set(status) != set(FIELDS)
    ):
        raise ProtocolError("correlation_fields")
    for name in FIELDS:
        if status[name] not in STATUSES:
            raise ProtocolError("correlation_status")
        if status[name] == "present":
            identifier(original[name])
        elif original[name] is not None:
            raise ProtocolError("correlation_value")
    if record["id"] != f"{TYPE}:{value['call_id']}":
        raise ProtocolError("correlation_record_identity")
