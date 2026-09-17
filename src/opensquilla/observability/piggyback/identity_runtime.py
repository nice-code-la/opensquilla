"""Execution-boundary producers for ID schema v3; never consumes the event journal."""

from __future__ import annotations

import functools
import json
import uuid
from dataclasses import dataclass, field, replace

from .identity import AUXILIARY, LOOP, kind, parts
from .protocol import canonical, digest


@dataclass
class Scope:
    run: str
    phase: str
    lane: str
    activation: str
    turn: str | None = None
    iteration: str | None = None
    operation: str | None = None
    attempt: str | None = None
    previous: str | None = None
    result: str | None = None
    message_ids: list[str] = field(default_factory=list)
    actions: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    cancelled_tools: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    ensemble: str | None = None
    selection: str | None = None
    generation: str | None = None
    waits: dict = field(default_factory=dict)
    input_turns: list[str] = field(default_factory=list)
    fallback: str | None = None
    fallback_attempt: str | None = None
    provider_parent: Scope | None = field(default=None, repr=False)
    provider_outcome: str | None = None
    # Published by a dispatched child Run when it closes, so a caller-level
    # provider fallback (gateway ProviderSelector) can name the Attempt it
    # takes over from even though that Attempt lives in another Run.
    last_child_call: str | None = None
    last_child_attempt: str | None = None


@dataclass(frozen=True)
class CallSnapshot:
    """Identity and inputs cannot change while the HTTP operation is suspended."""

    run: str
    attempt: str
    operation: str
    generation: str | None
    message_ids: tuple[str, ...]
    owner: Scope = field(repr=False, compare=False)


def _publish_cursor(s):
    parent = s.provider_parent
    if parent and (parent.lane, parent.operation, parent.attempt) == (
        s.lane,
        s.operation,
        s.attempt,
    ):
        parent.previous, parent.result, parent.fallback = s.previous, s.result, s.fallback
        _publish_cursor(parent)


def current():
    from .capture import context, get_capture

    cap = get_capture()
    return cap, cap.identities if cap else None, context.get().get("_execution_ids")


