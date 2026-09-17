"""Produce direct Call IDs at execution boundaries, without traversing event logs."""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from dataclasses import asdict, dataclass, field

from .id_graph import call_key, validate_call_ids
from .protocol import canonical, digest


def uid():
    return uuid.uuid4().hex


@dataclass
class RunIds:
    run_id: str
    session_id: str
    user_turn_id: str | None
    previous_user_turn_id: str | None
    parent_run_id: str | None
    parent_call_id: str | None
    run_path_ids: tuple[str, ...] = ()
    latest_call_id: str | None = None
    iteration_id: str | None = None
    previous_iteration_id: str | None = None
    continuation_id: str | None = None
    logical_id: str = field(default_factory=uid)
    attempt_id: str | None = None
    latest_by_operation: dict[str, str] = field(default_factory=dict)
    retry_of: str | None = None
    input_calls: tuple[str, ...] = ()
    input_joins: tuple[str, ...] = ()
    tool_calls: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tool_joins: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tool_waits: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tool_unknown: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tool_unknown_messages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    input_turns: tuple[str, ...] = ()
    input_waits: tuple[str, ...] = ()
    input_unknown: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    phase_id: str = field(default_factory=uid)
    auxiliary_phase_id: str | None = None
    system_activation_id: str | None = None
    dispatch_id: str | None = None
    history_ids: list[dict] = field(default_factory=list)
    run_declarations: list[dict] = field(default_factory=list)
    unknown_message_ids: tuple[str, ...] = ()
    result_ids: dict | None = None


def _current():
    from .capture import context, get_capture

    return get_capture(), context.get(), context.get().get("_id_state")


def operation_context(messages=(), *, force_auxiliary=False):
    """Bind a provider invocation; auxiliary work never mutates the parent's cursor."""
    from .capture import context

    cap, ctx, prior = _current()
    if not cap or not cap.current_configuration_allows_capture():
        return None
    created = (
        prior is None
        or bool(ctx.get("tool_execution_id"))
        or prior.iteration_id is None
        or force_auxiliary
    )
    state = prior
    if created:
        run_id = ctx.get("run_id") if prior is None and ctx.get("run_id") else uid()
        state = RunIds(
            run_id=run_id,
            session_id=prior.session_id
            if prior
            else (ctx.get("session_id") or digest(("standalone:" + run_id).encode())),
            user_turn_id=prior.user_turn_id if prior else None,
            previous_user_turn_id=prior.previous_user_turn_id if prior else None,
            parent_run_id=prior.run_id if prior else None,
            parent_call_id=prior.latest_call_id if prior else None,
            run_path_ids=(*(prior.run_path_ids if prior else ()), run_id),
            attempt_id=uid(),
            system_activation_id=None if prior and prior.user_turn_id else uid(),
        )
        state.auxiliary_phase_id = state.phase_id
        state.run_declarations = [
            *(prior.run_declarations if prior else ()),
            run_declaration(state),
        ]
        state.input_calls = tuple(sorted(source_ids(messages)))
        state.input_joins = tuple(sorted(source_ids(messages, "_trace_join_ids")))
        state.input_turns = tuple(sorted(source_ids(messages, "_trace_turn_ids")))
        cap.id_runs[state.run_id] = state
    token = context.set(
        {
            **ctx,
            "_id_state": state,
            "_id_provider_scope": True,
            "run_id": state.run_id,
            "session_id": state.session_id,
            "trace_id": ctx.get("trace_id") or state.run_id,
        }
    )
    return token, state if created else None


def provider_scope(fn):
    """Keep one operation across a provider's internal HTTP fallback attempts."""

    @functools.wraps(fn)
    def wrapped(self, messages, *args, **kwargs):
        from .capture import context, get_capture
        from .causality import _ContextStream

        stream = fn(self, messages, *args, **kwargs)
        if not get_capture():
            return stream

        async def observe():
            binding = None
            execution_binding = None
            try:
                try:
                    config = kwargs.get("config", args[1] if len(args) > 1 else None)
                    kind = getattr(
                        getattr(config, "provider_request_correlation", None), "call_kind", ""
                    )
                    binding = operation_context(
                        messages, force_auxiliary=kind.startswith("auxiliary.")
                    )
                    from . import identity_runtime

                    execution_binding = identity_runtime.provider_enter(
                        messages,
                        auxiliary=kind.startswith("auxiliary.")
                        or bool(context.get().get("tool_execution_id"))
                        and not context.get().get("_ensemble_round"),
                    )
                except Exception:
                    get_capture().fail()
                async for event in stream:
                    if getattr(event, "kind", None) in {"done", "error"}:
                        from . import identity_runtime

                        identity_runtime.provider_result_ready(success=event.kind == "done")
                    yield event
            finally:
                close = getattr(stream, "aclose", None)
                if close:
                    await close()
                if execution_binding:
                    from . import identity_runtime

                    identity_runtime.provider_exit(execution_binding)
                if binding:
                    token, created = binding
                    if created:
                        get_capture().id_runs.pop(created.run_id, None)
                    context.reset(token)

        return _ContextStream(observe())

    return wrapped


