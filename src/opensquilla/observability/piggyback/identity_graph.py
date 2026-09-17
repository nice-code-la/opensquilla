"""Deterministic execution graphs and finite-scope closure from ID declarations alone."""

from __future__ import annotations

import heapq
from collections import defaultdict

from .identity import AUXILIARY, CONSTANTS, LOOP, PROFILE, kind, parts, references, validate
from .protocol import ProtocolError, canonical, digest

# Membership and closure references are not execution edges. In particular,
# dispatch/completion and closure/part references intentionally form reference cycles.
CAUSAL = {
    "turn": ("previous_id", "input_id"),
    "input_group": ("turn_ids",),
    "activation": ("input_group_id", "trigger_id"),
    "iteration": ("previous_id", "phase_id"),
    "phase": ("trigger_id", "run_id"),
    "lane": ("phase_id", "fork_id"),
    "operation": ("trigger_id", "parent_id", "lane_id", "iteration_id"),
    "attempt": ("previous_id", "operation_id", "outer_id"),
    "call": ("previous_id", "retry_id", "fallback_id", "context_id", "attempt_id"),
    "context": ("message_ids", "selection_id", "definition_ids"),
    "message": ("source_ids",),
    "transform": ("input_ids",),
    "result": ("producer_id", "source_ids"),
    "acceptance": ("result_id",),
    "received": ("call_id",),
    "call_success": ("call_id",),
    "call_failure": ("call_id",),
    "call_interrupted": ("call_id",),
    "action": ("message_id",),
    "tool_attempt": ("action_id",),
    "tool_execution": ("attempt_id",),
    "tool_success": ("result_id",),
    "tool_failure": ("execution_id",),
    "tool_cancelled": ("execution_id",),
    "tool_rejection": ("attempt_id",),
    "dispatch": ("call_id", "tool_id", "run_id"),
    "run": ("dispatch_id", "activation_id"),
    "wait": ("result_ids",),
    "candidate_result": ("result_id",),
    "ensemble": ("operation_id",),
    "candidate": ("ensemble_id", "lane_id"),
    "projection": ("candidate_result_id",),
    "selection": ("projection_ids",),
    "aggregation": ("fallback_id", "selection_id"),
    "replacement": ("previous_id", "next_id"),
    "generation_output": ("generation_id", "result_id"),
    "generation": ("attempt_id",),
    "generation_discarded": ("generation_id",),
    "fallback_transition": ("from_attempt_id",),
    "attempt_success": ("attempt_id",),
    "attempt_failure": ("attempt_id",),
    "attempt_interrupted": ("attempt_id",),
    "commit": ("message_ids",),
    "delivery": ("message_ids",),
    "delivered": ("delivery_id",),
    "delivery_failed": ("delivery_id",),
}


def _order(vertices, edges):
    outgoing, degree = defaultdict(set), dict.fromkeys(vertices, 0)
    for source, target, _ in edges:
        if source in degree and target in degree and target not in outgoing[source]:
            outgoing[source].add(target)
            degree[target] += 1
    ready = [node for node in degree if not degree[node]]
    heapq.heapify(ready)
    order = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for child in sorted(outgoing[node]):
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(ready, child)
    return order, sorted(node for node in degree if degree[node])


