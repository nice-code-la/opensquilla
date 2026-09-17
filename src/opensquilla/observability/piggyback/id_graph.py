"""Call topology from identity fields alone. No event, content or clock inputs.

References to calls are qualified as ``source_id:call_id``. A source is an
authenticated ingestion namespace, not a tenant claim supplied by a client.
"""

from __future__ import annotations

import heapq
from collections import defaultdict

from .protocol import ProtocolError, canonical, digest, identifier

SCALARS = (
    "source_id",
    "session_id",
    "user_turn_id",
    "previous_user_turn_id",
    "trace_id",
    "run_id",
    "parent_run_id",
    "iteration_id",
    "previous_iteration_id",
    "logical_call_id",
    "outer_attempt_id",
    "call_id",
    "context_id",
    "previous_call_id",
    "parent_call_id",
    "retry_of_call_id",
)
ARRAYS = ("join_call_ids", "context_parent_call_ids", "input_turn_ids")
PATHS = ("run_path_ids",)
V2_SCALARS = ("phase_id", "auxiliary_phase_id", "system_activation_id", "dispatch_id")
V2_ARRAYS = ("waited_call_ids", "unknown_context_call_ids")
V2_ORDERED = ("context_message_ids",)
V2_LOCAL_ARRAYS = ("unknown_message_ids",)
REQUIRED = (
    "source_id",
    "session_id",
    "run_id",
    "logical_call_id",
    "call_id",
    "context_id",
    "trace_id",
    "outer_attempt_id",
)
CALL_RELATIONS = {
    "previous_call_id": "continues",
    "parent_call_id": "spawned_by",
    "retry_of_call_id": "retry_of",
    "join_call_ids": "joins",
    "context_parent_call_ids": "context_from",
}
TURN_FIELDS = {"version", "source_id", "session_id", "user_turn_id", "previous_user_turn_id"}


def validate_turn_ids(value):
    if (
        not isinstance(value, dict)
        or set(value) != TURN_FIELDS
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ProtocolError("turn_ids_shape")
    for name in ("source_id", "session_id", "user_turn_id"):
        identifier(value[name])
    if ":" in value["source_id"]:
        raise ProtocolError("turn_ids_source")
    if value["previous_user_turn_id"] is not None:
        identifier(value["previous_user_turn_id"])
    return value


def call_key(source_id: str, call_id: str) -> str:
    return f"{identifier(source_id)}:{identifier(call_id)}"


def validate_call_ids(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] not in {1, 2}
    ):
        raise ProtocolError("call_ids_version")
    modern = value["version"] == 2
    extra = (
        {
            *V2_SCALARS,
            *V2_ARRAYS,
            *V2_ORDERED,
            *V2_LOCAL_ARRAYS,
            "turn_declarations",
            "run_declarations",
        }
        if modern
        else set()
    )
    if set(value) != {"version", *SCALARS, *ARRAYS, *PATHS, *extra}:
        raise ProtocolError("call_ids_fields")
    for name in (*SCALARS, *(V2_SCALARS if modern else ())):
        if value[name] is not None:
            identifier(value[name])
    for name in REQUIRED:
        identifier(value[name])
    if ":" in value["source_id"]:
        raise ProtocolError("call_ids_source_namespace")
    for name in (*ARRAYS, *PATHS, *(V2_ARRAYS + V2_ORDERED + V2_LOCAL_ARRAYS if modern else ())):
        values = value[name]
        if not isinstance(values, list) or len(values) > 1024:
            raise ProtocolError("call_ids_array")
        for item in values:
            identifier(item)
        if name in (*ARRAYS, *V2_ARRAYS, *V2_LOCAL_ARRAYS) and values != sorted(set(values)):
            raise ProtocolError("call_ids_noncanonical_set")
    path = value["run_path_ids"]
    if not path or len(set(path)) != len(path) or path[-1] != value["run_id"]:
        raise ProtocolError("call_ids_run_path")
    if (path[-2] if len(path) > 1 else None) != value["parent_run_id"]:
        raise ProtocolError("call_ids_parent_path")
    for name in CALL_RELATIONS:
        refs = value[name] if name in ARRAYS else [value[name]]
        for ref in refs:
            if ref and ":" not in ref:
                raise ProtocolError("call_ids_unqualified_reference")
    if modern:
        identifier(value["phase_id"])
        if value["auxiliary_phase_id"] not in {None, value["phase_id"]}:
            raise ProtocolError("call_ids_auxiliary_phase")
        for name in V2_ARRAYS:
            for ref in value[name]:
                if ":" not in ref:
                    raise ProtocolError("call_ids_unqualified_reference")
        declarations = value["turn_declarations"]
        if not isinstance(declarations, list) or len(declarations) > 1024:
            raise ProtocolError("call_ids_turn_declarations")
        seen = set()
        for row in declarations:
            if not isinstance(row, dict) or set(row) != {
                "user_turn_id",
                "session_id",
                "previous_user_turn_id",
            }:
                raise ProtocolError("call_ids_turn_declaration")
            identifier(row["user_turn_id"])
            identifier(row["session_id"])
            if row["previous_user_turn_id"] is not None:
                identifier(row["previous_user_turn_id"])
            if row["user_turn_id"] in seen:
                raise ProtocolError("call_ids_duplicate_turn_declaration")
            seen.add(row["user_turn_id"])
        declarations = value["run_declarations"]
        if not isinstance(declarations, list) or len(declarations) > 1024:
            raise ProtocolError("call_ids_run_declarations")
        seen = set()
        for row in declarations:
            if not isinstance(row, dict) or set(row) != {
                "run_id",
                "parent_run_id",
                "session_id",
                "user_turn_id",
            }:
                raise ProtocolError("call_ids_run_declaration")
            for name in ("run_id", "session_id"):
                identifier(row[name])
            for name in ("parent_run_id", "user_turn_id"):
                if row[name] is not None:
                    identifier(row[name])
            if row["run_id"] in seen:
                raise ProtocolError("call_ids_duplicate_run_declaration")
            seen.add(row["run_id"])
    return value