def freeze_dispatch():
    cap, ctx, state = _current()
    if not cap or not state:
        return None
    from . import identity_runtime

    _, _, execution = identity_runtime.current()
    dispatch = uid()
    return {
        "source_id": cap.journal.source_id,
        "dispatch_id": dispatch,
        "execution_scope": json.loads(
            json.dumps(asdict(execution), default=lambda value: sorted(value))
        )
        if execution
        else None,
        "execution_dispatch_id": identity_runtime.dispatch_start(child_id=dispatch),
        "scope": {
            name: ctx[name]
            for name in (
                "run_id",
                "session_id",
                "turn_id",
                "user_turn_id",
                "trace_id",
            )
            if ctx.get(name) is not None
        },
        "parent": json.loads(json.dumps(asdict(state))),
    }


def task_scope(fn):
    """Restore durable dispatch identity at execution, independent of queue ContextVars."""

    @functools.wraps(fn)
    async def wrapped(self, task, *args, **kwargs):
        from .capture import context, get_capture

        cap = get_capture()
        frozen = getattr(task, "trace_dispatch_scope", None)
        parent = {}
        if frozen and cap and frozen.get("source_id") == cap.journal.source_id:
            try:
                prior = RunIds(**frozen["parent"])
                prior.run_path_ids = tuple(prior.run_path_ids)
                parent = {
                    **frozen["scope"],
                    "_id_state": prior,
                    "_id_spawn_call": prior.latest_call_id,
                    "_id_dispatch_id": frozen["dispatch_id"],
                    "_scheduled_run_id": task.task_id,
                }
                if frozen.get("execution_scope"):
                    from .identity_runtime import Scope

                    parent["_execution_ids"] = Scope(**frozen["execution_scope"])
                    parent["_execution_dispatch"] = frozen.get("execution_dispatch_id")
            except (KeyError, TypeError, ValueError):
                cap.fail()
        native_inputs = getattr(task, "persisted_user_message_ids", None)
        if native_inputs:
            parent["_execution_input_ids"] = list(native_inputs)
        token = context.set(parent)
        try:
            return await fn(self, task, *args, **kwargs)
        finally:
            context.reset(token)

    return wrapped


def start_run(capture, identity, parent):
    """Turn order is committed locally; transport never allocates session identity."""
    if not capture.current_configuration_allows_capture():
        return None
    prior = parent.get("_id_state")
    previous = prior.previous_user_turn_id if prior else None
    if not parent.get("run_id") and identity.get("user_turn_id"):
        journal = capture.journal
        key = "id_turn_head:" + digest(str(identity["session_id"]).encode())
        turn_key = "id_turn:" + identity["user_turn_id"]
        with journal.transaction():
            existing = journal.db.execute(
                "SELECT value FROM meta WHERE key=?", (turn_key,)
            ).fetchone()
            if existing:
                previous = json.loads(existing[0])["previous_user_turn_id"]
            else:
                head = journal.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                previous = head[0] if head else None
                journal.db.execute(
                    "INSERT INTO meta VALUES(?,?)",
                    (
                        turn_key,
                        json.dumps(
                            {
                                "user_turn_id": identity["user_turn_id"],
                                "session_id": identity["session_id"],
                                "previous_user_turn_id": previous,
                            }
                        ),
                    ),
                )
                journal.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES(?,?)", (key, identity["user_turn_id"])
                )
            if getattr(capture, "record_scope", "full") == "full":
                turn_record = {
                    "id": "turn_ids:" + identity["user_turn_id"],
                    "type": "turn_ids",
                    "value": {
                        "version": 1,
                        "source_id": journal.source_id,
                        "session_id": identity["session_id"],
                        "user_turn_id": identity["user_turn_id"],
                        "previous_user_turn_id": previous,
                    },
                }
                journal._capacity(len(canonical(turn_record)) * 2)
                journal._insert(turn_record, identity["run_id"])
    state = RunIds(
        run_id=identity["run_id"],
        session_id=identity["session_id"],
        user_turn_id=identity.get("user_turn_id"),
        previous_user_turn_id=previous,
        parent_run_id=identity.get("parent_run_id"),
        parent_call_id=parent.get("_id_spawn_call", prior.latest_call_id if prior else None),
        run_path_ids=(*prior.run_path_ids, identity["run_id"]) if prior else (identity["run_id"],),
        system_activation_id=None if identity.get("user_turn_id") else uid(),
        dispatch_id=parent.get("_id_dispatch_id"),
    )
    capture.id_runs[state.run_id] = state
    state.run_declarations = [
        *(prior.run_declarations if prior else ()),
        run_declaration(state),
    ]
    return state


