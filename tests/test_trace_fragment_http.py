"""Actual Agent + OpenAI HTTP provider + ASGI platform over loopback TCP."""

import asyncio
import hashlib
import json
import socket

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from opensquilla.engine import Agent, AgentConfig
from opensquilla.observability.piggyback.capture import Capture, _active, context
from opensquilla.observability.piggyback.platform import PlatformIngest, TraceMiddleware
from opensquilla.observability.piggyback.protocol import FIELD, canonical, digest
from opensquilla.observability.piggyback.transport import Destination
from opensquilla.provider.openai import OpenAIProvider


async def _exercise_fragment_delivery(tmp_path):
    class BudgetStore(PlatformIngest):
        large_rejected = 0

        def accept(self, auth_context, batch):
            if len(canonical(batch)) > 8192:
                self.large_rejected += 1
                raise OSError("injected first large-batch storage failure")
            return super().accept(auth_context, batch)

    store = BudgetStore(tmp_path / "api.db")
    business = []

    async def endpoint(request: Request):
        payload = await request.json()
        assert FIELD not in payload
        business.append(payload)
        # The predetermined business workload is 32 independent arithmetic jobs.
        value = int(payload["model"].rsplit("-", 1)[1])
        answer = str(value * value)
        # Simulated model prefill before response headers. The ingestion
        # middleware must not wait for its own writes to manufacture an ACK.
        await asyncio.sleep(0.01)

        async def stream():
            for delta, finish in [({"role": "assistant", "content": answer}, None), ({}, "stop")]:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "mock",
                            "object": "chat.completion.chunk",
                            "model": "mock",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        }
                    )
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    middleware = TraceMiddleware(
        Starlette(routes=[Route("/v1/chat/completions", endpoint, methods=["POST"])]),
        store,
        lambda scope: "tenant",
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(middleware, log_level="error"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    cap = Capture(
        tmp_path / "client", Destination(url, "tenant", digest(b"test")), execution_version=4
    )
    data = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(2048))
    content = cap.journal.content.put(data)
    original_prepare = cap.transport.prepare
    budgets = []

    def prepare(destination, budget):
        effective = budget if not budgets else min(budget, 8192)
        batch = original_prepare(destination, effective)
        budgets.append(len(canonical(batch)) if batch else 0)
        return batch

    cap.transport.prepare = prepare
    token = context.set({})
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        answers = []
        for capture in (cap, None):
            active = _active.set(capture)
            try:
                group = []
                for value in range(32):
                    provider = OpenAIProvider(
                        api_key="test",
                        model=f"mock-{value}",
                        base_url=url,
                        provider_kind="openai",
                        provider_id="mock",
                    )
                    agent = Agent(
                        provider=provider,
                        config=AgentConfig(max_iterations=1),
                        session_key=f"arithmetic-{value}",
                    )
                    events = [e async for e in agent.run_turn(str(value))]
                    group.append([e.text for e in events if e.kind == "done"])
                answers.append(group)
            finally:
                _active.reset(active)
        await middleware.drain()
        assert answers[0] == answers[1] == [[str(i * i)] for i in range(32)]
        assert len(business) == 64  # no flush requests or telemetry-only calls
        assert business[:32] == business[32:]
        assert budgets[0] > 8192 and all(size <= 8192 for size in budgets[1:])
        assert store.large_rejected >= 1
        originals = [
            r for r in store.records("tenant", cap.journal.source_id) if r["id"] == content + ":0"
        ]
        assert len(originals) == 1  # restored exclusively from subsequent small fragments
        assert cap.journal.db.execute(
            "SELECT acknowledged FROM records WHERE id=?", (content + ":0",)
        ).fetchone()[0]
        assert not cap.failed_runs
    finally:
        context.reset(token)
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        await middleware.drain()
        cap.journal.close()
        store.close()
        sock.close()


@pytest.mark.asyncio
async def test_real_agent_fragment_delivery_does_not_add_business_calls(tmp_path):
    async with asyncio.timeout(30):
        await _exercise_fragment_delivery(tmp_path)