class IdTraceReconstructor:
    """Build membership and typed Call edges using only the versioned ID contract."""

    def reconstruct(self, identities: list[dict]) -> dict:
        nodes, conflicts, invalid = {}, set(), set()
        standalone_turns = []
        for value in identities:
            try:
                if isinstance(value, dict) and set(value) == TURN_FIELDS:
                    standalone_turns.append(validate_turn_ids(value))
                    continue
                validate_call_ids(value)
            except (ProtocolError, TypeError, ValueError):
                invalid.add(digest(canonical(value)))
                continue
            key = call_key(value["source_id"], value["call_id"])
            if key in nodes and canonical(nodes[key]) != canonical(value):
                conflicts.add(key)
            else:
                nodes[key] = {
                    name: list(item) if isinstance(item, list) else item
                    for name, item in value.items()
                }
        for key in conflicts:
            nodes.pop(key, None)

        edges, missing, issues = set(), set(), set()
        runs, turns, sessions, iterations, contexts = {}, {}, {}, {}, {}
        traces, attempts, phases, operation_owners = {}, {}, {}, {}

        # A scope declaration must agree on all calls, independent of arrival order.
        def declare(table, key, value):
            if key in table and table[key] != value:
                issues.add(("scope_conflict", key))
            else:
                table[key] = value

        # Collect scope declarations before checking references: a declaration
        # in a later Call or a backlog-only Turn must resolve identically.
        for row in sorted(standalone_turns, key=canonical):
            declare(
                turns,
                call_key(row["source_id"], row["user_turn_id"]),
                (
                    call_key(row["source_id"], row["session_id"]),
                    row["previous_user_turn_id"],
                ),
            )
        for _, node in sorted(nodes.items()):
            source = node["source_id"]
            for row in node.get("turn_declarations", []):
                declare(
                    turns,
                    call_key(source, row["user_turn_id"]),
                    (
                        call_key(source, row["session_id"]),
                        row["previous_user_turn_id"],
                    ),
                )
            for row in node.get("run_declarations", []):
                declare(
                    runs,
                    call_key(source, row["run_id"]),
                    (
                        call_key(source, row["session_id"]),
                        call_key(source, row["user_turn_id"]) if row["user_turn_id"] else None,
                        row["parent_run_id"],
                    ),
                )

        for key, node in sorted(nodes.items()):
            source = node["source_id"]
            run = call_key(source, node["run_id"])
            session = call_key(source, node["session_id"])
            declare(traces, run, node["trace_id"])
            declare(
                operation_owners,
                call_key(source, node["logical_call_id"]),
                (
                    run,
                    node["iteration_id"],
                    node.get("phase_id"),
                ),
            )
            if node["version"] == 2:
                declare(
                    phases,
                    call_key(source, node["phase_id"]),
                    (
                        run,
                        node["auxiliary_phase_id"],
                    ),
                )
            declare(
                attempts,
                call_key(source, node["outer_attempt_id"]),
                (run, node["logical_call_id"], node["iteration_id"]),
            )
            if (
                not node["user_turn_id"]
                and not node.get("system_activation_id")
                or not node["iteration_id"]
                and not node.get("auxiliary_phase_id")
            ):
                issues.add(("legacy_position_incomplete", key))
            for declaration in node.get("turn_declarations", []):
                declare(
                    turns,
                    call_key(source, declaration["user_turn_id"]),
                    (
                        call_key(source, declaration["session_id"]),
                        declaration["previous_user_turn_id"],
                    ),
                )
            turn = call_key(source, node["user_turn_id"]) if node["user_turn_id"] else None
            if node["version"] == 1:
                for i, ancestor in enumerate(node["run_path_ids"]):
                    parent = node["run_path_ids"][i - 1] if i else None
                    declare(runs, call_key(source, ancestor), (session, turn, parent))
            else:
                for row in node["run_declarations"]:
                    declare(
                        runs,
                        call_key(source, row["run_id"]),
                        (
                            call_key(source, row["session_id"]),
                            call_key(source, row["user_turn_id"]) if row["user_turn_id"] else None,
                            row["parent_run_id"],
                        ),
                    )
                declare(runs, run, (session, turn, node["parent_run_id"]))
                for i, ancestor in enumerate(node["run_path_ids"]):
                    ancestor_key = call_key(source, ancestor)
                    if ancestor_key not in runs:
                        missing.add(ancestor_key)
                    elif runs[ancestor_key][2] != (node["run_path_ids"][i - 1] if i else None):
                        issues.add(("wrong_declared_run_path", key))
            if turn and node["version"] == 1:
                declare(turns, turn, (session, node["previous_user_turn_id"]))
            elif turn:
                if turn not in turns:
                    missing.add(turn)
                elif turns[turn][1] != node["previous_user_turn_id"]:
                    issues.add(("wrong_declared_previous_turn", key))
            if node["iteration_id"]:
                declare(
                    iterations,
                    call_key(source, node["iteration_id"]),
                    (run, node["previous_iteration_id"]),
                )
            context = call_key(source, node["context_id"])
            declare(contexts, context, (key, tuple(node["context_parent_call_ids"])))
            sessions.setdefault(session, set()).add(key)
            for field, relation in CALL_RELATIONS.items():
                refs = node[field] if field in ARRAYS else [node[field]]
                for ref in refs:
                    if not ref:
                        continue
                    edges.add((ref, key, relation))
                    if ref not in nodes:
                        missing.add(ref)
                        continue
                    predecessor = nodes[ref]
                    if ref == key:
                        issues.add(("self_reference", key))
                    if relation in {"continues", "retry_of"} and (
                        predecessor["source_id"],
                        predecessor["run_id"],
                        predecessor.get("phase_id"),
                    ) != (source, node["run_id"], node.get("phase_id")):
                        issues.add(("wrong_run_predecessor", key))
                    if relation == "retry_of" and (
                        predecessor["logical_call_id"] != node["logical_call_id"]
                        or predecessor["iteration_id"] != node["iteration_id"]
                        or node["previous_call_id"] != ref
                    ):
                        issues.add(("wrong_retry_operation", key))
                    if relation == "spawned_by" and (
                        predecessor["source_id"] != source
                        or predecessor["run_id"] != node["parent_run_id"]
                    ):
                        issues.add(("wrong_spawn_parent", key))
                    if relation == "joins" and predecessor["run_id"] == node["run_id"]:
                        issues.add(("join_requires_other_run", key))
            for ref in node.get("waited_call_ids", []):
                edges.add((ref, key, "waited_for"))
                if ref not in nodes:
                    missing.add(ref)
            for ref in node.get("unknown_context_call_ids", []):
                if ref not in nodes:
                    missing.add(ref)

        operations = defaultdict(list)
        iteration_calls = defaultdict(list)
        for key, node in nodes.items():
            operations[(node["source_id"], node["run_id"], node["logical_call_id"])].append(key)
            iteration_calls[(node["source_id"], node.get("phase_id"), node["iteration_id"])].append(
                key
            )
            for input_turn in node["input_turn_ids"]:
                ref = call_key(node["source_id"], input_turn)
                if ref not in turns:
                    missing.add(ref)
                elif node["version"] == 1 and turns[ref][0] != call_key(
                    node["source_id"], node["session_id"]
                ):
                    issues.add(("wrong_input_session", key))
        for group in operations.values():
            roots = [key for key in group if not nodes[key]["retry_of_call_id"]]
            if len(roots) > 1:
                issues.update(("unlinked_physical_attempts", key) for key in roots)
        for group in iteration_calls.values():
            roots = [key for key in group if nodes[key]["previous_call_id"] not in group]
            if len(roots) > 1:
                issues.update(("unlinked_iteration_calls", key) for key in roots)

        def scope_edges(table, relation, parent_position):
            result = []
            for key, values in sorted(table.items()):
                parent = values[parent_position]
                if parent:
                    source = key.split(":", 1)[0]
                    parent_key = call_key(source, parent)
                    result.append([parent_key, key, relation])
                    if parent_key not in table:
                        missing.add(parent_key)
                    elif relation != "child_run" and values[0] != table[parent_key][0]:
                        issues.add(("cross_scope_parent", key))
            return result

        turn_edges = scope_edges(turns, "next_user_turn", 1)
        iteration_edges = scope_edges(iterations, "next_iteration", 1)
        run_edges = scope_edges(runs, "child_run", 2)
        for links, label in (
            (turn_edges, "forked_turn_chain"),
            (iteration_edges, "forked_iteration_chain"),
            ([edge for edge in edges if edge[2] == "continues"], "forked_call_chain"),
        ):
            children = defaultdict(set)
            for left, right, _ in links:
                children[left].add(right)
            for left, values in children.items():
                if len(values) > 1:
                    issues.add((label, left))
        for table, name in ((turns, "unlinked_user_turns"), (iterations, "unlinked_iterations")):
            roots = defaultdict(list)
            for key, (owner, previous) in table.items():
                if previous is None:
                    roots[owner].append(key)
            for group in roots.values():
                if len(group) > 1:
                    issues.update((name, key) for key in group)

        # IDs-only scope cycles are invalid even if there is no corresponding Call edge.
        def ordered(keys, links):
            adjacency, degree = defaultdict(set), dict.fromkeys(keys, 0)
            for left, right, *_ in links:
                if left in degree and right in degree and right not in adjacency[left]:
                    adjacency[left].add(right)
                    degree[right] += 1
            queue = [key for key in degree if degree[key] == 0]
            heapq.heapify(queue)
            result = []
            while queue:
                key = heapq.heappop(queue)
                result.append(key)
                for child in sorted(adjacency[key]):
                    degree[child] -= 1
                    if degree[child] == 0:
                        heapq.heappush(queue, child)
            return result, sorted(set(keys) - set(result))

        order, cyclic = ordered(nodes, edges)
        for cycle_keys, cycle_edges in (
            (turns, turn_edges),
            (runs, run_edges),
            (iterations, iteration_edges),
        ):
            _, cycles = ordered(cycle_keys, cycle_edges)
            issues.update(("scope_cycle", key) for key in cycles)
        for key in cyclic:
            issues.add(("call_cycle", key))
        successor = defaultdict(set)
        for left, right, _ in edges:
            successor[left].add(right)
        for session, _ in turns.values():
            sessions.setdefault(session, set())
        result = {
            "schema": "call-id-graph-v2"
            if any(n["version"] == 2 for n in nodes.values())
            else "call-id-graph-v1",
            "calls": {
                key: {
                    "ids": nodes[key],
                    "successor_call_ids": sorted(successor[key]),
                    "provenance_coverage": (
                        "unknown"
                        if nodes[key]["version"] == 1
                        else "partial"
                        if (
                            nodes[key].get("unknown_context_call_ids")
                            or nodes[key].get("unknown_message_ids")
                        )
                        else "declared"
                    ),
                }
                for key in sorted(nodes)
            },
            "edges": [list(edge) for edge in sorted(edges)],
            "turn_edges": turn_edges,
            "turns": {
                key: {"session_id": owner, "previous_user_turn_id": previous}
                for key, (owner, previous) in sorted(turns.items())
            },
            "run_edges": run_edges,
            "iteration_edges": iteration_edges,
            "sessions": {key: sorted(values) for key, values in sorted(sessions.items())},
            "topological_order": order,
            "missing_ids": sorted(missing),
            "conflicting_call_ids": sorted(conflicts),
            "invalid_identity_hashes": sorted(invalid),
            "issues": [list(issue) for issue in sorted(issues)],
            "references_resolved": not (missing or conflicts or invalid or issues),
            "location_resolved": bool(nodes) and not (missing or conflicts or invalid or issues),
            # Forward references, zero-call turns and the final tail cannot be certified
            # from a finite collection of request identities alone.
            "session_complete": None,
        }
        result["revision"] = digest(canonical(result))
        return result