def run_declaration(state):
    return {
        "run_id": state.run_id,
        "parent_run_id": state.parent_run_id,
        "session_id": state.session_id,
        "user_turn_id": state.user_turn_id,
    }


def iteration_started(iteration_id):
    _, _, state = _current()
    if state:
        state.previous_iteration_id = state.iteration_id
        state.iteration_id = iteration_id
        state.continuation_id = state.latest_call_id
        state.retry_of = None
        state.result_ids = None
        state.tool_calls.clear()
        state.tool_joins.clear()
        state.tool_waits.clear()
        state.tool_unknown.clear()
        state.tool_unknown_messages.clear()


def source_ids(value, attr="_trace_call_ids"):
    """Read attached references, never search by text equality or content hashes."""
    refs = set(getattr(value, attr, ()))
    fields = {
        "_trace_call_ids": "source_call_ids",
        "_trace_join_ids": "adopted_child_call_ids",
        "_trace_turn_ids": "input_turn_ids",
        "_trace_wait_ids": "waited_call_ids",
        "_trace_unknown_ids": "unknown_call_ids",
        "_trace_unknown_message_ids": "unknown_message_ids",
    }
    provenance = getattr(value, "trace_ids", None)
    if provenance is not None:
        refs.update(getattr(provenance, fields[attr], ()))
    if isinstance(value, (list, tuple)):
        for item in value:
            refs.update(source_ids(item, attr))
    elif isinstance(value, dict):
        for item in value.values():
            refs.update(source_ids(item, attr))
    return refs


def attempt_started(messages):
    _, ctx, state = _current()
    if state:
        from opensquilla.provider.types import MessageTraceIds

        for message in messages:
            if hasattr(message, "trace_ids") and message.trace_ids is None:
                message_id = uid()
                message.trace_ids = MessageTraceIds(
                    message_id=message_id,
                    unknown_message_ids=(message_id,),
                )
        operation = ctx.get("logical_call_id") or uid()
        state.logical_id = operation
        state.attempt_id = ctx.get("outer_attempt_id")
        state.retry_of = state.latest_by_operation.get(operation)
        state.input_calls = tuple(sorted(source_ids(messages)))
        state.input_joins = tuple(sorted(source_ids(messages, "_trace_join_ids")))
        state.input_turns = tuple(sorted(source_ids(messages, "_trace_turn_ids")))
        state.input_waits = tuple(sorted(source_ids(messages, "_trace_wait_ids")))
        state.input_unknown = tuple(sorted(source_ids(messages, "_trace_unknown_ids")))
        state.message_ids = tuple(
            m.trace_ids.message_id for m in messages if getattr(m, "trace_ids", None)
        )
        state.unknown_message_ids = tuple(
            sorted(source_ids(messages, "_trace_unknown_message_ids"))
        )


def _turn_declarations(cap, state):
    """Bounded ID closure; absent older declarations remain explicit forward references."""
    pending = list(
        dict.fromkeys(
            filter(
                None,
                (
                    state.user_turn_id,
                    state.previous_user_turn_id,
                    *state.input_turns,
                ),
            )
        )
    )
    declarations = {}
    with cap.journal.lock:
        while pending and len(declarations) < 128:
            turn = pending.pop(0)
            if turn in declarations:
                continue
            row = cap.journal.db.execute(
                "SELECT value FROM meta WHERE key=?",
                ("id_turn:" + turn,),
            ).fetchone()
            if row:
                item = {**json.loads(row[0]), "user_turn_id": turn}
                declarations[turn] = item
                if item.get("previous_user_turn_id"):
                    pending.append(item["previous_user_turn_id"])
    return [declarations[key] for key in sorted(declarations)]