class ExecutionIdReconstructor:
    def reconstruct(self, declarations):
        from .identity_facts import FACT_SPECS, fact_key, to_declaration, to_fact

        variants, invalid = defaultdict(dict), set()
        invalid_ids, invalid_scopes = set(), set()
        for value in declarations:
            try:
                value = to_declaration(value)
                validate(value)
                variants[value["id"]][canonical(value)] = value
            except (ProtocolError, TypeError, ValueError):
                invalid.add(digest(canonical(value)))
                if isinstance(value, dict):
                    for name, collection in (("id", invalid_ids), ("scope_id", invalid_scopes)):
                        try:
                            parts(value.get(name))
                            collection.add(value[name])
                        except ProtocolError:
                            pass
        conflicts = sorted(key for key, values in variants.items() if len(values) != 1)
        nodes = {
            key: next(iter(values.values()))
            for key, values in sorted(variants.items())
            if len(values) == 1
        }
        # Detached copies: callers cannot mutate the graph by retaining input dictionaries.
        import json

        nodes = json.loads(canonical(nodes))
        issues, missing, edges = set(), set(), set()
        by_type, by_scope, refs = defaultdict(list), defaultdict(set), {}
        for node_id, row in nodes.items():
            typ = kind(node_id)
            by_type[typ].append(node_id)
            if row["scope_id"]:
                by_scope[row["scope_id"]].add(node_id)
            refs[node_id] = list(references(row))
            for field, target in refs[node_id]:
                if target not in nodes and target not in CONSTANTS and target not in conflicts:
                    missing.add(target)
                if field in CAUSAL.get(typ, ()):
                    edges.add((target, node_id, f"{typ}.{field}"))
            if typ == "phase" and row["profile_id"] not in {LOOP, AUXILIARY}:
                issues.add(("unknown_profile", node_id))

        facts = {i: to_fact(row) for i, row in nodes.items() if kind(i) in FACT_SPECS}
        fact_index = defaultdict(list)
        for identity, fact in facts.items():
            fact_index[fact_key(fact)].append(identity)
        for identities in fact_index.values():
            if len(identities) > 1:
                for identity in identities:
                    issues.add(("conflicting_lifecycle_fact", identity))

        def resolve(identity, field):
            return nodes.get(identity, {}).get(field)

        def chain(call):
            attempt = resolve(call, "attempt_id")
            operation = resolve(attempt, "operation_id")
            lane = resolve(operation, "lane_id")
            phase = resolve(lane, "phase_id")
            run = resolve(phase, "run_id")
            branch = resolve(run, "branch_id")
            return {
                "attempt_id": attempt,
                "operation_id": operation,
                "lane_id": lane,
                "phase_id": phase,
                "run_id": run,
                "branch_id": branch,
                "session_id": resolve(branch, "session_id"),
                "iteration_id": resolve(operation, "iteration_id"),
                "activation_id": resolve(run, "activation_id"),
            }

        positions = {}
        serial = defaultdict(list)
        for typ, owner_field in (
            ("turn", "branch_id"),
            ("iteration", "phase_id"),
            ("attempt", "operation_id"),
        ):
            for node_id in by_type[typ]:
                row = nodes[node_id]
                previous = row["previous_id"]
                if (
                    previous
                    and previous in nodes
                    and nodes[previous][owner_field] != row[owner_field]
                ):
                    issues.add(("wrong_predecessor_owner", node_id))
                serial[(typ, row[owner_field], previous)].append(node_id)
        for call in by_type["call"]:
            position = positions[call] = chain(call)
            row = nodes[call]
            if position["run_id"] is not None and row["scope_id"] != position["run_id"]:
                issues.add(("wrong_call_scope", call))
            phase = nodes.get(position["phase_id"], {})
            iteration = position["iteration_id"]
            if phase.get("profile_id") == LOOP and not iteration:
                issues.add(("missing_iteration", call))
            if (
                position["phase_id"]
                and iteration in nodes
                and nodes[iteration]["phase_id"] != position["phase_id"]
            ):
                issues.add(("wrong_iteration_phase", call))
            previous = row["previous_id"]
            serial[("call", position["lane_id"], previous)].append(call)
            for field in ("previous_id", "retry_id", "fallback_id"):
                previous = row[field]
                if previous in nodes:
                    prior = chain(previous)
                    expected = "lane_id" if field == "previous_id" else "operation_id"
                    if (
                        prior[expected] is not None
                        and position[expected] is not None
                        and prior[expected] != position[expected]
                    ):
                        issues.add(("wrong_" + field, call))
            if row["fallback_id"] and row["fallback_id"] != row["retry_id"]:
                issues.add(("fallback_without_retry", call))
        for (typ, owner, _), children in serial.items():
            if owner and len(children) > 1:
                issues.update(("serial_fork_" + typ, child) for child in children)

        # A dispatch's trigger must agree with the immutable tool action. This
        # redundant relationship catches a missing or stale caller even when
        # every declaration named by the closure is present.
        for dispatch in by_type["dispatch"]:
            row = nodes[dispatch]
            tool = row["tool_id"]
            if not tool:
                continue
            attempt = resolve(tool, "attempt_id")
            owner = resolve(attempt, "run_id")
            if owner and owner != row["run_id"]:
                issues.add(("wrong_dispatch_tool_owner", dispatch))
            action = resolve(attempt, "action_id")
            message = resolve(action, "message_id")
            origins = set()
            for source in resolve(message, "source_ids") or ():
                if kind(source) == "acceptance":
                    result = resolve(source, "result_id")
                    producer = resolve(result, "producer_id")
                    if producer in nodes and kind(producer) == "call":
                        origins.add(producer)
            if origins and origins != {row["call_id"]}:
                issues.add(("wrong_dispatch_call_origin", dispatch))

        terminal_calls, terminal_tools, terminal_runs = (
            defaultdict(list),
            defaultdict(list),
            defaultdict(list),
        )
        received_calls = defaultdict(list)
        for terminal in by_type["received"]:
            received_calls[nodes[terminal]["call_id"]].append(terminal)
        for typ in ("call_success", "call_failure", "call_interrupted"):
            for terminal in by_type[typ]:
                terminal_calls[nodes[terminal]["call_id"]].append(terminal)
        for typ in ("tool_success", "tool_failure", "tool_cancelled"):
            for terminal in by_type[typ]:
                terminal_tools[nodes[terminal]["execution_id"]].append(terminal)
        for typ in ("run_success", "run_failure", "run_cancelled"):
            for terminal in by_type[typ]:
                terminal_runs[nodes[terminal]["run_id"]].append(terminal)
        for table in (terminal_calls, terminal_tools, terminal_runs, received_calls):
            for target, terminals in table.items():
                if len(terminals) > 1:
                    issues.add(("conflicting_terminal", target))

        # Every execution owner's scope is checked, even for nodes with no Call.
        scope_owners = {
            "phase": "run_id",
            "lane": "phase_id",
            "iteration": "phase_id",
            "operation": "lane_id",
            "attempt": "operation_id",
            "tool_attempt": "run_id",
            "tool_execution": "attempt_id",
            "candidate": "lane_id",
            "ensemble": "operation_id",
            "aggregation": "operation_id",
            "generation": "attempt_id",
            "closure": "run_id",
            "closure_part": "closure_id",
            "acceptance": "operation_id",
        }
        for typ, field in scope_owners.items():
            for identity in by_type[typ]:
                row = nodes[identity]
                owner = nodes.get(row[field])
                if owner and row["scope_id"] != (
                    owner["id"] if kind(owner["id"]) == "run" else owner["scope_id"]
                ):
                    issues.add(("wrong_owner_scope", identity))
        for call, position in positions.items():
            if (
                resolve(call, "context_id") in nodes
                and position["run_id"] is not None
                and resolve(resolve(call, "context_id"), "scope_id") != position["run_id"]
            ):
                issues.add(("wrong_context_scope", call))
        for typ in ("candidate", "aggregation"):
            for identity in by_type[typ]:
                row = nodes[identity]
                if resolve(row["ensemble_id"], "scope_id") != row["scope_id"]:
                    issues.add(("wrong_ensemble_scope", identity))
        for selection in by_type["selection"]:
            row = nodes[selection]
            for projection in row["projection_ids"]:
                candidate_result = resolve(projection, "candidate_result_id")
                candidate = resolve(candidate_result, "candidate_id")
                if candidate in nodes and resolve(candidate, "ensemble_id") != row["ensemble_id"]:
                    if not any(
                        resolve(reuse, "selection_id") == selection
                        and resolve(reuse, "source_ensemble_id")
                        == resolve(candidate, "ensemble_id")
                        for reuse in by_type["selection_reuse"]
                    ):
                        issues.add(("wrong_selection_ensemble", selection))
        attempts_terminal = defaultdict(list)
        for typ in ("attempt_success", "attempt_failure", "attempt_interrupted"):
            for identity in by_type[typ]:
                attempts_terminal[nodes[identity]["attempt_id"]].append(identity)
        for attempt, values in attempts_terminal.items():
            if len(values) > 1:
                issues.add(("conflicting_attempt_terminal", attempt))
        deliveries_terminal = defaultdict(list)
        for typ in ("delivered", "delivery_failed"):
            for identity in by_type[typ]:
                deliveries_terminal[nodes[identity]["delivery_id"]].append(identity)
        for delivery, values in deliveries_terminal.items():
            if len(values) > 1:
                issues.add(("conflicting_delivery_terminal", delivery))
        discarded = {nodes[i]["generation_id"] for i in by_type["generation_discarded"]}
        discarded_results = {
            nodes[i]["result_id"]
            for i in by_type["generation_output"]
            if nodes[i]["generation_id"] in discarded
        }
        for acceptance in by_type["acceptance"]:
            row = nodes[acceptance]
            result = row["result_id"]
            producer = resolve(result, "producer_id")
            call_attempt = (
                resolve(producer, "attempt_id") if producer and kind(producer) == "call" else None
            )
            if result in discarded_results or any(
                kind(i) != "attempt_success" for i in attempts_terminal[call_attempt]
            ):
                issues.add(("accepted_unsuccessful_result", acceptance))
        for candidate in by_type["candidate"]:
            terminals = [
                i
                for typ in ("candidate_result", "candidate_failure", "candidate_cancelled")
                for i in by_type[typ]
                if nodes[i]["candidate_id"] == candidate
            ]
            if len(terminals) > 1:
                issues.add(("conflicting_candidate_terminal", candidate))

        order, cyclic = _order(nodes, edges)
        issues.update(("causal_cycle", node) for node in cyclic)
        closures = defaultdict(list)
        for closure in by_type["closure"]:
            closures[nodes[closure]["run_id"]].append(closure)
        for run, values in closures.items():
            if len(values) > 1:
                issues.add(("multiple_closures", run))

        reports = {}
        visiting = set()

        def check_scope(run):
            if run in reports:
                return reports[run]
            if run in visiting:
                return {"complete": False, "state": "conflict", "reasons": ["closure_cycle"]}
            visiting.add(run)
            reasons, required = set(), set()
            values = closures[run]
            closure = nodes[values[0]] if len(values) == 1 else None
            if not closure:
                reasons.add("closure_missing" if not values else "multiple_closures")
            else:
                if closure["profile_id"] != PROFILE:
                    reasons.add("unknown_profile")
                if (
                    len(closure["part_ids"]) != len(set(closure["part_ids"]))
                    or not closure["part_ids"]
                ):
                    reasons.add("invalid_parts")
                for part in closure["part_ids"]:
                    if part not in nodes:
                        reasons.add("part_missing")
                    elif nodes[part]["closure_id"] != closure["id"]:
                        reasons.add("wrong_part_owner")
                    else:
                        required.update(nodes[part]["required_ids"])
                actual = {i for i in by_scope[run] if kind(i) not in {"closure", "closure_part"}}
                if not actual <= required or run not in required:
                    reasons.add("inventory_mismatch")
                if closure["gap_ids"]:
                    reasons.add("capture_gap")
                for child in closure["child_ids"]:
                    if child not in nodes:
                        reasons.add("child_closure_missing")
                    elif not check_scope(nodes[child]["run_id"])["complete"]:
                        reasons.add("child_incomplete")
            # Walk references, not event logs. Reference cycles in scope metadata are benign.
            todo, reached = list(required or by_scope[run]), set()
            if closure:
                todo.append(closure["id"])
            while todo:
                identity = todo.pop()
                if identity in reached or identity in CONSTANTS:
                    continue
                reached.add(identity)
                if identity in conflicts:
                    reasons.add("identity_conflict")
                elif identity not in nodes:
                    reasons.add("declaration_missing")
                else:
                    node = nodes[identity]
                    if kind(identity) == "gap":
                        reasons.add("capture_gap")
                    if kind(identity) == "dispatch_completion":
                        child = node["child_closure_id"]
                        if bool(child) == bool(node["cancelled_id"]):
                            reasons.add("dispatch_completion_invalid")
                        elif (
                            child in nodes
                            and resolve(node["dispatch_id"], "run_id") == run
                            and not check_scope(nodes[child]["run_id"])["complete"]
                        ):
                            reasons.add("child_incomplete")
                    todo.extend(
                        target
                        for field, target in refs.get(identity, ())
                        if target not in reached
                        and not (
                            kind(identity) == "dispatch"
                            and node["run_id"] != run
                            and field == "completion_id"
                        )
                    )
            if any(target in reached or target == run for _, target in issues):
                reasons.add("invalid_graph")
            if len(terminal_runs[run]) != 1:
                reasons.add("run_not_terminal")
            for identity in by_scope[run]:
                typ = kind(identity)
                if typ == "call" and len(terminal_calls[identity]) != 1:
                    reasons.add("call_not_terminal")
                if (
                    typ == "call"
                    and any(kind(t) == "call_success" for t in terminal_calls[identity])
                    and len(received_calls[identity]) != 1
                ):
                    # A complete response claim without response_received evidence.
                    reasons.add("call_not_received")
                if typ == "attempt" and len(attempts_terminal[identity]) != 1:
                    reasons.add("attempt_not_terminal")
                if typ == "delivery" and len(deliveries_terminal[identity]) != 1:
                    reasons.add("delivery_not_terminal")
                if typ == "tool_execution" and len(terminal_tools[identity]) != 1:
                    reasons.add("tool_not_terminal")
                if typ == "candidate" and not any(
                    nodes[i]["candidate_id"] == identity
                    for terminal in ("candidate_result", "candidate_failure", "candidate_cancelled")
                    for i in by_type[terminal]
                ):
                    reasons.add("candidate_not_terminal")
                if typ == "tool_attempt":
                    children = [
                        i for i in by_type["tool_execution"] if nodes[i]["attempt_id"] == identity
                    ]
                    rejects = [
                        i for i in by_type["tool_rejection"] if nodes[i]["attempt_id"] == identity
                    ]
                    if len(children) + len(rejects) != 1:
                        reasons.add("tool_attempt_not_resolved")
            if reached & invalid_ids or run in invalid_scopes:
                reasons.add("invalid_declarations")
            # A missing obligation is only "pending" while declarations can still
            # arrive. Once the closure and every listed member are present, an absent
            # terminal was never produced: producer omission, reported as incomplete.
            omissions = {
                "run_not_terminal",
                "call_not_terminal",
                "call_not_received",
                "tool_not_terminal",
                "attempt_not_terminal",
                "delivery_not_terminal",
                "candidate_not_terminal",
                "tool_attempt_not_resolved",
            }
            report = {
                "complete": not reasons,
                "state": "complete"
                if not reasons
                else "pending"
                if reasons
                <= {
                    "closure_missing",
                    "part_missing",
                    "declaration_missing",
                    "child_closure_missing",
                    "child_incomplete",
                    *(omissions if not closure or missing else set()),
                }
                else "incomplete",
                "reasons": sorted(reasons),
                "closure_id": closure["id"] if closure else None,
            }
            reports[run] = report
            visiting.remove(run)
            return report

        for run in by_type["run"]:
            check_scope(run)
        position_status = {}
        for call, position in positions.items():
            relevant = {call, *(value for value in position.values() if value)}
            position_status[call] = (
                "conflict"
                if any(target in relevant for _, target in issues)
                else "unique"
                if all(value in nodes for key, value in position.items() if key != "iteration_id")
                else "pending"
            )
        sessions = {}
        for position in positions.values():
            group_id = resolve(position["activation_id"], "input_group_id")
            input_turns = resolve(group_id, "turn_ids") or []
            position["input_group_id"] = group_id
            position["owner_user_turn_id"] = input_turns[0] if input_turns else None
            position["input_turn_ids"] = list(input_turns)
        for session in by_type["session"]:
            branches = {}
            for branch in by_type["branch"]:
                if resolve(branch, "session_id") != session:
                    continue
                turns = [
                    i for i in order if kind(i) == "turn" and resolve(i, "branch_id") == branch
                ]
                runs = [i for i in order if kind(i) == "run" and resolve(i, "branch_id") == branch]
                branches[branch] = {"user_turn_ids": turns, "run_ids": runs}
            sessions[session] = {"branches": branches, "complete": None}
        result = {
            "schema": "execution-id-graph-v3",
            "validation_profile": "opensquilla-strict-20260917",
            "nodes": nodes,
            "positions": positions,
            "edges": [list(edge) for edge in sorted(edges)],
            "topological_order": order,
            "missing_ids": sorted(missing),
            "conflicting_ids": conflicts,
            "invalid_declarations": sorted(invalid),
            "issues": [list(i) for i in sorted(issues)],
            "references_closed": not (missing or conflicts or invalid or issues),
            "scopes": dict(sorted(reports.items())),
            "session_complete": None,
            "position_status": position_status,
            "sessions": sessions,
        }
        result["view_schema"] = "execution-call-locator-v1"
        result["entities"] = {i: row for i, row in nodes.items() if i not in facts}
        result["facts"] = facts
        result["fact_index"] = dict(sorted(fact_index.items()))
        result["call_locators"] = {}
        result["call_relations"] = {}
        for call, position in positions.items():
            row = nodes[call]
            result["call_locators"][call] = {
                "session_id": position["session_id"],
                "user_turn_id": position["owner_user_turn_id"],
                "run_id": position["run_id"],
                "iteration_id": position["iteration_id"],
                "operation_id": position["operation_id"],
                "call_id": call,
                "context_id": row["context_id"],
            }
            # An unresolved input group or context must not be hidden by a valid owner chain.
            required = [row["context_id"], position["input_group_id"], *position["input_turn_ids"]]
            location_status = position_status[call]
            if any(i in conflicts or i in invalid_ids for i in required):
                location_status = "conflict"
            elif location_status == "unique" and any(i not in nodes for i in required):
                location_status = "pending"
            result["call_relations"][call] = {
                "previous_call_id": row["previous_id"],
                "retry_of_call_id": row["retry_id"],
                "fallback_of_call_id": row["fallback_id"],
                "activation_turn_ids": position["input_turn_ids"],
                "branch_id": position["branch_id"],
                "phase_id": position["phase_id"],
                "lane_id": position["lane_id"],
                "attempt_id": position["attempt_id"],
                "location_status": location_status,
            }
        result["revision"] = digest(canonical(result))
        return result
