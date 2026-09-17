"""Opt-in framework instrumentation; failures are visible and do not stop business work."""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from .journal import TraceJournal
from .protocol import CONTEXT_KEYS, canonical, digest
from .transport import Destination, PiggybackTransport

log = logging.getLogger(__name__)
context: contextvars.ContextVar[dict] = contextvars.ContextVar("trace_capture_context", default={})
_active: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "trace_capture_override", default=None
)
_cache: dict[tuple, Any] = {}
_cache_lock = threading.RLock()
SENSITIVE = {
    "authorization",
    "api_key",
    "api-key",
    "x-api-key",
    "password",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
    "client_secret",
    "secret",
}


def serializable(value: Any, redactions: list[str] | None = None) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE:
                out[str(key)] = "[redacted]"
                if redactions is not None:
                    redactions.append(str(key))
            else:
                out[str(key)] = serializable(item, redactions)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serializable(v, redactions) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bytes):
        import base64

        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return {"unserialized_type": type(value).__name__}


class Capture:
    def __init__(
        self,
        root: str | Path,
        destination: Destination,
        *,
        quota: int = 10 * 1024**3,
        max_request_bytes: int = 16 * 1024**2,
        environment_roots: list[str] | None = None,
        execution_version: int = 3,
        record_scope: str = "full",
        retain_acknowledged_seconds: float | None = None,
    ):
        if type(execution_version) is not int or execution_version not in {3, 4}:
            raise ValueError("invalid_execution_version")
        if record_scope not in {"full", "identity"}:
            raise ValueError("invalid_record_scope")
        if retain_acknowledged_seconds is not None and (
            not isinstance(retain_acknowledged_seconds, (int, float))
            or retain_acknowledged_seconds < 0
        ):
            raise ValueError("invalid_retention")
        # None keeps acknowledged data locally forever (explicit maintenance only).
        # 0 deletes acknowledged events/bodies and sealed-Run declarations as soon as
        # the platform has confirmed them; the unconfirmed tail always stays.
        self.retain_acknowledged_seconds = retain_acknowledged_seconds
        # "identity": keep only the ID declarations the platform needs to rebuild
        # the execution graph (entities, lifecycle facts, closures, call_correlation).
        # No event journal, no request/response bodies, no environment snapshots,
        # no legacy call_ids/turn_ids/manifest records.
        self.record_scope = record_scope
        self.execution_version = execution_version
        self.destination = destination
        self.journal = TraceJournal(Path(root) / destination.key, destination.key, quota=quota)
        self.transport = PiggybackTransport(self.journal, execution_version=execution_version)
        self.max_request_bytes = max_request_bytes
        self.environment_roots = environment_roots or []
        self.failed_runs: set[str] = set()
        self.id_runs: dict[str, Any] = {}
        from .identity_registry import IdentityRegistry

        self.identities = IdentityRegistry(self.journal)
        self.execution_scopes: dict[str, Any] = {}
        self.execution_aggregations: dict[str, str] = {}
        self.config_path: Path | None = None

    def current_configuration_allows_capture(self) -> bool:
        if self.config_path is None:
            return True
        try:
            current = json.loads(self.config_path.read_text())
            return (
                bool(current.get("enabled"))
                and Destination(**current["destination"]).key == self.destination.key
            )
        except Exception:
            return False

    def fail(self) -> None:
        current = context.get()
        self.failed_runs.add(current.get("run_id", "unscoped"))
        # Auxiliary operation contexts replace run_id with a never-sealed identity.
        # Also charge the execution Scope that will actually be sealed, so its
        # closure carries the gap instead of only the global capture_gap flag.
        scope = current.get("_execution_ids")
        run = getattr(scope, "run", None)
        if isinstance(run, str):
            try:
                from .identity import parts

                self.failed_runs.add(parts(run)[2])
            except Exception:
                pass
        try:
            self.journal.mark_gap()
        except Exception:
            pass
        log.warning("trace_capture_gap")

    def emit(
        self,
        kind: str,
        payload: Any = None,
        *,
        identity: dict | None = None,
        blob_refs: list[str] | None = None,
        links: list[dict] | None = None,
    ) -> dict | None:
        if not self.current_configuration_allows_capture():
            self.failed_runs.add(context.get().get("run_id", "unscoped"))
            return None
        if self.record_scope == "identity":
            return None  # events and bodies are deliberately not recorded
        try:
            ctx = {**context.get(), **(identity or {})}
            redactions: list[str] = []
            data = serializable(payload or {}, redactions)
            if not isinstance(data, dict):
                data = {"value": data}
            refs = list(blob_refs or [])
            if len(canonical(data)) > 16 * 1024:
                ref = self.journal.content.put(canonical(data))
                data = {"content_ref": ref, "encoding": "json"}
                refs.append(ref)
            return self.journal.append(
                {
                    "kind": kind,
                    "payload": data,
                    "context": {
                        k: str(v) for k, v in ctx.items() if k in CONTEXT_KEYS and v is not None
                    },
                    "blob_refs": sorted(set(refs)),
                    "links": links
                    if links is not None
                    else [
                        {"event_id": cause, "relation": "operation_origin"}
                        for cause in ctx.get("_causes", [])
                        if cause
                    ],
                    "redacted_fields": sorted(set(redactions)),
                }
            )
        except Exception:
            self.fail()
            return None