def mark_retry():
    """Called by a Provider's actual internal fallback branch."""
    cap, _, state = _current()
    if cap and state:
        state.retry_of = state.latest_by_operation.get(state.logical_id)
    from .identity_runtime import mark_fallback

    mark_fallback()


def new_call(call_id):
    cap, ctx, state = _current()
    if not cap or not state or not cap.current_configuration_allows_capture():
        return None
    if getattr(cap, "record_scope", "full") != "full":
        return None  # legacy v2 call_ids are redundant with the v3/v4 declarations
    # Parallel model fusion has its own protocol and is outside this profile.
    if ctx.get("ensemble_round_id") or ctx.get("_ensemble_round"):
        return None
    value = {
        "version": 2,
        "source_id": cap.journal.source_id,
        "session_id": state.session_id,
        "user_turn_id": state.user_turn_id,
        "previous_user_turn_id": state.previous_user_turn_id,
        "trace_id": ctx.get("trace_id") or state.run_id,
        "run_id": state.run_id,
        "parent_run_id": state.parent_run_id,
        "iteration_id": state.iteration_id,
        "previous_iteration_id": state.previous_iteration_id,
        "logical_call_id": state.logical_id,
        "outer_attempt_id": state.attempt_id or state.logical_id,
        "call_id": call_id,
        "context_id": uid(),
        "previous_call_id": state.retry_of or state.continuation_id,
        "parent_call_id": state.parent_call_id,
        "retry_of_call_id": state.retry_of,
        "join_call_ids": list(state.input_joins),
        "context_parent_call_ids": list(state.input_calls),
        "input_turn_ids": list(state.input_turns),
        "run_path_ids": list(state.run_path_ids),
        "phase_id": state.phase_id,
        "auxiliary_phase_id": state.auxiliary_phase_id,
        "system_activation_id": state.system_activation_id,
        "dispatch_id": state.dispatch_id,
        "waited_call_ids": list(state.input_waits),
        "unknown_context_call_ids": list(state.input_unknown),
        "context_message_ids": list(state.message_ids),
        "turn_declarations": _turn_declarations(cap, state),
        "run_declarations": state.run_declarations,
        "unknown_message_ids": list(state.unknown_message_ids),
    }
    validate_call_ids(value)
    record = {"id": "call_ids:" + call_id, "type": "call_ids", "value": value}
    with cap.journal.transaction():
        cap.journal._capacity(len(canonical(record)) * 2)
        cap.journal._insert(record, state.run_id)
    return record


def call_finished(ids, call_state=None):
    cap, _, state = _current()
    if ids and cap:
        value = ids["value"]
        state = call_state or cap.id_runs.get(value["run_id"])
        if state:
            key = call_key(value["source_id"], value["call_id"])
            state.latest_call_id = key
            state.latest_by_operation[value["logical_call_id"]] = key


class ResultWithIds(str):
    """Internal str-compatible result; references are never serialized into prose."""


def transform_result(value, transform):
    """Explicit adapter for a transformation that uses its input value.

    There is no text matching or implicit attribution of arbitrary Python strings.
    """
    result = ResultWithIds(transform(str(value)))
    result.trace_ids = result_provenance(value)
    return result


def result_provenance(value=None):
    """Explicit source declaration; None intentionally declares an independent result."""
    from opensquilla.provider.types import MessageTraceIds

    from .identity_runtime import result_sources

    return MessageTraceIds(
        message_id=uid(),
        source_call_ids=tuple(sorted(source_ids(value))),
        adopted_child_call_ids=tuple(sorted(source_ids(value, "_trace_join_ids"))),
        input_turn_ids=tuple(sorted(source_ids(value, "_trace_turn_ids"))),
        waited_call_ids=tuple(sorted(source_ids(value, "_trace_wait_ids"))),
        unknown_call_ids=tuple(sorted(source_ids(value, "_trace_unknown_ids"))),
        unknown_message_ids=tuple(sorted(source_ids(value, "_trace_unknown_message_ids"))),
        execution_source_ids=tuple(sorted(result_sources(value))),
    )


