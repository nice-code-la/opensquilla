"""Explicit provider send seams, preserving the original client's retry and TLS policies."""

from __future__ import annotations

import base64
import contextlib
import uuid

import httpx

from .capture import context, get_capture, serializable
from .protocol import (
    CALL,
    EXECUTION_ACK,
    FIELD,
    IDS_ACK,
    MAX_ENCODED,
    canonical,
    digest,
    make_carrier,
)


class _Call:
    def __init__(self, url, kwargs, correlation=None, *, allow_piggyback=True, auxiliary=False):
        self.capture = get_capture()
        self.batch = None
        self.ids_record = None
        self.ids_sent = False
        self.operation_binding = None
        self.execution_binding = None
        self.execution_provider_binding = None
        self.execution_records = []
        self.execution_sent = False
        self.id_state = None
        self.token = None
        self.call_id = uuid.uuid4().hex
        self.ordinal = 0
        self.body_complete = False
        self.kwargs = kwargs
        self.identity = None
        self.request_event = None
        self.url = str(url)
        if not self.capture:
            return
        try:
            if (
                auxiliary
                or not context.get().get("_id_provider_scope")
                and (
                    not context.get().get("_id_state")
                    or context.get().get("tool_execution_id")
                    or getattr(correlation, "call_kind", "").startswith("auxiliary.")
                )
            ):
                from .id_capture import operation_context

                self.operation_binding = operation_context(force_auxiliary=True)
        except Exception:
            self.capture.fail()
        identity = {**context.get(), "execution_id": self.call_id, "call_id": self.call_id}
        if correlation:
            identity.update(
                {
                    "session_id": correlation.session_id,
                    "turn_id": correlation.turn_id,
                    "provider_execution_id": correlation.execution_id,
                    "call_kind": correlation.call_kind,
                }
            )
        business = kwargs.get("json", {})
        if isinstance(business, dict):
            business = {k: v for k, v in business.items() if k != FIELD}
            # Generic Message.model_dump is the durable representation. Provider
            # adapters project business fields explicitly; this handles generic
            # adapters without touching user content or tool argument dictionaries.
            if isinstance(business.get("messages"), list):
                business = {
                    **business,
                    "messages": [
                        {k: v for k, v in message.items() if k != "trace_ids"}
                        if isinstance(message, dict)
                        and message.get("role") in {"user", "assistant"}
                        else message
                        for message in business["messages"]
                    ],
                }
                kwargs["json"] = business
        try:
            identity["input_context_id"] = digest(
                canonical({"version": 1, "request": serializable(business)})
            )
        except Exception:
            self.capture.fail()
        self.token = context.set(identity)
        self.identity = identity
        from . import identity_runtime

        if auxiliary or not context.get().get("_execution_ids"):
            # Legacy string APIs (call_compaction_llm) have no message DTOs. Project
            # the actual wire messages so the auxiliary Call records its real inputs
            # instead of an unconditional gap. Unknown native origin stays explicit.
            self.execution_provider_binding = identity_runtime.provider_enter(
                _wire_messages(business) if auxiliary else [], auxiliary=auxiliary
            )
        self.execution_binding = identity_runtime.call_start(self.call_id, business)
        if self.execution_binding:
            # Bind the original upstream correlation values (as actually available at
            # this send boundary) to the physical Call. Uploaded in the backlog of
            # this or a later business request; never a separate request.
            try:
                from .original_correlation import build as correlation_record

                record = correlation_record(
                    self.capture.journal.source_id,
                    self.execution_binding[1],
                    correlation,
                    disabled=_network_observability_disabled(),
                )
                with self.capture.journal.transaction():
                    self.capture.journal._capacity(len(canonical(record)) * 2)
                    self.capture.journal._insert(record, self.execution_binding[0].run)
            except Exception:
                self.capture.fail()
        try:
            from .id_capture import new_call

            self.ids_record = new_call(self.call_id)
            self.id_state = context.get().get("_id_state")
        except Exception:
            self.capture.fail()
        from .causality import note_physical_call

        note_physical_call(self.call_id)
        recorded = self.capture.emit(
            "llm.request", {"url": self.url.split("?", 1)[0], "request": business}
        )
        self.request_event = recorded["event_id"] if recorded else None
        try:
            headers = dict(kwargs.get("headers") or {})
            disabled = _network_observability_disabled()
            if (
                allow_piggyback
                and not disabled
                and self.capture.current_configuration_allows_capture()
                and self.capture.destination.matches(self.url, headers)
            ):
                payload = kwargs.get("json")
                if isinstance(payload, dict) and FIELD not in payload:
                    spare = self.capture.max_request_bytes - len(canonical(payload)) - 4096
                    budget = min(MAX_ENCODED, spare)
                    if self.execution_binding:
                        self.execution_records = self.capture.identities.call_bundle(
                            self.execution_binding[1]
                        )
                        if self.capture.execution_version == 4:
                            from .identity_facts import wire_record

                            self.execution_records = [
                                wire_record(r) for r in self.execution_records
                            ]
                    ids_bytes = (
                        len(
                            canonical(
                                make_carrier(
                                    self.ids_record,
                                    None,
                                    self.execution_records,
                                    execution_call_id=self.execution_binding[1]
                                    if self.execution_binding
                                    else None,
                                    execution_version=self.capture.execution_version,
                                )
                            )
                        )
                        if self.ids_record or self.execution_records
                        else 0
                    )
                    self.batch = self.capture.transport.prepare(
                        self.capture.destination, budget - ids_bytes
                    )
                    headers[CALL] = self.call_id
                    envelope = (
                        make_carrier(
                            self.ids_record,
                            self.batch,
                            self.execution_records,
                            execution_call_id=self.execution_binding[1]
                            if self.execution_binding
                            else None,
                            execution_version=self.capture.execution_version,
                        )
                        if (self.ids_record or self.execution_records) and ids_bytes <= budget
                        else self.batch
                    )
                    if envelope and len(canonical(envelope)) <= budget:
                        kwargs["json"] = {**payload, FIELD: envelope}
                        self.ids_sent = envelope.get("version") in {2, 3, 4}
                        self.execution_sent = envelope.get("version") in {3, 4}
                    kwargs["headers"] = headers
        except Exception:
            self.capture.fail()

    def response(self, response):
        if not self.capture:
            return
        from . import identity_runtime

        identity_runtime.call_received(self.execution_binding, response.status_code)
        try:
            self.body_complete = response.is_stream_consumed
            if self.execution_sent and response.headers.get(EXECUTION_ACK) == digest(
                canonical(self.execution_records)
            ):
                with self.capture.journal.transaction():
                    self.capture.journal.db.executemany(
                        "UPDATE records SET acknowledged=1 WHERE id=?",
                        [(row["id"],) for row in self.execution_records],
                    )
            if self.batch:
                self.capture.transport.confirm(response.headers, batch_id=self.batch["batch_id"])
            if (
                self.ids_sent
                and self.ids_record
                and response.headers.get(IDS_ACK)
                == (self.ids_record["id"] + ":" + digest(canonical(self.ids_record)))
            ):
                with self.capture.journal.transaction():
                    self.capture.journal.db.execute(
                        "UPDATE records SET acknowledged=1 WHERE id=?",
                        (self.ids_record["id"],),
                    )
            received = self.capture.emit(
                "llm.headers",
                {
                    "status_code": response.status_code,
                    "content_encoding": ""
                    if response.is_stream_consumed
                    else response.headers.get("content-encoding", ""),
                },
                identity=self.identity,
                links=self._response_links(),
            )
            self._note_receive(received)
        except Exception:
            self.capture.fail()

    def chunk(self, chunk: bytes):
        if self.capture:
            # Retain exact receive bytes; HTTP chunks need not align with SSE frames.
            received = self.capture.emit(
                "llm.chunk",
                {"ordinal": self.ordinal, "data": base64.b64encode(chunk).decode("ascii")},
                identity=self.identity,
                links=self._response_links(),
            )
            self._note_receive(received)
            self.ordinal += 1

    def finish(self, error=None):
        from . import identity_runtime

        identity_runtime.call_end(self.execution_binding, error, self.body_complete)
        try:
            from .id_capture import call_finished

            call_finished(self.ids_record, self.id_state)
        except Exception:
            if self.capture:
                self.capture.fail()
        if self.capture:
            ended = self.capture.emit(
                "llm.end",
                {
                    "status": "error"
                    if error
                    else "completed"
                    if self.body_complete
                    else "interrupted",
                    "body_complete": self.body_complete,
                    "error_type": type(error).__name__ if error else None,
                    "chunks": self.ordinal,
                },
                identity=self.identity,
                links=self._response_links(),
            )
            from .causality import note_physical_end

            note_physical_end(ended, self.identity)
            self._note_receive(ended)
            try:
                if self.batch:
                    self.capture.transport.release(self.batch["batch_id"])
                retention = self.capture.retain_acknowledged_seconds
                if retention is not None and (self.batch or self.execution_sent):
                    # Local maintenance only, after this request's receipts were applied.
                    import time

                    self.capture.transport.prune_acknowledged(time.time() - retention)
            except Exception:
                self.capture.fail()
        if self.execution_provider_binding:
            identity_runtime.provider_exit(self.execution_provider_binding)
        if self.token is not None:
            try:
                context.reset(self.token)
            except ValueError:
                # Coordinator heartbeat helpers may pull successive chunks in
                # different Tasks. Events use the frozen identity above.
                pass
        if self.operation_binding:
            token, created = self.operation_binding
            if created:
                self.capture.id_runs.pop(created.run_id, None)
            try:
                context.reset(token)
            except ValueError:
                pass

    def _response_links(self):
        return (
            [{"event_id": self.request_event, "relation": "physical_response"}]
            if self.request_event
            else []
        )

    def _note_receive(self, event):
        from .causality import note_physical_receive

        note_physical_receive(event, self.identity)


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream, call):
        self.stream, self.call = stream, call

    async def __aiter__(self):
        async for chunk in self.stream:
            self.call.chunk(chunk)
            yield chunk
        self.call.body_complete = True

    async def aclose(self):
        await self.stream.aclose()