def get_capture() -> Capture | None:
    override = _active.get()
    if override is not None:
        return override
    filename = os.environ.get("OPENSQUILLA_TRACE_CONFIG")
    if not filename:
        return None
    try:
        path = Path(filename)
        key = (os.getpid(), str(path), path.stat().st_mtime_ns)
        with _cache_lock:
            if key not in _cache:
                config = json.loads(path.read_text())
                if not config.get("enabled", False):
                    return None
                destination = Destination(**config["destination"])
                _cache[key] = Capture(
                    config["root"],
                    destination,
                    quota=config.get("quota_bytes", 10 * 1024**3),
                    max_request_bytes=config.get("max_request_bytes", 16 * 1024**2),
                    environment_roots=config.get("environment_roots", []),
                    execution_version=config.get("execution_version", 3),
                    # Deployment defaults: declarations only, and acknowledged data is
                    # removed locally right after the platform confirms it. A config
                    # can opt into "full" and into keeping data (null = forever).
                    record_scope=config.get("record_scope", "identity"),
                    retain_acknowledged_seconds=config.get("retain_acknowledged_seconds", 0),
                )
                _cache[key].config_path = path
            return _cache[key]
    except Exception:
        log.warning("trace_config_unavailable")
        return None


def enabled() -> bool:
    return get_capture() is not None


def emit(kind: str, payload: Any = None, **kwargs) -> dict | None:
    capture = get_capture()
    return capture.emit(kind, payload, **kwargs) if capture else None


def observe_trace(event: dict) -> None:
    """Import the existing operational trace without changing its v1 file format."""
    emit(
        "runtime." + event["kind"],
        {
            "attrs": event.get("attrs", {}),
            "payload": event.get("payload", {}),
            "legacy_context": {k: event[k] for k in CONTEXT_KEYS if event.get(k)},
        },
        identity={k: event[k] for k in ("turn_id", "session_id", "agent_id") if event.get(k)},
    )


def capture_environments(capture: Capture, phase: str) -> list[dict]:
    snapshots = []
    if capture.environment_roots and capture.record_scope == "full":
        from .environment import FileEnvironmentAdapter

        for root in capture.environment_roots:
            try:
                snapshot = FileEnvironmentAdapter(capture.journal.content).capture(Path(root))
                capture.emit(
                    "environment.checkpoint",
                    {"snapshot": snapshot, "phase": phase},
                    blob_refs=snapshot["blob_refs"],
                )
                snapshots.append(snapshot)
            except Exception:
                capture.fail()
    return snapshots