def note_waited_result(value):
    from . import identity_runtime

    identity_runtime.waited(value)
    cap, ctx, state = _current()
    tool_id = ctx.get("tool_call_id")
    if cap and state and tool_id:
        state.tool_waits[tool_id] = tuple(
            sorted(
                set(state.tool_waits.get(tool_id, ()))
                | source_ids(value)
                | source_ids(value, "_trace_wait_ids")
            )
        )


async def await_child_results(awaitable):
    """Receive a gathered/wrapped result in the caller's scope, preserving wait IDs."""
    value = await awaitable
    try:
        note_waited_result(value)
    except Exception:
        from .capture import get_capture

        if cap := get_capture():
            cap.fail()
    return value


class ObservedChildTask(asyncio.Task):
    """Record actual await receipt in the caller's scope, not child completion."""

    def __await__(self):
        value = yield from super().__await__()
        try:
            note_waited_result(value)
        except Exception:
            from .capture import get_capture

            if cap := get_capture():
                cap.fail()
        return value


def finish_run(capture, run_id):
    state = capture.id_runs.pop(run_id, None)
    if state and state.history_ids:
        parent = capture.id_runs.get(state.parent_run_id)
        if parent and parent.iteration_id is None:
            # The enclosing runtime persists this Agent's transcript. A parent
            # Agent loop must not inherit its subagent's history as its own.
            parent.history_ids = list(state.history_ids)
    if state and (state.latest_call_id or state.result_ids):
        with capture.journal.transaction():
            capture.journal._capacity(512)
            capture.journal.db.execute(
                "INSERT OR REPLACE INTO meta VALUES(?,?)",
                (
                    "id_run_result:" + run_id,
                    json.dumps(
                        {
                            "trace_ids": state.result_ids,
                            "last_call_id": state.latest_call_id,
                        }
                    ),
                ),
            )


def child_result(text, run_id):
    cap, _, _ = _current()
    state = cap.id_runs.get(run_id) if cap else None
    if not cap:
        return text
    snapshot = (
        {"trace_ids": state.result_ids, "last_call_id": state.latest_call_id} if state else None
    )
    if snapshot is None:
        with cap.journal.lock:
            row = cap.journal.db.execute(
                "SELECT value FROM meta WHERE key=?", ("id_run_result:" + run_id,)
            ).fetchone()
            if row:
                try:
                    snapshot = json.loads(row[0])
                except ValueError:
                    snapshot = {"trace_ids": None, "last_call_id": row[0]}
    from opensquilla.provider.types import MessageTraceIds

    snapshot = snapshot or {}
    message_id = uid()
    provenance = (
        MessageTraceIds.model_validate(snapshot["trace_ids"])
        if snapshot.get("trace_ids")
        else MessageTraceIds(
            message_id=message_id,
            unknown_message_ids=(message_id,),
        )
    )
    provenance = provenance.model_copy(
        update={
            "adopted_child_call_ids": provenance.source_call_ids,
            "waited_call_ids": (snapshot["last_call_id"],) if snapshot.get("last_call_id") else (),
        }
    )
    value = ResultWithIds(text)
    value.trace_ids = provenance
    return value


def tool_returned(tool_id, result):
    _, _, state = _current()
    if state and tool_id:
        content = getattr(result, "content", result)
        explicit = getattr(result, "trace_ids", None)
        if explicit is not None:
            from opensquilla.provider.types import MessageTraceIds

            if isinstance(explicit, dict):
                explicit = MessageTraceIds.model_validate(explicit)
            calls, joins = set(explicit.source_call_ids), set(explicit.adopted_child_call_ids)
        else:
            calls, joins = source_ids(content), source_ids(content, "_trace_join_ids")
        state.tool_unknown_messages[tool_id] = (
            tuple(explicit.unknown_message_ids)
            if explicit is not None
            else tuple(sorted(source_ids(content, "_trace_unknown_message_ids")))
        )
        state.tool_calls[tool_id] = tuple(sorted(calls))
        state.tool_joins[tool_id] = tuple(sorted(joins))
        state.tool_unknown[tool_id] = (
            tuple(sorted(set(state.tool_waits.get(tool_id, ())) - calls))
            if explicit is None
            else tuple(explicit.unknown_call_ids)
        )


