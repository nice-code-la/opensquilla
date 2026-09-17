"""Reference API platform with authenticated trace queries and transparent LLM forwarding."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .environment import RecordedContentStore
from .export import html_report
from .id_graph import IdTraceReconstructor, call_key
from .platform import PlatformIngest, TraceMiddleware, token_authenticator
from .protocol import ProtocolError, canonical, digest
from .reconstruct import TraceReconstructor


def create_platform(config: dict):
    auth = token_authenticator(config["token_hash_to_tenant"])
    if config.get("backend", "sqlite") == "postgres-s3":
        import boto3

        from .postgres import PostgresS3Ingest

        store = PostgresS3Ingest(
            os.environ[config["dsn_env"]],
            boto3.client("s3", endpoint_url=config.get("s3_endpoint")),
            config["bucket"],
        )
    else:
        store = PlatformIngest(config["sqlite_path"])
    upstream = config["upstream_base_url"].rstrip("/")
    parsed = urlsplit(upstream)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_upstream")
    client = httpx.AsyncClient(
        timeout=config.get("timeout_seconds", 120), trust_env=False, follow_redirects=False
    )

    def tenant(request):
        return auth(request.scope)

    async def query_execution_ids(request: Request):
        import asyncio

        from .identity import CONSTANTS, parts, references
        from .identity_graph import ExecutionIdReconstructor

        owner = tenant(request)
        if not owner:
            return JSONResponse({"error": "unauthorized"}, 401)
        source = request.path_params.get("source")
        selected = request.path_params.get("call_id")
        try:
            if selected:
                source, typ, _ = parts(selected)
                if typ != "call":
                    raise ProtocolError("identity_kind")
            rows = await asyncio.to_thread(store.execution_identities, owner, source)
            if source:
                needed = {
                    parts(target)[0]
                    for row in rows
                    for _, target in references(row)
                    if target not in CONSTANTS
                } - {source}
                if needed:
                    # Resolve other producer namespaces within this authenticated
                    # tenant, without reading events or captured content.
                    all_rows = await asyncio.to_thread(store.execution_identities, owner)
                    by_source = {}
                    for row in all_rows:
                        by_source.setdefault(parts(row["id"])[0], []).append(row)
                    visited = {source}
                    while needed:
                        current = needed.pop()
                        if current in visited:
                            continue
                        visited.add(current)
                        extra = by_source.get(current, [])
                        rows.extend(extra)
                        needed.update(
                            parts(target)[0]
                            for row in extra
                            for _, target in references(row)
                            if target not in CONSTANTS and parts(target)[0] not in visited
                        )
            graph = await asyncio.to_thread(ExecutionIdReconstructor().reconstruct, rows)
            reader = getattr(store, "correlation_identities", None)
            if reader:
                # Separate view joining the original upstream correlation values to
                # Calls. Conflicting variants for one Call are reported, never chosen.
                original = {}
                for value in await asyncio.to_thread(reader, owner, source):
                    entry = {"original": value["original"], "field_status": value["field_status"]}
                    current = original.get(value["call_id"])
                    if current is None:
                        original[value["call_id"]] = entry
                    elif current != entry and current.get("conflict") is not True:
                        original[value["call_id"]] = {"conflict": True, "variants": [current, entry]}
                    elif current.get("conflict") is True and entry not in current["variants"]:
                        current["variants"].append(entry)
                graph["original_correlation"] = original
        except ProtocolError:
            return JSONResponse({"error": "invalid_identity"}, 400)
        if selected:
            if selected in graph["conflicting_ids"]:
                return JSONResponse({"error": "conflicting_call_id", "graph": graph}, 409)
            if selected not in graph["positions"]:
                return JSONResponse({"error": "call_ids_not_received"}, 404)
            graph["selected_call_id"] = selected
        return JSONResponse(graph)

    async def query_ids(request: Request):
        import asyncio

        owner = tenant(request)
        if not owner:
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            identities = await asyncio.to_thread(
                store.call_identities, owner, request.path_params.get("source")
            )
        except ProtocolError:
            return JSONResponse({"error": "invalid_source_id"}, 400)
        requested = request.path_params.get("call_id")
        selected_source = request.path_params.get("source")
        selected_session = request.query_params.get("session_id")
        if requested:
            found = [ids for ids in identities if ids["call_id"] == requested]
            if not found:
                return JSONResponse({"error": "call_ids_not_received"}, 404)
            if len({canonical(ids) for ids in found}) != 1:
                return JSONResponse({"error": "ambiguous_call_id"}, 409)
            selected = found[0]
            selected_session = selected["session_id"]
            selected_source = selected["source_id"]
            identities = [row for row in identities if row["source_id"] == selected_source]
        if selected_session:
            seeds = [row for row in identities if row["session_id"] == selected_session]
            traces = {(row["source_id"], row["trace_id"]) for row in seeds}
            identities = [
                row
                for row in identities
                if (
                    row["session_id"] == selected_session
                    or (row["source_id"], row["trace_id"]) in traces
                )
            ]
        turn_reader = getattr(store, "turn_identities", None)
        if turn_reader:
            turn_ids = await asyncio.to_thread(turn_reader, owner, selected_source)
            if selected_session:
                sessions = {row["session_id"] for row in identities} | {selected_session}
                turn_ids = [row for row in turn_ids if row["session_id"] in sessions]
            identities.extend(turn_ids)
        graph = await asyncio.to_thread(IdTraceReconstructor().reconstruct, identities)
        if requested:
            graph["selected_call_id"] = call_key(selected["source_id"], selected["call_id"])
        return JSONResponse(graph)

    async def query(request: Request):
        owner = tenant(request)
        if not owner:
            return JSONResponse({"error": "unauthorized"}, 401)
        source = request.path_params.get("source")
        call_id = request.path_params.get("call_id")
        import asyncio

        if call_id and not source:
            try:
                sources = await asyncio.to_thread(store.call_sources, owner, call_id)
            except ProtocolError:
                return JSONResponse({"error": "invalid_call_id"}, 400)
            if not sources:
                return JSONResponse({"error": "call_not_received"}, 404)
            if len(sources) != 1:
                return JSONResponse({"error": "ambiguous_call_id"}, 409)
            source = sources[0]
        if not source:
            return JSONResponse({"sources": await asyncio.to_thread(store.sources, owner)})
        records = await asyncio.to_thread(store.records, owner, source)
        if request.path_params.get("ref"):
            try:
                data = await asyncio.to_thread(
                    RecordedContentStore(records).get, request.path_params["ref"]
                )
            except FileNotFoundError:
                return JSONResponse({"error": "content_not_received"}, 404)
            except ValueError:
                return JSONResponse({"error": "content_incomplete_or_invalid"}, 409)
            return Response(data, media_type="application/octet-stream")
        bundle = await asyncio.to_thread(
            TraceReconstructor().reconstruct, records, provenance="platform"
        )
        if call_id:
            entry = bundle["call_index"].get(call_id)
            if entry is None:
                return JSONResponse({"error": "call_not_received"}, 404)
            if entry["position_status"] == "ambiguous":
                return JSONResponse(
                    {"error": "ambiguous_call_id", "reasons": entry["reasons"]}, 409
                )
            return JSONResponse(
                {
                    "source_id": source,
                    "revision": bundle["revision"],
                    "provenance": "platform",
                    **entry,
                    "model_context": bundle["model_contexts"][call_id],
                    "input_context": bundle["input_contexts"].get(entry["input_context_id"]),
                    "user_turn": bundle["user_turns"].get(
                        (entry["location"] or {}).get("user_turn_id")
                    ),
                }
            )
        if turn_id := request.path_params.get("user_turn_id"):
            turn = bundle["user_turns"].get(turn_id)
            if turn and any(r.startswith("conflicting_") for r in turn["reasons"]):
                return JSONResponse({"error": "ambiguous_user_turn_id"}, 409)
            return JSONResponse(
                turn if turn else {"error": "user_turn_not_received"}, 200 if turn else 404
            )
        bundle["server_observations"] = await asyncio.to_thread(
            store.observations, owner, list(bundle["model_contexts"])
        )
        # Keep the client evidence revision stable, with a separate revision for the
        # combined query view as independently observed server data arrives.
        bundle["server_observations"].sort(key=canonical)
        bundle["view_revision"] = digest(canonical(bundle))
        return (
            HTMLResponse(html_report(bundle))
            if request.url.path.endswith("/report")
            else JSONResponse(bundle)
        )

    async def forward(request: Request):
        # Authentication and extension removal have already happened in TraceMiddleware.
        body = await request.body()
        headers = {
            name: request.headers[name]
            for name in ("content-type", "accept", "anthropic-version", "anthropic-beta")
            if name in request.headers
        }
        api_key = os.environ[config["upstream_key_env"]] if config.get("upstream_key_env") else ""
        if api_key:
            header = config.get("upstream_auth_header", "authorization")
            headers[header] = "Bearer " + api_key if header.lower() == "authorization" else api_key
        upstream_request = client.build_request(
            "POST", upstream + request.url.path, headers=headers, content=body
        )
        response = await client.send(upstream_request, stream=True)

        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        response_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            not in {
                "connection",
                "transfer-encoding",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "upgrade",
            }
        }
        return StreamingResponse(
            chunks(), status_code=response.status_code, headers=response_headers
        )

    middleware = None

    @asynccontextmanager
    async def lifespan(app):
        yield
        await middleware.drain()
        await client.aclose()
        store.close()

    paths = (
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/responses/compact",
        "/codex/responses",
        "/backend-api/codex/responses",
        "/v1/messages",
        "/api/chat",
    )
    app = Starlette(
        routes=[Route(path, forward, methods=["POST"]) for path in paths]
        + [
            Route("/traces", query),
            Route("/id-traces", query_ids),
            Route("/execution-id-traces", query_execution_ids),
            Route("/execution-calls/{call_id}/ids", query_execution_ids),
            Route("/execution-traces/{source}/ids", query_execution_ids),
            Route("/calls/{call_id}/ids", query_ids),
            Route("/traces/{source}/ids", query_ids),
            Route("/calls/{call_id}", query),
            Route("/traces/{source}", query),
            Route("/traces/{source}/report", query),
            Route("/traces/{source}/content/{ref}", query),
            Route("/traces/{source}/calls/{call_id}", query),
            Route("/traces/{source}/turns/{user_turn_id}", query),
        ],
        lifespan=lifespan,
    )
    middleware = TraceMiddleware(
        app,
        store,
        auth,
        paths=paths,
        max_request_bytes=config.get("max_request_bytes", 16 * 1024**2),
    )
    return middleware


def from_environment():
    with open(os.environ["OPENSQUILLA_TRACE_SERVER_CONFIG"]) as file:
        return create_platform(json.load(file))