def safe(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from .capture import get_capture

        cap = get_capture()
        if not cap or not cap.current_configuration_allows_capture():
            return None
        try:
            # Serialize local identity boundary updates, not business execution.
            with cap.journal.lock:
                return fn(*args, **kwargs)
        except Exception:
            cap.fail()
            return None

    return wrapped


def _ensure_turn(r, turn, session):
    authority = r.put("authority", key=r.namespace)
    session_ref = r.put("session", key=session, authority_id=authority)
    existing_branch = r.get(r.id("branch", session))
    branch = (
        existing_branch["id"]
        if existing_branch
        else r.put("branch", key=session, session_id=session_ref, fork_id=None)
    )
    known = r.resolve_turn(branch, turn)
    if known:
        return known["id"]
    with r.journal.lock:
        row = r.journal.db.execute(
            "SELECT value FROM meta WHERE key=?", ("id_turn:" + turn,)
        ).fetchone()
    meta = json.loads(row[0]) if row else {}
    previous = meta.get("previous_user_turn_id")
    if previous:
        _ensure_turn(r, previous, meta.get("session_id", session))
    return r.accept_local_turn(branch, turn)


@safe
def start_run(identity, parent):
    cap, r, _ = current()
    prior = parent.get("_execution_ids")
    run = r.id("run", identity["run_id"])
    r.open(run)
    authority = r.put("authority", key=r.namespace)
    session_key = identity.get("_execution_session_id") or identity["session_id"]
    if prior and not identity.get("_execution_session_id"):
        session_key = (
            identity.get("_execution_session_key", session_key)
            if parent.get("_execution_dispatch")
            else parts(r.get(prior.run)["branch_id"])[2]
        )
    session = r.put("session", key=session_key, authority_id=authority)
    existing_branch = r.get(r.id("branch", session_key))
    branch = (
        existing_branch["id"]
        if existing_branch
        else r.put("branch", key=session_key, session_id=session, fork_id=None)
    )
    native_turns = identity.get("_execution_turn_ids", [])
    turn = native_turns[0] if native_turns else prior.turn if prior else None
    if not turn and identity.get("user_turn_id") and not identity.get("_execution_native_expected"):
        turn = _ensure_turn(
            r,
            identity["user_turn_id"],
            prior and parts(r.get(prior.run)["branch_id"])[2] or identity["session_id"],
        )
    if prior:
        activation = prior.activation
    elif identity.get("_execution_native_expected") and not native_turns:
        activation = r.id("activation")
    else:
        group = r.put("input_group", run, turn_ids=native_turns or ([turn] if turn else []))
        activation = r.put("activation", run, input_group_id=group, trigger_id=None)
    dispatch = parent.get("_execution_dispatch")
    if prior and not dispatch:
        dispatch = dispatch_start(prior, identity["run_id"])
    r.put(
        "run",
        run,
        key=identity["run_id"],
        branch_id=branch,
        activation_id=activation,
        dispatch_id=dispatch,
    )
    phase = r.put("phase", run, run_id=run, trigger_id=None, profile_id=AUXILIARY)
    lane = r.put("lane", run, phase_id=phase, fork_id=None)
    scope = Scope(
        run, phase, lane, activation, turn, input_turns=native_turns or ([turn] if turn else [])
    )
    if prior and prior.previous is None and (prior.fallback or prior.fallback_attempt):
        # A fallback marked on a dispatching Scope that has no calls of its own
        # (gateway runtime) is taken over by the next dispatched Run: its first
        # Attempt records the transition. A provider-internal fallback inside a
        # Scope with calls stays with that Scope.
        scope.fallback, scope.fallback_attempt = prior.fallback, prior.fallback_attempt
        prior.fallback = prior.fallback_attempt = None
    cap.execution_scopes[run] = scope
    return scope


@safe
def end_run(identity, status):
    cap, r, scope = current()
    run = r.id("run", identity)
    if not r.get(run):
        return
    owned = cap.execution_scopes.get(run)
    if owned and not r.get(owned.activation):
        group = r.put("input_group", run, turn_ids=[])
        r.put(
            "activation",
            run,
            key=parts(owned.activation)[2],
            input_group_id=group,
            trigger_id=_gap(r, owned),
        )
    closed = r.seal(run, status, failed=identity in cap.failed_runs)
    dispatch = r.get(run)["dispatch_id"]
    if dispatch:
        spec = r.get(dispatch)
        r.put(
            "dispatch_completion",
            key=parts(spec["completion_id"])[2],
            dispatch_id=dispatch,
            child_closure_id=closed,
            cancelled_id=None,
        )
        parent = cap.execution_scopes.get(spec["run_id"])
        if parent is not None and owned is not None:
            parent.last_child_call = owned.previous
            parent.last_child_attempt = owned.attempt
    cap.execution_scopes.pop(run, None)


@safe
def iteration_start(identity):
    _, r, s = current()
    if s is None:
        return
    if r.get(s.phase)["profile_id"] != LOOP:
        s.phase = r.put("phase", s.run, run_id=s.run, trigger_id=None, profile_id=LOOP)
        s.lane = r.put("lane", s.run, phase_id=s.phase, fork_id=None)
    s.iteration = r.put("iteration", s.run, key=identity, phase_id=s.phase, previous_id=s.iteration)
    s.operation = s.attempt = s.result = None


def _gap(r, s, affected=None):
    return r.put("gap", s.run, affected_id=affected)


def _message(r, s, message):
    from opensquilla.provider.types import MessageTraceIds

    metadata = getattr(message, "trace_ids", None)
    existing = metadata.execution_message_id if metadata else None
    signature = digest(canonical(message.business_dump()))
    if existing and r.get(existing):
        if metadata.execution_signature == signature:
            return existing
        transform = r.put("transform", s.run, input_ids=[existing])
        sources = [transform]
    elif metadata and metadata.input_turn_ids:
        sources = []
        for turn in metadata.input_turn_ids:
            # A declared origin is not the current execution's navigation anchor.
            # Legacy naked keys are only resolvable within this branch. Never
            # accept a new user input here just to make the graph look closed.
            branch = r.get(s.run)["branch_id"]
            origin = r.resolve_turn(branch, turn)
            if origin is None:
                # A dispatched run can explicitly inherit inputs from its parent
                # Session. Resolve against the declared activation, not s.turn.
                activation = r.get(s.activation)
                group = r.get(activation["input_group_id"]) if activation else None
                candidates = []
                for declared in group["turn_ids"] if group else ():
                    node = r.get(declared)
                    resolved = r.resolve_turn(node["branch_id"], turn) if node else None
                    if resolved and resolved["id"] == declared:
                        candidates.append(resolved)
                if len({n["id"] for n in candidates}) == 1:
                    origin = candidates[0]
            if origin:
                sources.append(origin["input_id"])
            else:
                sources.append(_gap(r, s))
    elif message.role == "user" and metadata is None:
        sources = [r.put("input", s.run, native_id=None)]
    else:
        sources = [_gap(r, s, existing)]
    value = r.put("message", s.run, source_ids=sources)
    if metadata is None:
        metadata = MessageTraceIds(message_id=uuid.uuid4().hex)
    message.trace_ids = metadata.model_copy(
        update={
            "execution_message_id": value,
            "execution_signature": signature,
            "execution_source_ids": (value,),
        }
    )
    return value


def context_message(message, *inputs):
    """Tag an observed framework transformation, or a framework-owned input.

    Call this at the constructor, with the actual input objects; never recover
    provenance by matching generated text against past messages.
    """
    _context_message(message, inputs)
    return message


@safe
def _context_message(message, inputs):
    from opensquilla.provider.types import MessageTraceIds

    _, r, s = current()
    if not s:
        return
    sources = [_message(r, s, item) for item in inputs]
    origin = r.put("transform", s.run, input_ids=sources) if inputs else r.put("definition", s.run)
    version = r.put("message", s.run, source_ids=[origin])
    metadata = message.trace_ids or MessageTraceIds(message_id=uuid.uuid4().hex)
    message.trace_ids = metadata.model_copy(
        update={
            "execution_message_id": version,
            "execution_signature": digest(canonical(message.business_dump())),
            "execution_source_ids": (version,),
        }
    )


@safe
def attempt_start(messages):
    from .capture import context

    _, r, s = current()
    if s is None:
        return
    ctx = context.get()
    operation = r.id("operation", ctx.get("logical_call_id") or uuid.uuid4().hex)
    if operation != s.operation:
        r.put(
            "operation",
            s.run,
            key=parts(operation)[2],
            lane_id=s.lane,
            iteration_id=s.iteration,
            parent_id=None,
            trigger_id=None,
        )
        s.operation, s.attempt, s.result = operation, None, None
    s.attempt = r.put(
        "attempt",
        s.run,
        key=ctx.get("outer_attempt_id") or uuid.uuid4().hex,
        operation_id=s.operation,
        previous_id=s.attempt,
        outer_id=None,
    )
    if s.fallback_attempt and s.fallback_attempt != s.attempt:
        r.put(
            "fallback_transition",
            s.run,
            from_attempt_id=s.fallback_attempt,
            to_attempt_id=s.attempt,
        )
        s.fallback_attempt = None
    # Remember which Attempt this Task registered for this Operation; a provider
    # entry from a Task that inherited the Scope but registered a different
    # Attempt must not be attributed to this one (see provider_enter).
    context.set({**ctx, "_execution_attempt": (s.operation, s.attempt)})
    s.message_ids = [_message(r, s, message) for message in messages]


@safe
def provider_enter(messages, *, auxiliary=False):
    from .capture import context

    cap, r, prior = current()
    owned = False
    if prior is None:
        raw = context.get().get("run_id") or uuid.uuid4().hex
        identity = {
            "run_id": raw,
            "session_id": context.get().get("session_id") or uuid.uuid4().hex,
            "user_turn_id": None,
        }
        prior = start_run(identity, {})
        if prior is None:
            return None
        owned = True
    leases = getattr(cap, "execution_provider_leases", None)
    if leases is None:
        leases = cap.execution_provider_leases = {}
    marker = context.get().get("_execution_attempt")
    foreign_attempt = bool(
        marker
        and prior.attempt
        and marker[0] == prior.operation
        and marker[1] != prior.attempt
    )
    # Two Tasks inherited one Scope and each registered an Attempt for the same
    # Operation; the Scope cursor now points at the other Task's Attempt. Never
    # attribute this request to it: isolate and withdraw completeness.
    concurrent = prior.lane in leases or foreign_attempt
    created_attempt = auxiliary or prior.operation is None or concurrent
    s = replace(prior, message_ids=tuple(prior.message_ids), provider_parent=prior)
    if created_attempt:
        phase = r.put(
            "phase",
            prior.run,
            run_id=prior.run,
            trigger_id=context.get().get("_execution_tool"),
            profile_id=AUXILIARY,
        )
        lane = r.put("lane", prior.run, phase_id=phase, fork_id=None)
        op = r.put(
            "operation",
            prior.run,
            lane_id=lane,
            iteration_id=None,
            parent_id=prior.operation,
            trigger_id=context.get().get("_execution_tool"),
        )
        attempt = r.put(
            "attempt", prior.run, operation_id=op, previous_id=None, outer_id=prior.attempt
        )
        s = Scope(
            prior.run, phase, lane, prior.activation, prior.turn, operation=op, attempt=attempt
        )
        if concurrent:
            # The generic entry cannot infer the missing fan-out boundary.
            # Isolate the actual request; explicitly withdraw completeness.
            _gap(r, s, prior.operation)
    s.message_ids = tuple(_message(r, s, message) for message in messages)
    leases[s.lane] = s
    token = context.set({**context.get(), "_execution_ids": s})
    return token, parts(s.run)[2] if owned else None, s, created_attempt


@safe
def provider_exit(binding):
    from .capture import context

    if binding:
        token, owned, s, created_attempt = binding
        cap, r, _ = current()
        if created_attempt:
            # This provider entry owns its Attempt. A semantic Done is stronger
            # evidence than transport EOF (some parsers stop reading early).
            attempt_end(
                success=s.provider_outcome == "success", failure=s.provider_outcome == "failure"
            )
        if cap.execution_provider_leases.get(s.lane) is s:
            del cap.execution_provider_leases[s.lane]
        if owned:
            end_run(owned, "completed")
        context.reset(token)


@safe
def call_start(call_id, business):
    _, r, s = current()
    if s is None or s.attempt is None:
        return None
    # The wire projection is a new immutable version derived from the prepared
    # message versions. Hashes detect mutation locally, never infer relationships.
    inputs = list(s.message_ids)
    if not inputs and (business.get("messages") or business.get("input")):
        inputs.append(_gap(r, s))
    projection = r.put("transform", s.run, input_ids=inputs)
    wire_message = r.put("message", s.run, source_ids=[projection])
    definitions = [
        r.put("definition", s.run)
        for name in ("system", "instructions", "tools")
        if business.get(name)
    ]
    context_id = r.put(
        "context",
        s.run,
        message_ids=[wire_message],
        selection_id=s.selection,
        definition_ids=definitions,
    )
    call = r.append_call(s.run, s.lane, s.attempt, context_id, call_id, s.fallback)
    s.fallback = None
    s.previous = call
    s.provider_outcome = None
    _publish_cursor(s)
    return CallSnapshot(s.run, s.attempt, s.operation, s.generation, tuple(inputs), s), call


@safe
def call_received(binding, status_code=None):
    if binding:
        s, call = binding
        _, r, _ = current()
        r.put("received", s.run, key=parts(call)[2], call_id=call)
        if status_code is not None and status_code >= 400:
            s.owner.provider_outcome = "failure"


@safe
def call_end(binding, error=None, complete=False):
    if not binding:
        return
    s, call = binding
    _, r, _ = current()
    terminal = "call_failure" if error else "call_success" if complete else "call_interrupted"
    r.put(terminal, s.run, key=parts(call)[2], call_id=call)
    if s.owner.provider_outcome is None:
        s.owner.provider_outcome = "failure" if error else "success" if complete else None
    _call_result(r, s, call)


def _call_result(r, s, call):
    # The logical output may be consumed before the HTTP iterator is closed.
    # One immutable result ID is shared by semantic Done and transport cleanup.
    result = r.put("result", s.run, key=parts(call)[2], producer_id=call, source_ids=[])
    if s.generation:
        r.put(
            "generation_output",
            s.run,
            key=parts(call)[2],
            generation_id=s.generation,
            result_id=result,
        )
    owner = s.owner if isinstance(s, CallSnapshot) else s
    if owner.previous == call and owner.attempt == s.attempt:
        owner.result = result
        _publish_cursor(owner)


@safe
def provider_result_ready(success=True):
    _, r, s = current()
    if s and not success:
        s.provider_outcome = "failure"
        return
    if s and s.previous and r.get(s.previous)["attempt_id"] == s.attempt:
        s.provider_outcome = "success"
        _call_result(r, s, s.previous)


@safe
def messages_committed(messages, phase):
    from .capture import context

    _, r, s = current()
    if not s:
        return
    for message in messages:
        if phase == "user_input":
            _message(r, s, message)
            continue
        sources = []
        if phase == "assistant_accepted_into_loop":
            result = s.result or r.put(
                "result", s.run, producer_id=s.operation or s.run, source_ids=[_gap(r, s)]
            )
            if s.operation:
                sources.append(
                    r.put("acceptance", s.run, operation_id=s.operation, result_id=result)
                )
            else:
                sources.append(result)
        elif phase == "tool_results_committed":
            for block in message.content if isinstance(message.content, list) else []:
                tool_id = getattr(block, "tool_use_id", None)
                if tool_id:
                    if tool_id not in s.tools and tool_id in s.cancelled_tools:
                        # The engine creates the timeout/cancellation observation
                        # after the handler was cancelled. It becomes a result
                        # only when actually committed, not when cancellation starts.
                        s.tools[tool_id] = r.put(
                            "result",
                            s.run,
                            producer_id=s.cancelled_tools[tool_id],
                            source_ids=[],
                        )
                    sources.append(s.tools.get(tool_id) or _gap(r, s))
        else:
            return
        version = r.put("message", s.run, source_ids=sources)
        metadata = message.trace_ids
        accepted_fields = {}
        if phase == "assistant_accepted_into_loop":
            producer = r.get(r.get(result)["producer_id"])
            if producer and kind(producer["id"]) == "call":
                from .id_graph import call_key

                namespace, _, physical_id = parts(producer["id"])
                # The accepted aggregate can belong to a different provider
                # scope. Map its authoritative physical ID to the legacy DTO;
                # do not inherit an unknown marker from the parent's cursor.
                accepted_fields = {
                    "source_call_ids": (call_key(namespace, physical_id),),
                    "unknown_call_ids": (),
                    "unknown_message_ids": (),
                }
        message.trace_ids = metadata.model_copy(
            update={
                **accepted_fields,
                "execution_message_id": version,
                "execution_signature": digest(canonical(message.business_dump())),
                "execution_source_ids": (version,),
            }
        )
        for block in message.content if isinstance(message.content, list) else []:
            if getattr(block, "type", None) == "tool_use":
                s.actions[block.id] = r.put("action", s.run, message_id=version)
        r.put("commit", s.run, message_ids=[version], native_id=None)
        s.history.append(version)
        # Keep the existing native transcript adapter lossless for both contracts.
        old = context.get().get("_id_state")
        if old:
            for row in old.history_ids:
                if row["trace_ids"]["message_id"] == metadata.message_id:
                    row["trace_ids"] = message.trace_ids.model_dump(mode="json")
            if phase == "assistant_accepted_into_loop":
                old.result_ids = message.trace_ids.model_dump(mode="json")


def result_sources(value):
    metadata = getattr(value, "trace_ids", None)
    result = set(metadata.execution_source_ids if metadata else ())
    if isinstance(value, (tuple, list)):
        for item in value:
            result.update(result_sources(item))
    elif isinstance(value, dict):
        for item in value.values():
            result.update(result_sources(item))
    return result


@safe
def tool_start(tool_id, execution_id, deferred=False):
    from .capture import context

    _, r, s = current()
    if not s:
        return None
    s.tools.pop(tool_id, None)
    s.cancelled_tools.pop(tool_id, None)
    action = s.actions.get(tool_id)
    if not action:
        _gap(r, s)
    attempt = r.put("tool_attempt", s.run, run_id=s.run, action_id=action)
    binding = {
        "scope": s,
        "tool_id": tool_id,
        "attempt": attempt,
        "key": execution_id,
        "execution": None,
        "raw": None,
    }
    context.set({**context.get(), "_execution_tool_binding": binding})
    if not deferred:
        tool_enter()
    return binding


@safe
def tool_enter():
    from .capture import context

    _, r, _ = current()
    binding = context.get().get("_execution_tool_binding")
    if binding and not binding["execution"]:
        binding["execution"] = r.put(
            "tool_execution",
            binding["scope"].run,
            key=binding["key"],
            attempt_id=binding["attempt"],
        )
        context.set({**context.get(), "_execution_tool": binding["execution"]})


@safe
def tool_observation(result):
    from .capture import context

    _, r, _ = current()
    binding = context.get().get("_execution_tool_binding")
    if binding and binding["execution"]:
        binding["raw"] = r.put(
            "result",
            binding["scope"].run,
            producer_id=binding["execution"],
            source_ids=sorted(result_sources(result)),
        )


@safe
def tool_end(binding, result=None, error=None):
    if not binding:
        return
    s, tool_id, execution = binding["scope"], binding["tool_id"], binding["execution"]
    _, r, _ = current()
    if not execution:
        r.put("tool_rejection", s.run, attempt_id=binding["attempt"])
        s.tools[tool_id] = r.put("result", s.run, producer_id=binding["attempt"], source_ids=[])
        return
    if error and type(error).__name__ in {"CancelledError", "GeneratorExit"}:
        s.cancelled_tools[tool_id] = r.put("tool_cancelled", s.run, execution_id=execution)
        return
    content = getattr(result, "content", None)
    explicit = getattr(result, "trace_ids", None)
    sources = result_sources(result) if explicit is not None else result_sources(content)
    if binding["raw"]:
        sources.add(binding["raw"])
    metadata = explicit or getattr(content, "trace_ids", None)
    if metadata and (
        getattr(metadata, "unknown_call_ids", ()) or getattr(metadata, "unknown_message_ids", ())
    ):
        sources.add(_gap(r, s, execution))
    # Awaiting a child establishes observation, not whether an arbitrary new
    # string uses it. An explicit empty DTO is the declaration of independent output.
    if explicit is None and set(s.waits.get(execution, ())) - sources:
        sources.add(_gap(r, s, execution))
    produced = r.put("result", s.run, producer_id=execution, source_ids=sorted(sources))
    s.tools[tool_id] = produced
    r.put(
        "tool_failure" if error or getattr(result, "is_error", False) else "tool_success",
        s.run,
        execution_id=execution,
        result_id=produced,
    )


def _tool_origin_call(r, execution):
    # Follow the immutable accepted action, not the parent's current lane head:
    # an aggregator or an earlier asynchronous action can own this tool call.
    node = r.get(execution) or {}
    attempt = r.get(node.get("attempt_id")) or {}
    action = r.get(attempt.get("action_id")) or {}
    message = r.get(action.get("message_id")) or {}
    calls = set()
    for source in message.get("source_ids", ()):
        acceptance = r.get(source) or {}
        if not acceptance or kind(acceptance["id"]) != "acceptance":
            continue
        result = r.get(acceptance.get("result_id")) or {}
        producer = r.get(result.get("producer_id")) or {}
        if producer and kind(producer["id"]) == "call":
            calls.add(producer["id"])
    return next(iter(calls)) if len(calls) == 1 else None


@safe
def dispatch_start(scope=None, child_id=None):
    from .capture import context

    _, r, s = current()
    s = scope or s
    if not s:
        return None
    tool = context.get().get("_execution_tool")
    call = _tool_origin_call(r, tool) if tool else s.previous
    if tool and not call:
        _gap(r, s, tool)
    completion = r.id("dispatch_completion", child_id)
    r.reserve(s.run, completion)
    return r.put(
        "dispatch",
        s.run,
        key=child_id,
        run_id=s.run,
        call_id=call,
        tool_id=tool,
        completion_id=completion,
    )


@safe
def dispatch_cancel(dispatch):
    _, r, _ = current()
    if not dispatch:
        return
    node = r.get(dispatch)
    if node and not r.get(node["completion_id"]):
        cancelled = r.put("dispatch_cancelled", dispatch_id=dispatch)
        r.put(
            "dispatch_completion",
            key=parts(node["completion_id"])[2],
            dispatch_id=dispatch,
            child_closure_id=None,
            cancelled_id=cancelled,
        )


@safe
def waited(value):
    from .capture import context

    _, r, s = current()
    if s and (sources := result_sources(value)):
        operation = context.get().get("_execution_tool") or s.operation or s.run
        r.put("wait", s.run, operation_id=operation, result_ids=sorted(sources))
        s.waits.setdefault(operation, set()).update(sources)


@safe
def round_start(key):
    _, r, s = current()
    if s:
        return r.put("ensemble", s.run, key=key, operation_id=s.operation)


@safe
def schedule_candidate(key, ensemble):
    _, r, s = current()
    if s:
        lane = r.put("lane", s.run, phase_id=s.phase, fork_id=ensemble)
        return r.put("candidate", s.run, key=key, ensemble_id=ensemble, lane_id=lane)


@safe
def candidate_cancelled(key):
    _, r, s = current()
    if s and r.get(r.id("candidate", key)):
        r.put("candidate_cancelled", s.run, key=key, candidate_id=r.id("candidate", key))


@safe
def candidate_start(key, ensemble):
    from .capture import context

    _, r, s = current()
    if not s:
        return None
    candidate = r.id("candidate", key)
    if not r.get(candidate):
        schedule_candidate(key, ensemble)
    lane = r.get(candidate)["lane_id"]
    operation = r.put(
        "operation",
        s.run,
        key=key,
        lane_id=lane,
        iteration_id=s.iteration,
        parent_id=s.operation,
        trigger_id=candidate,
    )
    branch = replace(
        s,
        lane=lane,
        operation=operation,
        attempt=None,
        previous=None,
        result=None,
        ensemble=ensemble,
        selection=None,
    )
    return context.set({**context.get(), "_execution_ids": branch}), candidate


@safe
def candidate_end(binding, result, cancelled=False):
    from .capture import context

    if binding:
        token, candidate = binding
        _, r, s = current()
        if result is not None and result.ok and s.result:
            value = r.put("candidate_result", s.run, candidate_id=candidate, result_id=s.result)
            result.trace_origin["execution_result_id"] = value
        elif cancelled:
            candidate_cancelled(parts(candidate)[2])
        else:
            r.put("candidate_failure", s.run, candidate_id=candidate)
        context.reset(token)


@safe
def aggregation_start(key, ensemble, candidates, messages, role):
    from .capture import context

    cap, r, parent = current()
    if not parent:
        return None
    projection_ids = []
    for candidate in candidates:
        origin = candidate.trace_origin.get("execution_result_id")
        if not origin:
            _gap(r, parent)
            continue
        projection_ids.append(r.put("projection", parent.run, candidate_result_id=origin))
    selection = (
        r.put("selection", parent.run, ensemble_id=ensemble, projection_ids=projection_ids)
        if projection_ids
        else None
    )
    for source_ensemble in sorted(
        {
            r.get(r.get(r.get(projection)["candidate_result_id"])["candidate_id"])["ensemble_id"]
            for projection in projection_ids
        }
        - {ensemble}
    ):
        r.put(
            "selection_reuse",
            parent.run,
            selection_id=selection,
            source_ensemble_id=source_ensemble,
        )
    lane = r.put("lane", parent.run, phase_id=parent.phase, fork_id=ensemble)
    operation = r.put(
        "operation",
        parent.run,
        key=key,
        lane_id=lane,
        iteration_id=parent.iteration,
        parent_id=parent.operation,
        trigger_id=selection or ensemble,
    )
    previous = cap.execution_aggregations.get(ensemble)
    aggregation = r.put(
        "aggregation",
        parent.run,
        key=key,
        ensemble_id=ensemble,
        operation_id=operation,
        selection_id=selection,
        fallback_id=previous if role.startswith("fixed") else None,
    )
    cap.execution_aggregations[ensemble] = aggregation
    branch = replace(
        parent,
        lane=lane,
        operation=operation,
        attempt=None,
        previous=None,
        result=None,
        ensemble=ensemble,
        selection=selection,
    )
    if selection:
        # The final candidate bundle is appended after the original conversation.
        from opensquilla.provider.types import MessageTraceIds

        if messages:
            final = messages[-1]
            version = r.put("message", parent.run, source_ids=[selection])
            metadata = final.trace_ids or MessageTraceIds(message_id=uuid.uuid4().hex)
            final.trace_ids = metadata.model_copy(
                update={
                    "execution_message_id": version,
                    "execution_source_ids": (version,),
                    "execution_signature": digest(canonical(final.business_dump())),
                }
            )
    branch.message_ids = [_message(r, branch, message) for message in messages]
    return context.set({**context.get(), "_execution_ids": branch}), parent


@safe
def aggregation_result_ready(binding):
    if binding:
        _, parent = binding
        _, _, s = current()
        parent.result = s.result
        _publish_cursor(parent)


@safe
def aggregation_end(binding, accepted):
    from .capture import context

    if binding:
        token, parent = binding
        _, _, s = current()
        if accepted:
            parent.result = s.result
            _publish_cursor(parent)
        context.reset(token)


@safe
def member_start(key):
    from .capture import context

    _, r, s = current()
    if not s:
        return
    outer = r.get(s.operation)["parent_id"]
    cap, _, _ = current()
    root = cap.execution_scopes.get(s.run)
    s.attempt = r.put(
        "attempt",
        s.run,
        key=key,
        operation_id=s.operation,
        previous_id=s.attempt,
        outer_id=root.attempt if root and outer else None,
    )
    s.generation = r.put("generation", s.run, attempt_id=s.attempt)
    role = context.get().get("role")
    if not hasattr(cap, "execution_generations"):
        cap.execution_generations = {}
    cap.execution_generations[(s.ensemble, role)] = s.generation
    previous = getattr(cap, "execution_pending_replacements", {}).pop((s.ensemble, role), None)
    if previous:
        r.put("replacement", s.run, previous_id=previous, next_id=s.generation)
    s.result = None


@safe
def mark_fallback():
    _, _, s = current()
    if s:
        if s.previous is None and s.last_child_call:
            # Gateway-level fallback: the failed physical call belongs to the
            # dispatched Agent Run that just closed, not to this Scope's lane.
            s.fallback, s.fallback_attempt = s.last_child_call, s.last_child_attempt
        else:
            s.fallback, s.fallback_attempt = s.previous, s.attempt


@safe
def attempt_end(success=False, failure=False):
    _, r, s = current()
    if s and s.attempt:
        typ = (
            "attempt_success"
            if success
            else "attempt_failure"
            if failure
            else "attempt_interrupted"
        )
        r.put(typ, s.run, key=parts(s.attempt)[2], attempt_id=s.attempt)


@safe
def generation_reset(from_role, to_role, terminal):
    from .capture import context

    cap, r, s = current()
    if not s:
        return
    state = context.get().get("_ensemble_round")
    ensemble = getattr(state, "execution_id", None)
    source_role = "aggregator" if str(from_role) == "primary_aggregator" else str(from_role)
    target_role = "aggregator" if str(to_role) == "primary_aggregator" else str(to_role)
    generations = getattr(cap, "execution_generations", {})
    old = generations.get((ensemble, source_role))
    if old:
        r.put("generation_discarded", s.run, key=parts(old)[2], generation_id=old)
        if not terminal:
            if not hasattr(cap, "execution_pending_replacements"):
                cap.execution_pending_replacements = {}
            cap.execution_pending_replacements[(ensemble, target_role)] = old


@safe
def delivery_start(identity, message, previous):
    from .capture import context

    cap, r, _ = current()
    identity = {**identity, "session_id": identity.get("session_id") or uuid.uuid4().hex}
    scope = start_run(identity, {})
    if scope is None:
        return None
    context.set({**context.get(), "_execution_ids": scope})
    metadata = getattr(message, "metadata", {}) or {}
    message_ids = list(metadata.get("execution_message_ids", ()))
    if not message_ids:
        source = previous.get("_execution_ids")
        message_ids = list(source.history[-1:]) if source else []
    if not message_ids:
        version = r.put("message", scope.run, source_ids=[_gap(r, scope)])
        message_ids = [version]
    delivery = r.put("delivery", scope.run, message_ids=message_ids)
    return scope, delivery


@safe
def delivery_end(binding, success):
    if binding:
        scope, delivery = binding
        _, r, _ = current()
        r.put("delivered" if success else "delivery_failed", scope.run, delivery_id=delivery)
        end_run(parts(scope.run)[2], "completed" if success else "error")


@safe
def scheduled_candidates_cancelled(tasks):
    from .capture import context

    state = context.get().get("_ensemble_round")
    if state:
        for index, task in enumerate(tasks):
            if task.cancelled() and index in state.scheduled:
                candidate_cancelled(state.scheduled[index])


@safe
def claimed_input(message, native_message_ids=(), *, goal=False, local=False):
    from opensquilla.provider.types import MessageTraceIds

    _, r, s = current()
    if not s:
        return
    branch = r.get(s.run)["branch_id"]
    turns, sources = [], []
    if local:
        # A sessionless in-process queue becomes this Session's input at claim.
        count = len(message.content) if isinstance(message.content, list) else 1
        turns = [r.accept_local_turn(branch) for _ in range(count)]
    else:
        for offset, message_id in enumerate(native_message_ids):
            if goal and offset == 0:
                sources.append(r.put("definition", s.run))
                continue
            with r.journal.lock:
                row = r.journal.db.execute(
                    "SELECT value FROM meta WHERE key=?",
                    ("identity_native_input:" + json.dumps([parts(branch)[2], message_id]),),
                ).fetchone()
            if row:
                turns.append(row[0])
            else:
                sources.append(_gap(r, s))
        if not native_message_ids:
            sources.append(_gap(r, s))
    group = r.put("input_group", s.run, turn_ids=turns)
    sources.append(group)
    sources.extend(r.get(turn)["input_id"] for turn in turns if r.get(turn))
    version = r.put("message", s.run, source_ids=sources)
    metadata = message.trace_ids or MessageTraceIds(message_id=uuid.uuid4().hex)
    message.trace_ids = metadata.model_copy(
        update={
            "execution_message_id": version,
            "execution_source_ids": (version,),
            "execution_signature": digest(canonical(message.business_dump())),
        }
    )


def trace_preflight(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if result is not None:
            tool_rejected(kwargs.get("tool_call"))
        return result

    return wrapped


@safe
def tool_rejected(call):
    _, r, s = current()
    if not s or call is None:
        return
    action = s.actions.get(call.tool_use_id)
    if action is None:
        _gap(r, s)
    attempt = r.put("tool_attempt", s.run, run_id=s.run, action_id=action)
    r.put("tool_rejection", s.run, attempt_id=attempt)
    s.tools[call.tool_use_id] = r.put("result", s.run, producer_id=attempt, source_ids=[])