def messages_committed(messages, phase):
    _, _, state = _current()
    if not state:
        return
    for message in messages:
        calls, joins, waits, unknown = set(), set(), set(), set()
        no_physical_source = False
        unknown_messages = set()
        if phase == "assistant_accepted_into_loop":
            accepted = state.latest_by_operation.get(state.logical_id)
            if accepted:
                calls.add(accepted)
            else:
                no_physical_source = True
        elif phase == "tool_results_committed":
            for block in getattr(message, "content", ()):
                tool_id = getattr(block, "tool_use_id", None)
                calls.update(state.tool_calls.get(tool_id, ()))
                joins.update(state.tool_joins.get(tool_id, ()))
                waits.update(state.tool_waits.get(tool_id, ()))
                unknown.update(state.tool_unknown.get(tool_id, ()))
                unknown_messages.update(state.tool_unknown_messages.get(tool_id, ()))
        elif phase == "user_input":
            pass
        else:
            continue
        from opensquilla.provider.types import MessageTraceIds

        message_id = uid()
        message.trace_ids = MessageTraceIds(
            message_id=message_id,
            source_call_ids=tuple(sorted(calls)),
            adopted_child_call_ids=tuple(sorted(joins)),
            input_turn_ids=(state.user_turn_id,)
            if phase == "user_input" and state.user_turn_id
            else (),
            waited_call_ids=tuple(sorted(waits)),
            unknown_call_ids=tuple(sorted(unknown)),
            unknown_message_ids=(message_id,)
            if no_physical_source
            else tuple(sorted(unknown_messages)),
        )
        if phase == "assistant_accepted_into_loop":
            state.result_ids = message.trace_ids.model_dump(mode="json")
        object.__setattr__(message, "_trace_call_ids", tuple(sorted(calls)))
        object.__setattr__(message, "_trace_join_ids", tuple(sorted(joins)))
        if phase in {"assistant_accepted_into_loop", "tool_results_committed"}:
            state.history_ids.append(
                {
                    "shape": message_shape(message),
                    "trace_ids": message.trace_ids.model_dump(mode="json"),
                }
            )


def message_shape(message):
    """Structural transcript correspondence, never a text/hash source lookup."""
    tool_ids = []
    if isinstance(message.content, list):
        for block in message.content:
            if block.type == "tool_use":
                tool_ids.append(["tool_use", block.id])
            elif block.type == "tool_result":
                tool_ids.append(["tool_result", block.tool_use_id])
    return [message.role, tool_ids]


def persist_trace_context(role, turn_context):
    """Attach provenance before the native transcript transaction commits."""
    cap, _, state = _current()
    if not cap or not state or not cap.current_configuration_allows_capture():
        return turn_context
    result = dict(turn_context or {})
    if role == "user" and state.user_turn_id:
        from opensquilla.provider.types import MessageTraceIds

        result["trace_user_ids"] = MessageTraceIds(
            message_id=uid(),
            input_turn_ids=(state.user_turn_id,),
        ).model_dump(mode="json")
    elif role == "assistant" and state.history_ids:
        result["trace_history_ids"] = json.loads(json.dumps(state.history_ids))
    return result


def restore_history_ids(fn):
    """Restore alongside the native transcript projection, including folded tool turns."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from opensquilla.provider.types import MessageTraceIds

        messages = fn(*args, **kwargs)
        metadata = kwargs.get("turn_context") or {}
        try:
            role = args[0] if args else kwargs.get("role")
            if role == "user" and metadata.get("trace_user_ids") and len(messages) == 1:
                messages[0].trace_ids = MessageTraceIds.model_validate(metadata["trace_user_ids"])
            elif rows := metadata.get("trace_history_ids"):
                parsed = [MessageTraceIds.model_validate(row["trace_ids"]) for row in rows]
                shapes_match = len(rows) == len(messages) and all(
                    row["shape"] == message_shape(message) for row, message in zip(rows, messages)
                )
                if shapes_match:
                    for message, ids in zip(messages, parsed):
                        message.trace_ids = ids
                else:
                    # Projection changed segmentation; no positional guessing.
                    unknown = tuple(sorted({ref for ids in parsed for ref in ids.source_call_ids}))
                    for message in messages:
                        message.trace_ids = MessageTraceIds(
                            message_id=uid(), unknown_call_ids=unknown
                        )
        except (KeyError, TypeError, ValueError):
            from .capture import get_capture

            if cap := get_capture():
                cap.fail()
        return messages

    return wrapped