def trace_run(fn):
    """Observe the full async-generator lifetime, including finalization and cancellation."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        capture = get_capture()
        if capture is None:
            async for item in fn(*args, **kwargs):
                yield item
            return
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        parent = context.get()
        run_id = parent.get("_scheduled_run_id") or uuid.uuid4().hex
        turn_id = values.get("root_turn_id")
        execution = values.get("execution_context")
        if execution is not None:
            turn_id = getattr(getattr(execution, "identity", None), "turn_id", turn_id)
        turn_id = (
            turn_id
            or (parent.get("turn_id") if fn.__name__ == "run_turn" else None)
            or uuid.uuid4().hex
        )
        if "root_turn_id" in signature.parameters and not values.get("root_turn_id"):
            bound.arguments["root_turn_id"] = turn_id
        session_key = values.get("session_key", getattr(values.get("self"), "_session_key", ""))
        identity = {
            "run_id": run_id,
            "turn_id": turn_id,
            "user_turn_id": parent.get("user_turn_id"),
            "trace_id": parent.get("trace_id", run_id),
            "session_id": values.get("expected_session_id")
            or parent.get("session_id")
            or digest(str(session_key).encode()),
            "agent_id": values.get("agent_id", "main"),
            "_execution_session_key": digest(str(session_key).encode()),
        }
        owns_user_turn = (
            not parent.get("run_id")
            and not identity["user_turn_id"]
            and (values.get("run_kind", "default") in {"default", "user", "chat"})
        )
        if owns_user_turn:
            identity["user_turn_id"] = uuid.uuid4().hex
        if parent.get("run_id"):
            if not parent.get("_scheduled_run_id"):
                capture.emit("agent.spawn", {"child_run_id": run_id})
            identity["parent_run_id"] = parent["run_id"]
        from .identity_native import activation_inputs

        identity.update(await activation_inputs(values.get("self"), values))
        try:
            from .id_capture import start_run

            identity["_id_state"] = start_run(capture, identity, parent)
            from . import identity_runtime

            identity["_execution_ids"] = identity_runtime.start_run(identity, parent)
        except Exception:
            capture.fail()
        token = context.set(identity)
        capture_token = _active.set(capture)
        status = "cancelled"
        terminal_seen = False
        generator = None
        try:
            if owns_user_turn:
                user_start = capture.emit(
                    "user_turn.start",
                    {
                        "input": values.get("message", ""),
                        "attachments": values.get("attachments", []),
                        "root_run_id": run_id,
                    },
                )
                if user_start:
                    context.set({**context.get(), "_causes": [user_start["event_id"]]})
            capture.emit(
                "run.start",
                {
                    "input": values.get("message", ""),
                    "attachments": values.get("attachments", []),
                    "run_kind": values.get("run_kind", "default"),
                    "lineage": parent.get("_lineage"),
                    "framework": {
                        "base_commit": "23f9c83a902c323fca7c3578721e674092df0e09",
                        "instrumentation_version": "tracepoint-4.0",
                    },
                },
            )
            if not parent.get("run_id"):
                capture_environments(capture, "run_start")
            generator = fn(*bound.args, **bound.kwargs)
            async for item in generator:
                if (
                    item.get("kind") if isinstance(item, dict) else getattr(item, "kind", None)
                ) == "done":
                    terminal_seen = True
                capture.emit(
                    "agent.output" if fn.__name__ == "run_turn" else "runtime.output",
                    {"event": item},
                )
                yield item
            status = "completed"
        except BaseException as exc:
            status = (
                "completed"
                if isinstance(exc, GeneratorExit) and terminal_seen
                else (
                    "cancelled"
                    if isinstance(exc, GeneratorExit) or type(exc).__name__ == "CancelledError"
                    else "error"
                )
            )
            if not (isinstance(exc, GeneratorExit) and terminal_seen):
                capture.emit("run.exception", {"type": type(exc).__name__})
            raise
        finally:
            try:
                if generator is not None:
                    await generator.aclose()
                from .causality import iteration_end

                iteration_end(status)
                from . import identity_runtime

                identity_runtime.end_run(run_id, status)
                try:
                    from .id_capture import finish_run

                    finish_run(capture, run_id)
                except Exception:
                    capture.fail()
                if owns_user_turn:
                    capture.emit("user_turn.end", {"status": status, "root_run_id": run_id})
                capture.emit("run.end", {"status": status})
                coverage = {
                    "capture_gap": run_id in capture.failed_runs,
                    "loop_causality": "explicit-v1"
                    if context.get().get("_loop_state") is not None
                    else "not_observed",
                    "boundary": "runtime_return",
                    "user_delivery": "separate_receipt_required",
                }
                if capture.record_scope == "full":
                    try:
                        capture.journal.seal(run_id, coverage=coverage)
                    except Exception:
                        capture.fail()
            finally:
                context.reset(token)
                _active.reset(capture_token)

    return wrapper


def trace_tool_handler(handler, initial_context):
    @functools.wraps(handler)
    async def wrapper(tool_call):
        capture = get_capture()
        if capture is None:
            return await handler(tool_call)
        tool_id = (
            getattr(tool_call, "tool_use_id", None)
            or getattr(tool_call, "id", None)
            or getattr(tool_call, "tool_call_id", None)
        )
        execution_id = uuid.uuid4().hex
        token = context.set(
            {**context.get(), "tool_call_id": tool_id, "tool_execution_id": execution_id}
        )
        from . import identity_runtime

        execution_binding = identity_runtime.tool_start(
            tool_id,
            execution_id,
            deferred=bool(getattr(handler, "_trace_deferred_execution", False)),
        )
        try:
            capture.emit("tool.requested", {"call": tool_call})
            before = capture_environments(capture, "before_tool")
            result = await handler(tool_call)
            identity_runtime.tool_end(execution_binding, result)
            try:
                from .id_capture import tool_returned

                tool_returned(tool_id, result)
            except Exception:
                capture.fail()
            recorded = capture.emit("tool.returned", {"result": result})
            from .causality import note_tool_result

            note_tool_result(tool_id, recorded)
            after = capture_environments(capture, "after_tool")
            from .environment import FileEnvironmentAdapter

            for old, new in zip(before, after):
                if old["source_root"] == new["source_root"]:
                    capture.emit("environment.diff", FileEnvironmentAdapter.diff(old, new))
            return result
        except BaseException as exc:
            identity_runtime.tool_end(execution_binding, error=exc)
            capture.emit("tool.exception", {"type": type(exc).__name__})
            raise
        finally:
            context.reset(token)

    return wrapper


def child_task_context(run_id: str, task: Any = None):
    """Record delegation before scheduling, including children that never begin running."""
    capture = get_capture()
    if capture:
        capture.emit("agent.spawn", {"child_run_id": run_id, "task": task})
    copied = contextvars.copy_context()
    state = context.get().get("_id_state")
    from . import identity_runtime

    dispatch = identity_runtime.dispatch_start(child_id=run_id)
    copied.run(
        context.set,
        {
            **context.get(),
            "_scheduled_run_id": run_id,
            "_id_spawn_call": state.latest_call_id if state else None,
            "_id_dispatch_id": uuid.uuid4().hex,
            "_execution_dispatch": dispatch,
        },
    )
    return copied


def trace_committed_message(fn):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        capture = get_capture()
        if capture:
            from .identity_native import committed

            committed(result)
            values = dict(signature.bind(*args, **kwargs).arguments)
            values.pop("self", None)
            values.pop("session_key", None)
            capture.emit("session.message_committed", {"message": values, "receipt": result})
        return result

    return wrapper


def trace_state_commit(kind):
    def decorate(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            capture = get_capture()
            if capture:
                values = dict(signature.bind(*args, **kwargs).arguments)
                values.pop("self", None)
                values.pop("session_key", None)
                capture.emit(kind, {"inputs": values, "receipt": result})
            return result

        return wrapper

    return decorate


def trace_delivery(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        capture = get_capture()
        if capture is None:
            return await fn(*args, **kwargs)
        delivery_id = uuid.uuid4().hex
        # A delivery scope closes independently; it may happen after the runtime has closed.
        previous = context.get()
        run_id = uuid.uuid4().hex
        token = context.set(
            {
                "run_id": run_id,
                "trace_id": previous.get("trace_id", run_id),
                "turn_id": previous.get("turn_id"),
                "user_turn_id": previous.get("user_turn_id"),
                "session_id": previous.get("session_id"),
            }
        )
        status = "error"
        from . import identity_runtime

        execution_binding = identity_runtime.delivery_start(
            context.get(), args[1] if len(args) > 1 else kwargs.get("message"), previous
        )
        try:
            capture.emit("run.start", {"operation": fn.__name__, "delivery_id": delivery_id})
            capture.emit("delivery.requested", {"message": args[1] if len(args) > 1 else kwargs})
            result = await fn(*args, **kwargs)
            capture.emit("delivery.completed", {"receipt": result, "delivery_id": delivery_id})
            status = "completed"
            return result
        except BaseException as exc:
            capture.emit("delivery.failed", {"error_type": type(exc).__name__})
            raise
        finally:
            identity_runtime.delivery_end(execution_binding, status == "completed")
            capture.emit("run.end", {"status": status})
            if capture.record_scope == "full":
                try:
                    capture.journal.seal(
                        run_id,
                        coverage={
                            "boundary": "channel_return",
                            "capture_gap": run_id in capture.failed_runs,
                        },
                    )
                except Exception:
                    capture.fail()
            context.reset(token)

    return wrapper
