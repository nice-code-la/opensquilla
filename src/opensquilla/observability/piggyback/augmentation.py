"""Environment-backed task admission with deterministic, independent verifiers."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from .environment import is_owned, safe_relative
from .journal import atomic_write
from .protocol import canonical, digest


@dataclasses.dataclass(frozen=True)
class TaskSpec:
    task_id: str
    instruction: str
    source_run_id: str
    source_revision: str
    source_family: str
    snapshot_id: str
    allowed_tools: tuple[str, ...]
    verifier: dict
    reference_actions: tuple[dict, ...]
    negative_actions: tuple[dict, ...]

    @property
    def split(self) -> str:
        value = int(digest(self.source_family.encode())[:8], 16) % 100
        return "test" if value < 10 else "validation" if value < 20 else "train"


def _target(root: Path, relative: str) -> Path:
    candidate = root / safe_relative(relative)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("task_path_escapes_environment")
    return candidate


def verify_task(spec: dict, root: Path) -> bool:
    kind = spec.get("kind")
    if kind == "all":
        checks = spec.get("checks", [])
        return bool(checks) and all(verify_task(check, root) for check in checks)
    if kind == "file_equals":
        path = _target(root, spec["path"])
        return path.is_file() and path.read_text(encoding="utf-8") == spec["content"]
    if kind == "json_equals":
        path = _target(root, spec["path"])
        try:
            return json.loads(path.read_text()) == spec["value"]
        except (OSError, ValueError):
            return False
    if kind == "sqlite_query":
        path = _target(root, spec["path"])
        if not path.is_file():
            return False
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            try:
                rows = db.execute(spec["query"], spec.get("parameters", [])).fetchall()
            except sqlite3.Error:
                return False
        return [list(row) for row in rows] == spec["rows"]
    raise ValueError("unsupported_verifier")


def execute_actions(
    actions: tuple[dict, ...], root: Path, allowed_tools: tuple[str, ...]
) -> list[dict]:
    if not is_owned(root):
        raise ValueError("task_execution_requires_owned_environment")
    trajectory = []
    for index, action in enumerate(actions):
        tool = action["tool"]
        if tool not in allowed_tools:
            raise ValueError("task_tool_not_allowed")
        path = _target(root, action["path"])
        if tool == "file_write":
            atomic_write(path, action["content"].encode())
            result = {"bytes": path.stat().st_size}
        elif tool == "sqlite_execute":
            with sqlite3.connect(path) as db:
                db.execute(action["query"], action.get("parameters", []))
            result = {"committed": True}
        else:
            raise ValueError("unsupported_reference_tool")
        trajectory.append({"step": index + 1, "action": action, "result": result})
    return trajectory


def admit_task(
    task: TaskSpec,
    bundle: dict,
    snapshot: dict,
    adapter,
    target: Path,
    runner: Callable | None = None,
) -> dict:
    report = bundle["runs"].get(task.source_run_id, {})
    if (
        not report.get("complete")
        or bundle.get("provenance") != "platform"
        or report.get("environment_grade") != "executable"
        or task.snapshot_id != snapshot["snapshot_id"]
        or task.source_revision != bundle["revision"]
        or snapshot["snapshot_id"] not in report.get("snapshot_ids", [])
    ):
        raise ValueError("task_requires_complete_verified_source")
    adapter.restore(snapshot, target)
    if not adapter.verify(snapshot, target)["ok"]:
        raise ValueError("initial_environment_invalid")
    if verify_task(task.verifier, target):
        raise ValueError("task_already_satisfied")
    reference_trace = execute_actions(task.reference_actions, target, task.allowed_tools)
    if not verify_task(task.verifier, target):
        raise ValueError("reference_failed")
    adapter.reset(snapshot, target)
    execute_actions(task.negative_actions, target, task.allowed_tools)
    if verify_task(task.verifier, target):
        raise ValueError("negative_control_passed")
    for _ in range(2):
        adapter.reset(snapshot, target)
        if not adapter.verify(snapshot, target)["ok"] or verify_task(task.verifier, target):
            raise ValueError("reset_not_repeatable")
    lineage = {
        "run_id": uuid.uuid4().hex,
        "source_run_id": task.source_run_id,
        "source_revision": task.source_revision,
        "source_family": task.source_family,
        "snapshot_id": task.snapshot_id,
        "task_id": task.task_id,
        "split": task.split,
    }
    execution_trace = None
    success = None
    if runner:
        # Verifier and reference actions stay outside the agent's task input.
        execution_trace = runner(
            instruction=task.instruction,
            root=target,
            allowed_tools=task.allowed_tools,
            lineage=lineage,
        )
        if inspect.isawaitable(execution_trace):
            if inspect.iscoroutine(execution_trace):
                execution_trace.close()
            raise ValueError("async_runner_requires_execute_task")
        success = verify_task(task.verifier, target)
    return {
        "task": dataclasses.asdict(task),
        "lineage": lineage,
        "admitted": True,
        "reference_trajectory": reference_trace,
        "execution_trajectory": execution_trace,
        "execution_success": success,
    }


async def execute_task(task: TaskSpec, bundle: dict, snapshot: dict, adapter, target: Path, runner):
    """Run admission before invoking an async OpenSquilla worker on an isolated reset state."""
    admitted = await asyncio.to_thread(admit_task, task, bundle, snapshot, adapter, target)
    trajectory = runner(
        instruction=task.instruction,
        root=target,
        allowed_tools=task.allowed_tools,
        lineage=admitted["lineage"],
    )
    if inspect.isawaitable(trajectory):
        trajectory = await trajectory
    admitted["execution_trajectory"] = trajectory
    admitted["execution_success"] = await asyncio.to_thread(verify_task, task.verifier, target)
    return admitted


class OpenSquillaTaskRunner:
    """Agent factory supplies provider credentials and a tool registry scoped to root.

    factory(root=..., allowed_tools=..., lineage=...) returns an Agent (or awaitable).
    The independent verifier and reference actions are never supplied to that factory.
    """

    def __init__(self, factory):
        self.factory = factory

    async def __call__(self, *, instruction, root, allowed_tools, lineage):
        from .capture import context, serializable

        agent = self.factory(root=root, allowed_tools=allowed_tools, lineage=lineage)
        if inspect.isawaitable(agent):
            agent = await agent
        token = context.set(
            {
                "_scheduled_run_id": lineage["run_id"],
                "trace_id": lineage["run_id"],
                "turn_id": lineage["run_id"],
                "_lineage": lineage,
            }
        )
        stream = agent.run_turn(instruction)
        try:
            return [serializable(event) async for event in stream]
        finally:
            await stream.aclose()
            context.reset(token)


def generate_file_tasks(
    bundle: dict, run_id: str, snapshot: dict, *, count: int = 3, source_family: str | None = None
) -> list[TaskSpec]:
    """Executable seed generator; production generators can emit the same TaskSpec contract."""
    if not 1 <= count <= 100:
        raise ValueError("invalid_task_count")
    family = source_family or snapshot["snapshot_id"]
    tasks = []
    for index in range(count):
        path = f"generated/result-{index + 1}.json"
        result = {"variant": index + 1, "source_files": len(snapshot["entries"])}
        text = json.dumps(result, sort_keys=True) + "\n"
        task_id = digest(canonical([family, index, result]))[:32]
        tasks.append(
            TaskSpec(
                task_id,
                f"Create {path} containing exactly this JSON object: {json.dumps(result)}",
                run_id,
                bundle["revision"],
                family,
                snapshot["snapshot_id"],
                ("file_write",),
                {"kind": "json_equals", "path": path, "value": result},
                ({"tool": "file_write", "path": path, "content": text},),
                ({"tool": "file_write", "path": path, "content": "{}"},),
            )
        )
    return tasks
