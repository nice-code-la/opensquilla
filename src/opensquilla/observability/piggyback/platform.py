"""Authenticated ASGI request extraction and durable, idempotent reference ingestion."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .protocol import (
    ACK,
    CALL,
    EXECUTION_ACK,
    FIELD,
    IDS_ACK,
    REJECT,
    ProtocolError,
    canonical,
    decode,
    digest,
    identifier,
    split_carrier,
)
from .sqlite_init import initialize


class PlatformIngest:
    """SQLite reference backend. accept() returns only after FULL-sync transaction commit."""

    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.lock = threading.RLock()
        initialize(
            self.db,
            Path(path),
            """
            CREATE TABLE IF NOT EXISTS batches(
                tenant TEXT, source TEXT, id TEXT, sha TEXT, envelope BLOB, received REAL,
                PRIMARY KEY(tenant,source,id));
            CREATE TABLE IF NOT EXISTS records(
                tenant TEXT, source TEXT, id TEXT, type TEXT, body BLOB,
                PRIMARY KEY(tenant,source,id));
            CREATE TABLE IF NOT EXISTS observations(
                tenant TEXT, call_id TEXT, kind TEXT, body BLOB, observed REAL);
            CREATE TABLE IF NOT EXISTS identity_conflicts(
                tenant TEXT, source TEXT, id TEXT, sha TEXT, body BLOB,
                PRIMARY KEY(tenant,source,id,sha));
            CREATE INDEX IF NOT EXISTS records_call_lookup ON records(
                tenant, json_extract(CAST(body AS TEXT), '$.value.context.execution_id'))
                WHERE type='event';
            CREATE INDEX IF NOT EXISTS record_fragments ON records(
                tenant, source, json_extract(CAST(body AS TEXT), '$.value.record_sha256'))
                WHERE type='record_fragment';
        """,
        )

    def accept(self, auth_context: str, batch: dict) -> str:
        tenant = identifier(auth_context)
        records = decode(batch)
        evidence_records = list(records)
        source, batch_id, sha = batch["source_id"], batch["batch_id"], batch["sha256"]
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute(
                    "SELECT sha FROM batches WHERE tenant=? AND source=? AND id=?",
                    (tenant, source, batch_id),
                ).fetchone()
                if old and old[0] != sha:
                    raise ProtocolError("batch_identity_conflict")
                if not old:
                    for record in records:
                        pending = [record]
                        if record["type"] == "record_fragment":
                            from .fragments import assemble

                            pieces = [
                                json.loads(row[0])
                                for row in self.db.execute(
                                    "SELECT body FROM records WHERE tenant=? AND source=? "
                                    "AND type='record_fragment' AND "
                                    "json_extract(CAST(body AS TEXT),'$.value.record_sha256')=?",
                                    (tenant, source, record["value"]["record_sha256"]),
                                )
                            ]
                            restored = assemble([*pieces, record], source)
                            if restored is not None:
                                pending.append(restored)
                                evidence_records.append(restored)
                        for item in pending:
                            self._insert_record(tenant, source, item)
                    self.db.execute(
                        "INSERT INTO batches VALUES(?,?,?,?,?,?)",
                        (tenant, source, batch_id, sha, canonical(batch), time.time()),
                    )
                self.db.execute("COMMIT")
            except BaseException as exc:
                self.db.execute("ROLLBACK")
                if isinstance(exc, ProtocolError) and str(exc) in {
                    "record_identity_conflict",
                    "batch_identity_conflict",
                }:
                    # A rejected variant is still evidence. Persist separately from the
                    # failed batch so future ID-only queries cannot silently select a winner.
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        for record in evidence_records:
                            if record["type"] in {
                                "call_ids",
                                "turn_ids",
                                "id_node",
                                "id_fact",
                                "call_correlation",
                            }:
                                body = canonical(record)
                                self.db.execute(
                                    "INSERT OR IGNORE INTO identity_conflicts VALUES(?,?,?,?,?)",
                                    (tenant, source, record["id"], digest(body), body),
                                )
                        self.db.execute("COMMIT")
                    except BaseException:
                        self.db.execute("ROLLBACK")
                        raise
                raise
        return batch_id

    def _insert_record(self, tenant, source, record):
        body = canonical(record)
        existing = self.db.execute(
            "SELECT body FROM records WHERE tenant=? AND source=? AND id=?",
            (tenant, source, record["id"]),
        ).fetchone()
        if existing and bytes(existing[0]) != body:
            from .identity_facts import equivalent_records

            if not equivalent_records(json.loads(existing[0]), record):
                raise ProtocolError("record_identity_conflict")
        self.db.execute(
            "INSERT OR IGNORE INTO records VALUES(?,?,?,?,?)",
            (tenant, source, record["id"], record["type"], body),
        )

    def records(self, tenant: str, source: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM records WHERE tenant=? AND source=? ORDER BY id",
                (identifier(tenant), identifier(source)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def sources(self, tenant: str) -> list[str]:
        with self.lock:
            return [
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT source FROM batches WHERE tenant=? ORDER BY source",
                    (identifier(tenant),),
                )
            ]

    def call_sources(self, tenant: str, call_id: str) -> list[str]:
        with self.lock:
            return [
                row[0]
                for row in self.db.execute(
                    "SELECT DISTINCT source FROM records WHERE type='event' AND tenant=? "
                    "AND json_extract(CAST(body AS TEXT), '$.value.context.execution_id')=? "
                    "AND json_extract(CAST(body AS TEXT), '$.value.kind') LIKE 'llm.%' "
                    "ORDER BY source",
                    (identifier(tenant), identifier(call_id)),
                )
            ]

    def execution_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        """Read only ID declarations and conflict variants; never events or bodies."""
        with self.lock:
            params = (identifier(tenant), identifier(source)) if source else (identifier(tenant),)
            condition = " AND source=?" if source else ""
            rows = self.db.execute(
                "SELECT body FROM records WHERE tenant=? AND type IN ('id_node','id_fact')"
                + condition
                + " ORDER BY source,id",
                params,
            ).fetchall()
            rows += self.db.execute(
                "SELECT body FROM identity_conflicts WHERE tenant=? AND "
                "json_extract(CAST(body AS TEXT), '$.type') IN ('id_node','id_fact')"
                + condition
                + " ORDER BY source,id,sha",
                params,
            ).fetchall()
        return [json.loads(bytes(row[0]))["value"] for row in rows]

    def call_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        """An ID-only query deliberately never reads event or content records."""
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM records WHERE tenant=? AND type='call_ids'"
                + (" AND source=?" if source else "")
                + " ORDER BY source,id",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
            rows += self.db.execute(
                "SELECT body FROM identity_conflicts WHERE tenant=? AND id LIKE 'call_ids:%'"
                + (" AND source=?" if source else "")
                + " ORDER BY source,id,sha",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
        return [json.loads(row[0])["value"] for row in rows]

    def turn_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM records WHERE tenant=? AND type='turn_ids'"
                + (" AND source=?" if source else "")
                + " ORDER BY source,id",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
            rows += self.db.execute(
                "SELECT body FROM identity_conflicts WHERE tenant=? AND id LIKE 'turn_ids:%'"
                + (" AND source=?" if source else "")
                + " ORDER BY source,id,sha",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
        return [json.loads(row[0])["value"] for row in rows]

    def correlation_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        """Original upstream correlation values bound to Calls, plus conflict variants.

        A separate view: the ID graph never depends on these records.
        """
        with self.lock:
            params = (identifier(tenant), identifier(source)) if source else (identifier(tenant),)
            condition = " AND source=?" if source else ""
            rows = self.db.execute(
                "SELECT body FROM records WHERE tenant=? AND type='call_correlation'"
                + condition
                + " ORDER BY source,id",
                params,
            ).fetchall()
            rows += self.db.execute(
                "SELECT body FROM identity_conflicts WHERE tenant=? "
                "AND id LIKE 'call_correlation:%'" + condition + " ORDER BY source,id,sha",
                params,
            ).fetchall()
        return [json.loads(bytes(row[0]))["value"] for row in rows]

    def observe(self, tenant: str, call_id: str, kind: str, value: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?)",
                (tenant, call_id, kind, canonical(value), time.time()),
            )

    def observations(self, tenant: str, call_ids: list[str]) -> list[dict]:
        result = []
        with self.lock:
            for offset in range(0, len(call_ids), 500):
                chunk = call_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.db.execute(
                    f"SELECT call_id,kind,body FROM observations WHERE tenant=? "
                    f"AND call_id IN ({placeholders}) ORDER BY observed",
                    (tenant, *chunk),
                )
                result.extend(
                    {"call_id": row[0], "kind": row[1], "payload": json.loads(row[2])}
                    for row in rows
                )
        return result

    def close(self):
        with self.lock:
            self.db.close()


class TraceMiddleware:
    """Mount after trusted authentication, before provider validation/cache/forwarding.

    authenticate(scope) must return a trusted tenant ID or None; it must not use
    tenant claims in the telemetry envelope. It may be synchronous or asynchronous.
    No telemetry endpoint is installed by this middleware.
    """

    def __init__(
        self,
        app,
        store,
        authenticate,
        *,
        max_request_bytes=16 * 1024**2,
        paths=(
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/responses/compact",
            "/codex/responses",
            "/backend-api/codex/responses",
            "/v1/messages",
            "/api/chat",
        ),
    ):
        self.app, self.store, self.authenticate = app, store, authenticate
        self.max_request_bytes, self.paths = max_request_bytes, frozenset(paths)
        self.tasks: set[asyncio.Task] = set()
        self.receipts: OrderedDict[tuple, asyncio.Task] = OrderedDict()

    def _background(self, fn, *args):
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        self.tasks.add(task)

        def done(finished):
            self.tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # retrieve failures without leaking trace contents into logs

        task.add_done_callback(done)
        return task

    def _ingest(self, tenant, batch):
        key = (tenant, batch.get("source_id"), batch.get("batch_id"), digest(canonical(batch)))
        previous = self.receipts.get(key)
        if previous is not None:
            if not previous.done() or (
                not previous.cancelled()
                and (
                    previous.exception() is None or isinstance(previous.exception(), ProtocolError)
                )
            ):
                self.receipts.move_to_end(key)
                return previous
            self.receipts.pop(key)
        # Backpressure leaves the batch in the client outbox without creating a model retry.
        if len(self.receipts) >= 4096:
            removable = next((k for k, t in self.receipts.items() if t.done()), None)
            if removable is None:
                return None
            self.receipts.pop(removable)
        task = self._background(self.store.accept, tenant, batch)
        self.receipts[key] = task
        return task

    async def drain(self):
        """Server-only graceful shutdown. This does not cause client uploads."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.paths:
            return await self.app(scope, receive, send)
        tenant = self.authenticate(scope)
        if inspect.isawaitable(tenant):
            tenant = await tenant
        if not tenant:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            return await send({"type": "http.response.body", "body": b"Unauthorized"})
        tenant = identifier(tenant)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                return await send({"type": "http.response.body", "body": b"Request too large"})
            if not message.get("more_body", False):
                break
        task, batch_id, ids_task, ids_receipt = None, None, None, None
        execution_receipt = None
        try:
            business = json.loads(body)
        except (ValueError, UnicodeError):
            business = None
        if isinstance(business, dict) and FIELD in business:
            batch = business.pop(FIELD)
            body = bytearray(canonical(business))
            try:
                if isinstance(batch, dict) and batch.get("version") in {2, 3, 4}:
                    carrier = batch
                    current, batch = split_carrier(carrier)
                    ids_task = self._ingest(tenant, current)
                    ids_receipt = (
                        (
                            "call_ids:"
                            + carrier["call_ids"]["call_id"]
                            + ":"
                            + carrier["call_ids_sha256"]
                        )
                        if carrier.get("call_ids")
                        else None
                    )
                    execution_receipt = carrier.get("execution_ids_sha256")
                if batch is not None:
                    batch_id = identifier(batch.get("batch_id"))
                    task = self._ingest(tenant, batch)
            except (ProtocolError, AttributeError, TypeError, ValueError):
                pass  # malformed extension cannot break an otherwise valid business request
        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.lower() not in {b"content-length", CALL.encode()}
        ]
        headers.append((b"content-length", str(len(body)).encode()))
        call_id = (
            dict(scope.get("headers", [])).get(CALL.encode(), b"").decode("ascii", errors="ignore")
        )
        observe = getattr(self.store, "observe", None)
        if observe and call_id:
            # Keep the original upstream correlation headers (when the client sent
            # them) next to the inbound request, so they can be cross-checked
            # against the call_correlation record without reading any body.
            original_headers = {
                k.decode("ascii", errors="ignore"): v.decode("ascii", errors="ignore")
                for k, v in scope.get("headers", [])
                if k.lower()
                in {
                    b"x-opensquilla-install-id",
                    b"x-opensquilla-session-id",
                    b"x-opensquilla-turn-id",
                    b"x-opensquilla-execution-id",
                    b"x-opensquilla-call-kind",
                }
            }
            self._background(
                observe,
                tenant,
                call_id,
                "server.request",
                {"request": business, "original_headers": original_headers},
            )
        child = {
            **scope,
            "headers": headers,
            "trace_platform": {"tenant": tenant, "call_id": call_id},
        }
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        response_ordinal = 0

        async def wrapped_send(message):
            nonlocal response_ordinal
            if message["type"] == "http.response.start":
                new_headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower()
                    not in {ACK.encode(), REJECT.encode(), IDS_ACK.encode(), EXECUTION_ACK.encode()}
                ]
                if ids_task is not None and ids_task.done() and not ids_task.cancelled():
                    try:
                        ids_task.result()
                        if ids_receipt:
                            new_headers.append((IDS_ACK.encode(), ids_receipt.encode("ascii")))
                        if execution_receipt:
                            new_headers.append(
                                (EXECUTION_ACK.encode(), execution_receipt.encode("ascii"))
                            )
                    except Exception:
                        pass
                if task is not None and task.done() and not task.cancelled():
                    try:
                        receipt = task.result()
                        new_headers.append((ACK.encode(), receipt.encode("ascii")))
                    except ProtocolError as exc:
                        new_headers.append((REJECT.encode(), f"{batch_id}:{exc}".encode("ascii")))
                    except Exception:
                        pass  # no durable acceptance, no ACK
                message = {**message, "headers": new_headers}
                if observe and call_id:
                    self._background(
                        observe, tenant, call_id, "server.headers", {"status": message["status"]}
                    )
            elif message["type"] == "http.response.body" and observe and call_id:
                import base64

                self._background(
                    observe,
                    tenant,
                    call_id,
                    "server.response",
                    {
                        "data": base64.b64encode(message.get("body", b"")).decode(),
                        "ordinal": response_ordinal,
                        "more_body": message.get("more_body", False),
                        "client_received": "unknown",
                    },
                )
                response_ordinal += 1
            await send(message)

        return await self.app(child, replay_receive, wrapped_send)


def token_authenticator(token_hash_to_tenant: dict[str, str]):
    """Reference auth configuration contains credential hashes, never plaintext API keys."""

    def authenticate(scope):
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        value = headers.get(b"authorization", b"")
        credential = (
            value[7:] if value.lower().startswith(b"bearer ") else headers.get(b"x-api-key", b"")
        )
        return token_hash_to_tenant.get(digest(credential)) if credential else None

    return authenticate