@contextlib.asynccontextmanager
async def stream_llm(client, method, url, *, correlation=None, **kwargs):
    call = _Call(url, kwargs, correlation, allow_piggyback=_eligible_client(client, kwargs))
    error = None
    try:
        async with client.stream(method, url, **kwargs) as response:
            call.response(response)
            if call.capture and isinstance(response, httpx.Response):
                if response.is_stream_consumed:
                    call.chunk(response.content)
                else:
                    response.stream = _ObservedStream(response.stream, call)
            yield response
    except BaseException as exc:
        error = exc
        raise
    finally:
        call.finish(error)


async def post_llm(client, url, *, correlation=None, auxiliary=False, **kwargs):
    call = _Call(
        url,
        kwargs,
        correlation,
        allow_piggyback=_eligible_client(client, kwargs),
        auxiliary=auxiliary,
    )
    error = None
    try:
        response = await client.post(url, **kwargs)
        call.response(response)
        if call.capture and isinstance(response, httpx.Response):
            call.chunk(response.content)
        return response
    except BaseException as exc:
        error = exc
        raise
    finally:
        call.finish(error)


def _network_observability_disabled():
    """Honor both the TracePoint switch and the upstream privacy switch that
    suppresses the original correlation headers; the JSON copy never bypasses it."""
    from os import environ

    truthy = {"1", "true", "yes", "on"}
    return (
        environ.get("OPENSQUILLA_NETWORK_OBSERVABILITY_DISABLED", "").strip().lower() in truthy
        or environ.get("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", "").strip().lower()
        in truthy
    )


def _wire_messages(business):
    """Message DTOs for the user/assistant entries of an OpenAI-style payload."""
    if not isinstance(business, dict):
        return []
    from opensquilla.provider.types import Message

    result = []
    for entry in business.get("messages") or []:
        if not isinstance(entry, dict) or entry.get("role") not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        try:
            result.append(Message(role=entry["role"], content=content))
        except Exception:
            continue
    return result


def _eligible_client(client, kwargs):
    # Preserve business redirect/auth behavior, but do not let it move an attached batch
    # beyond the destination or credential checked at the explicit send boundary.
    return not (
        getattr(client, "follow_redirects", False)
        or kwargs.get("follow_redirects", False)
        or getattr(client, "auth", None)
        or kwargs.get("auth")
    )
