"""Closing at a terminal event must release the real provider's inner streams."""

from types import SimpleNamespace

import pytest

from opensquilla.provider import ChatConfig, DoneEvent, Message
from opensquilla.provider.openai import OpenAIProvider


def test_continuation_prefix_ignores_trace_metadata_but_preserves_business_differences():
    from opensquilla.provider.ensemble import EnsembleProvider
    from opensquilla.provider.types import MessageTraceIds

    original = Message(role="user", content="task")
    current = original.model_copy(deep=True)
    current.trace_ids = MessageTraceIds(message_id="observed")
    following = Message(role="assistant", content="answer")
    assert EnsembleProvider._continuation_messages([current, following], [original]) == [
        current,
        following,
    ]
    changed = Message(role="user", content="different task")
    assert EnsembleProvider._continuation_messages([changed], [original]) == [original, changed]


@pytest.mark.parametrize("accounted", [False, True])
async def test_accounting_closes_inner_iterator_before_returning_from_aclose(
    monkeypatch, accounted
):
    import opensquilla.engine.usage_accounting as usage

    closed = []
    finalized = []

    async def stream():
        try:
            yield DoneEvent(stop_reason="stop")
            raise AssertionError("cleanup must not pull an extra event")
        finally:
            closed.append(True)

    async def start(*args, **kwargs):
        return SimpleNamespace(provider="mock", model="model")

    async def finish(*args, **kwargs):
        finalized.append(True)

    monkeypatch.setattr(
        usage, "current_usage_accounting_scope", lambda: object() if accounted else None
    )
    monkeypatch.setattr(usage, "start_usage_call", start)
    monkeypatch.setattr(usage, "finalize_usage_call", finish)
    outer = usage.account_provider_stream(stream, provider="mock", model="model")
    assert (await anext(outer)).kind == "done"
    assert not closed
    await outer.aclose()
    assert closed == [True]
    assert len(finalized) == int(accounted)


async def test_openai_detached_cancellation_wrapper_closes_transport_iterator(monkeypatch):
    closed = []

    async def inner(*args):
        try:
            yield DoneEvent(stop_reason="stop")
            raise AssertionError("cleanup must not pull an extra event")
        finally:
            closed.append(True)

    provider = OpenAIProvider(api_key="mock", base_url="http://127.0.0.1:1")
    monkeypatch.setattr(provider, "_stream", inner)
    outer = provider._stream_with_detached_cancellation(
        [Message(role="user", content="test")], None, ChatConfig()
    )
    assert (await anext(outer)).kind == "done"
    await outer.aclose()
    assert closed == [True]
