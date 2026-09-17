"""Local causal identities at Agent and Ensemble coordinator boundaries.

No network I/O. Context dictionaries are copied between concurrent members;
only the enclosing round's registry is shared, never the current member identity.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import uuid
from dataclasses import dataclass, field

from .capture import context, get_capture, serializable


def _id():
    return uuid.uuid4().hex


class _ContextStream:
    """Keep one private Context across successive coordinator heartbeat Tasks."""

    def __init__(self, stream):
        self.stream = stream
        self.context = contextvars.copy_context()

    def __aiter__(self):
        return self

    async def __anext__(self):
        async def pull():
            return await self.stream.__anext__()

        return await asyncio.create_task(pull(), context=self.context)

    async def aclose(self):
        async def close():
            await self.stream.aclose()

        await asyncio.create_task(close(), context=self.context)


def _contextual(fn):
    @functools.wraps(fn)
    def start(*args, **kwargs):
        stream = fn(*args, **kwargs)
        if not get_capture():
            return stream
        bound = _ContextStream(stream)
        state = context.get().get("_ensemble_round")
        if state is not None and fn.__name__ == "_provider_stream_with_lifecycle":
            state.streams.append(bound)
        return bound

    return start


def _links(ids, relation):
    return [{"event_id": value, "relation": relation} for value in sorted({v for v in ids if v})]


def _emit(kind, payload=None, *, causes=(), relation="caused_by"):
    cap = get_capture()
    event = cap.emit(kind, payload, links=_links(causes, relation)) if cap else None
    return event["event_id"] if event else None


def _safe(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if not get_capture():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            get_capture().fail()
            return None

    return wrapped


@dataclass
class LoopState:
    iteration: str | None = None
    start: str | None = None
    attempts: list[str] = field(default_factory=list)
    attempt_start: str | None = None
    response: str | None = None
    checkpoints: list[str] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    pending: bool = False


@_safe
def iteration_start(index, messages):
    state = context.get().get("_loop_state")
    if state is None:
        state = LoopState()
        context.set({**context.get(), "_loop_state": state})
    previous = iteration_end("advanced") if state.iteration else None
    state.iteration, state.attempts = _id(), []
    from .id_capture import iteration_started

    iteration_started(state.iteration)
    from . import identity_runtime

    identity_runtime.iteration_start(state.iteration)
    state.attempt_start = state.response = None
    context.set(
        {
            **context.get(),
            "iteration_id": state.iteration,
            "iteration_index": index,
            "logical_call_id": _id(),
            "outer_attempt_id": None,
            "outer_attempt_index": None,
            "agent_call_id": None,
            "role": "agent",
            "_causes": [previous] if previous else [],
            "_physical_calls": [],
            "_physical_ends": [],
            "_logical_results": [],
        }
    )
    state.start = _emit(
        "loop.iteration.start",
        {
            "index": index,
            "causality_version": 1,
            "previous_iteration_end": previous,
            "input_messages": messages,
        },
        causes=[previous] if previous else [],
        relation="next_iteration",
    )
    state.checkpoints = [state.start] if state.start else []
    context.set({**context.get(), "_causes": state.checkpoints})


@_safe
def attempt_start(iteration, attempt, call_id, messages):
    state = context.get().get("_loop_state")
    if state is None:
        iteration_start(iteration, messages)
        state = context.get().get("_loop_state")
    if state is None:
        return
    if state.pending:
        attempt_end({"status": "interrupted_before_response"})
    previous = state.response
    attempt_id = _id()
    context.set(
        {
            **context.get(),
            "outer_attempt_id": attempt_id,
            "outer_attempt_index": attempt,
            "agent_call_id": call_id,
            "role": "agent",
            "_physical_calls": [],
            "_physical_ends": [],
            "_logical_results": [],
        }
    )
    state.attempts.append(attempt_id)
    from .id_capture import attempt_started

    attempt_started(messages)
    from . import identity_runtime

    identity_runtime.attempt_start(messages)
    state.response = None
    state.attempt_start = _emit(
        "loop.attempt.start",
        {
            "iteration": iteration,
            "attempt": attempt,
            "agent_call_id": call_id,
            "request_messages": messages,
            "previous_attempt_response": previous,
            "context_event_ids": state.checkpoints,
        },
        causes=[*state.checkpoints, *([previous] if previous else [])],
        relation="context_origin",
    )
    state.pending = True
    context.set({**context.get(), "_causes": [state.attempt_start] if state.attempt_start else []})


@_safe
def attempt_end(response):
    state = context.get().get("_loop_state")
    if state is None or not state.pending:
        return
    from . import identity_runtime

    identity_runtime.attempt_end(
        success=bool(response.get("got_done_event")), failure=bool(response.get("error"))
    )
    state.response = _emit(
        "loop.attempt.end",
        {
            "response": response,
            "physical_call_ids": list(context.get().get("_physical_calls", [])),
        },
        causes=[
            state.attempt_start,
            *context.get().get("_physical_ends", []),
            *context.get().get("_logical_results", []),
        ],
        relation="attempt_result",
    )
    state.pending = False
    context.set({**context.get(), "_causes": [state.response] if state.response else []})


@_safe
def state_commit(messages, phase):
    from .id_capture import messages_committed

    messages_committed(messages, phase)
    from . import identity_runtime

    identity_runtime.messages_committed(messages, phase)
    state = context.get().get("_loop_state")
    if state is None:
        return

    def tool_ids(value):
        if isinstance(value, dict):
            if value.get("type") == "tool_result" and value.get("tool_use_id"):
                yield value["tool_use_id"]
            for item in value.values():
                yield from tool_ids(item)
        elif isinstance(value, list):
            for item in value:
                yield from tool_ids(item)

    inputs = [state.tools[t] for t in tool_ids(serializable(messages)) if t in state.tools]
    event = _emit(
        "loop.state.committed",
        {
            "messages": messages,
            "phase": phase,
            "tool_result_event_ids": inputs,
            "source_attempt_response": state.response,
        },
        causes=[*inputs, *([state.response] if state.response else [])],
        relation="state_from",
    )
    if event:
        state.checkpoints.append(event)
        context.set({**context.get(), "_causes": [event]})


@_safe
def iteration_end(status):
    state = context.get().get("_loop_state")
    if state is None or not state.iteration:
        return None
    if state.pending:
        attempt_end({"status": "interrupted_before_response"})
    event = _emit(
        "loop.iteration.end",
        {
            "status": status,
            "attempt_ids": state.attempts,
            "checkpoint_event_ids": state.checkpoints,
        },
        causes=[*state.checkpoints, *([state.response] if state.response else [])],
        relation="iteration_closed",
    )
    state.iteration = None
    return event


def note_physical_call(call_id):
    for key in ("_physical_calls", "_member_physical_calls"):
        calls = context.get().get(key)
        if calls is not None:
            calls.append(call_id)


def note_physical_end(event, identity):
    if event:
        for key in ("_physical_ends", "_member_physical_ends"):
            ends = identity.get(key)
            if ends is not None:
                ends.append(event["event_id"])


def note_physical_receive(event, identity):
    latest = identity.get("_member_latest_receive")
    if event and latest is not None:
        latest[identity["execution_id"]] = event["event_id"]


def note_tool_result(tool_id, event):
    state = context.get().get("_loop_state")
    if state is not None and event:
        state.tools[tool_id] = event["event_id"]


@dataclass
class RoundState:
    id: str = field(default_factory=_id)
    start: str | None = None
    scheduled: dict[int, str] = field(default_factory=dict)
    candidates: dict[str, dict] = field(default_factory=dict)
    members: list[dict] = field(default_factory=list)
    selections: list[str] = field(default_factory=list)
    streams: list = field(default_factory=list)


@_safe
def schedule_candidates(members):
    state = context.get().get("_ensemble_round")
    if state is None:
        return
    index = 0
    for member in members:
        for sample in range(max(1, int(member.k or 1))):
            candidate_id = _id()
            state.scheduled[index] = candidate_id
            from .identity_runtime import schedule_candidate

            schedule_candidate(candidate_id, getattr(state, "execution_id", None))
            _emit(
                "ensemble.candidate.scheduled",
                {
                    "candidate_id": candidate_id,
                    "index": index,
                    "sample_index": sample,
                    "model": member.provider_config.model,
                    "provider": member.provider_config.provider,
                },
                causes=[state.start] if state.start else [],
                relation="dispatch",
            )
            index += 1


def _bound(fn, args, kwargs):
    return inspect.signature(fn).bind(*args, **kwargs).arguments


def trace_candidate(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        state = context.get().get("_ensemble_round")
        if state is None or not get_capture():
            return await fn(*args, **kwargs)
        values = _bound(fn, args, kwargs)
        candidate_id = state.scheduled.get(values["index"], _id())
        token = context.set(
            {
                **context.get(),
                "candidate_id": candidate_id,
                "candidate_index": values["index"],
                "sample_index": values["sample_index"],
                "logical_call_id": candidate_id,
                "role": "proposer",
            }
        )
        from . import identity_runtime

        execution_binding = identity_runtime.candidate_start(
            candidate_id, getattr(state, "execution_id", None)
        )
        start = _emit(
            "ensemble.candidate.start",
            {"index": values["index"], "sample_index": values["sample_index"]},
            causes=[state.start] if state.start else [],
        )
        context.set({**context.get(), "_causes": [start] if start else []})
        result, error = None, None
        try:
            result = await fn(*args, **kwargs)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            attempts = [m for m in state.members if m.get("candidate_id") == candidate_id]
            latest = attempts[-1] if attempts else {}
            end = _emit(
                "ensemble.candidate.end",
                {
                    "ok": bool(result is not None and result.ok),
                    "error": error or getattr(result, "error", ""),
                    "text": getattr(result, "text", ""),
                    "member_attempt_ids": [m["id"] for m in attempts],
                    "accepted_member_attempt_id": latest.get("id")
                    if result is not None and result.ok
                    else None,
                },
                causes=[start, *[m.get("end") for m in attempts]],
                relation="candidate_from",
            )
            origin = {
                "candidate_id": candidate_id,
                "result_event_id": end,
                "accepted_member_attempt_id": latest.get("id")
                if result is not None and result.ok
                else None,
            }
            state.candidates[candidate_id] = origin
            if result is not None:
                result.trace_origin = origin
            identity_runtime.candidate_end(
                execution_binding, result, cancelled=error == "CancelledError"
            )
            context.reset(token)

    return wrapped


def trace_round(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        if not get_capture():
            stream = fn(*args, **kwargs)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()
            return
        state = RoundState()
        token = context.set(
            {**context.get(), "ensemble_round_id": state.id, "_ensemble_round": state}
        )
        from . import identity_runtime

        state.execution_id = identity_runtime.round_start(state.id)
        state.start = _emit(
            "ensemble.round.start",
            {"causality_version": 1},
            causes=context.get().get("_causes", []),
        )
        context.set({**context.get(), "_causes": [state.start] if state.start else []})
        stream, terminal, error = fn(*args, **kwargs), None, None
        try:
            async for event in stream:
                if getattr(event, "kind", None) in {"done", "error"}:
                    terminal = event
                if getattr(event, "kind", None) == "provider_generation_reset":
                    _emit(
                        "ensemble.generation.reset",
                        {"event": event},
                        causes=[m.get("observation") for m in state.members],
                        relation="generation_replaced",
                    )
                yield event
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit) or terminal is None:
                error = type(exc).__name__
            raise
        finally:
            try:
                await stream.aclose()
            finally:
                # Some coordinators return immediately after DoneEvent. Close
                # retained member streams in their original Context before the
                # round manifest, rather than relying on async-generator GC.
                for member_stream in state.streams:
                    try:
                        await asyncio.wait_for(member_stream.aclose(), timeout=2)
                    except Exception:
                        get_capture().fail()
                trace = getattr(terminal, "ensemble_trace", None) or {}
                role = trace.get("final_request_role")
                accepted = next(
                    (
                        m
                        for m in reversed(state.members)
                        if m["role"] == role and m.get("terminal_kind") == "done"
                    ),
                    None,
                )
                end = _emit(
                    "ensemble.round.end",
                    {
                        "status": "completed"
                        if getattr(terminal, "kind", None) == "done" and not error
                        else "interrupted"
                        if error
                        else "error",
                        "error": error,
                        "candidate_ids": list(state.scheduled.values()),
                        "member_attempt_ids": [m["id"] for m in state.members],
                        "selection_event_ids": state.selections,
                        "accepted_member_attempt_id": accepted["id"] if accepted else None,
                        "final_role": role,
                        "fallback_used": trace.get("fallback_used", False),
                    },
                    causes=[state.start, *[m.get("end") for m in state.members], *state.selections],
                    relation="round_closed",
                )
                results = context.get().get("_logical_results")
                if end and results is not None:
                    results.append(end)
                context.reset(token)

    return _contextual(wrapped)


def trace_member_attempt(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        state = context.get().get("_ensemble_round")
        if state is None or not get_capture():
            stream = fn(*args, **kwargs)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()
            return
        values = _bound(fn, args, kwargs)
        role = values["role"].value
        role = "aggregator" if role == "primary_aggregator" else role
        logical = (
            context.get().get("candidate_id")
            if role == "proposer"
            else context.get().get("aggregation_id")
        )
        logical = logical or f"{state.id}:{role}:{values['logical_call_index']}"
        attempt = {
            "id": _id(),
            "role": role,
            "logical_call_id": logical,
            "candidate_id": context.get().get("candidate_id"),
        }
        previous = next(
            (m for m in reversed(state.members) if m["logical_call_id"] == logical), None
        )
        state.members.append(attempt)
        execution = values.get("execution_context")
        token = context.set(
            {
                **context.get(),
                "logical_call_id": logical,
                "role": role,
                "member_attempt_id": attempt["id"],
                "member_attempt_index": values["attempt_index"],
                "generation_epoch": getattr(execution, "generation_epoch", None),
                "_member_physical_calls": [],
                "_member_physical_ends": [],
                "_member_latest_receive": {},
            }
        )
        from . import identity_runtime

        identity_runtime.member_start(attempt["id"])
        start = _emit(
            "ensemble.member.start",
            {
                "logical_call_index": values["logical_call_index"],
                "attempt_index": values["attempt_index"],
                "owner": values["owner"],
                "previous_attempt_id": previous["id"] if previous else None,
            },
            causes=[
                *context.get().get("_causes", []),
                *([previous.get("observation") or previous.get("end")] if previous else []),
            ],
            relation="member_dependency",
        )
        context.set({**context.get(), "_causes": [start] if start else []})
        stream, error = fn(*args, **kwargs), None
        try:
            async for event in stream:
                if getattr(event, "kind", None) in {"done", "error"}:
                    attempt["terminal_kind"] = event.kind
                    identity_runtime.attempt_end(
                        success=event.kind == "done", failure=event.kind == "error"
                    )
                    attempt["observation"] = _emit(
                        "ensemble.member.observation",
                        {"event": event},
                        causes=[start, *context.get().get("_member_latest_receive", {}).values()],
                        relation="member_result",
                    )
                yield event
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit) or not attempt.get("terminal_kind"):
                error = type(exc).__name__
            raise
        finally:
            try:
                await stream.aclose()
            finally:
                if not attempt.get("terminal_kind"):
                    identity_runtime.attempt_end(failure=bool(error and error != "CancelledError"))
                attempt["end"] = _emit(
                    "ensemble.member.end",
                    {
                        "status": "interrupted"
                        if error
                        else attempt.get("terminal_kind", "incomplete"),
                        "error": error,
                        "physical_call_ids": context.get().get("_member_physical_calls", []),
                        "terminal_event_id": attempt.get("observation"),
                    },
                    causes=[
                        start,
                        attempt.get("observation"),
                        *context.get().get("_member_physical_ends", []),
                    ],
                    relation="member_closed",
                )
                context.reset(token)

    return _contextual(wrapped)


def trace_aggregation(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        state = context.get().get("_ensemble_round")
        if state is None or not get_capture():
            stream = fn(*args, **kwargs)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()
            return
        values = _bound(fn, args, kwargs)
        candidates = values.get("candidate_bundle", values.get("trace_candidates", ()))
        role = values.get("fixed_role", values.get("aggregator_role", "aggregator"))
        aggregation_id = _id()
        token = context.set({**context.get(), "aggregation_id": aggregation_id, "role": role})
        from . import identity_runtime

        execution_binding = identity_runtime.aggregation_start(
            aggregation_id,
            getattr(state, "execution_id", None),
            candidates,
            values.get("messages", values.get("fixed_messages", ())),
            role,
        )
        selected, refs, causes = [], [], []
        cap = get_capture()
        try:
            for position, candidate in enumerate(candidates):
                origin = candidate.trace_origin
                ref = (
                    cap.journal.content.put(candidate.text.encode())
                    if cap.record_scope == "full"
                    else None
                )
                if ref:
                    refs.append(ref)
                causes.append(origin.get("result_event_id"))
                selected.append(
                    {
                        **origin,
                        "index": candidate.index,
                        "sample_index": candidate.sample_index,
                        "position": position + 1,
                        "projected_content_ref": ref,
                    }
                )
            fallback = next((m for m in reversed(state.members) if m["role"] != "proposer"), None)
            if fallback and role.startswith("fixed"):
                causes.append(fallback.get("observation") or fallback.get("end"))
            selection = cap.emit(
                "ensemble.selection",
                {
                    "role": role,
                    "input_candidates": selected,
                    "messages": values.get("messages", values.get("fixed_messages")),
                    "fallback_from_attempt_id": fallback["id"]
                    if fallback and role.startswith("fixed")
                    else None,
                },
                blob_refs=refs,
                links=_links([state.start, *causes], "selected_input"),
            )
            if selection:
                state.selections.append(selection["event_id"])
                context.set(
                    {
                        **context.get(),
                        "selection_event_id": selection["event_id"],
                        "_causes": [selection["event_id"]],
                    }
                )
        except Exception:
            cap.fail()
        stream = fn(*args, **kwargs)
        execution_accepted = False
        try:
            async for event in stream:
                if getattr(event, "kind", None) in {"done", "error"}:
                    execution_accepted = event.kind == "done"
                    if execution_accepted:
                        identity_runtime.aggregation_result_ready(execution_binding)
                yield event
        finally:
            try:
                await stream.aclose()
            finally:
                identity_runtime.aggregation_end(execution_binding, execution_accepted)
                context.reset(token)

    return _contextual(wrapped)
