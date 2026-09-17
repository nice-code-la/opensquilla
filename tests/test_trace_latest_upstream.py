"""TracePoint must preserve native replay state and runtime identity spans."""

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.observability.piggyback import identity_runtime as runtime
from opensquilla.provider.execution_identity import (
    project_execution_identity,
    render_execution_identity,
    with_execution_identity,
)
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockText,
    ExecutionIdentity,
    Message,
    MessageTraceIds,
    ProviderReplayState,
)
from tests.test_trace_correctness_revision import cap as cap
from tests.test_trace_correctness_revision import run


@pytest.mark.parametrize("blocks", [False, True])
def test_runtime_context_preserves_native_execution_span_and_trace_sources(cap, blocks):
    identity = ExecutionIdentity(kind="single_model", provider="openai", model="first")
    replacement = ExecutionIdentity(kind="single_model", provider="openai", model="fallback")
    with run(cap):
        user = Message(
            role="user",
            content=[ContentBlockText(text="user input")] if blocks else "user input",
        )
        context = runtime.context_message(
            with_execution_identity(Message(role="user", content="runtime context"), identity)
        )
        combined = Agent._append_runtime_context_to_user_message(user, context)
        message = cap.identities.get(combined.trace_ids.execution_message_id)
        transform = cap.identities.get(message["source_ids"][0])
        assert transform["input_ids"] == [
            user.trace_ids.execution_message_id,
            context.trace_ids.execution_message_id,
        ]
        projected = project_execution_identity(
            [combined], ChatConfig(execution_identity=replacement)
        )[0]
        native = projected.content[-1] if blocks else projected
        text = native.text if blocks else native.content
        start, stop = native.execution_identity_span
        assert text[start:stop] == render_execution_identity(replacement)
        assert "user input" in str(projected.content)
        assert combined.trace_ids == projected.trace_ids
        assert "trace_ids" not in projected.business_dump()


@pytest.mark.parametrize("annotated", [False, True])
def test_trace_serialization_retains_exact_native_provider_replay(annotated):
    replay = ProviderReplayState(
        protocol="openai_chat", source="endpoint", model="model",
        native_reasoning_content="native continuation",
        reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque"}],
    )
    message = Message(role="assistant", content="answer", provider_replay=replay)
    baseline = message.model_dump(mode="json", exclude_none=True)
    if annotated:
        message.trace_ids = MessageTraceIds(message_id="M1", source_call_ids=("C1",))
    assert message.business_dump() == baseline
    restored = Message.model_validate_json(message.model_dump_json())
    assert restored.provider_replay == replay
    assert restored.trace_ids == message.trace_ids
