"""Direct call and user-turn lookup, derived only from captured identities and links."""

from collections import defaultdict

from .protocol import canonical, digest

POSITION_KEYS = (
    "trace_id",
    "user_turn_id",
    "session_id",
    "run_id",
    "parent_run_id",
    "agent_id",
    "iteration_id",
    "iteration_index",
    "outer_attempt_id",
    "outer_attempt_index",
    "logical_call_id",
    "call_kind",
    "provider_execution_id",
    "ensemble_round_id",
    "candidate_id",
    "candidate_index",
    "sample_index",
    "aggregation_id",
    "selection_event_id",
    "role",
    "member_attempt_id",
    "member_attempt_index",
)


def build_call_index(events, model_contexts, edges):
    by_id = {e["event_id"]: e for e in events}
    parents = defaultdict(set)
    for left, right, relation in edges:
        if relation != "producer_order":
            parents[right].add(left)
    for e in events:
        parents[e["event_id"]].update(link["event_id"] for link in e.get("links", []))
    frontier, unresolved, ancestors, predecessors = {}, {}, {}, {}
    for e in events:
        event_id, ctx = e["event_id"], e["context"]
        previous, missing = set(), set()
        for parent in parents[event_id]:
            if parent not in frontier:
                missing.add(parent)
            else:
                previous.update(frontier[parent])
                missing.update(unresolved[parent])
        call = ctx.get("execution_id")
        if e["kind"] == "llm.request" and call:
            previous.discard(call)
            # Remove transitive ancestors included through coordinator-scope links.
            redundant = set().union(*(ancestors.get(p, set()) for p in previous))
            predecessors[call] = previous - redundant
            ancestors[call] = previous | redundant
        frontier[event_id] = (
            {call} if e["kind"] in {"llm.headers", "llm.chunk", "llm.end"} and call else previous
        )
        unresolved[event_id] = missing

    errors, index, snapshots = defaultdict(set), {}, {}
    for call, model in sorted(model_contexts.items()):
        observations = [by_id[k] for k in model["event_ids"]]
        requests = [e for e in observations if e["kind"] == "llm.request"]
        identity = (requests[0] if requests else observations[0])["context"]
        reasons = set()
        if len(requests) > 1:
            reasons.add("duplicate_call_identity")
        if sum(e["kind"] == "llm.end" for e in observations) > 1:
            reasons.add("multiple_call_terminals")
        for key in (*POSITION_KEYS, "call_id", "input_context_id"):
            if len({e["context"][key] for e in observations if key in e["context"]}) > 1:
                reasons.add("conflicting_call_position")
        input_id = identity.get("input_context_id")
        if requests and input_id and "request" in requests[0]["payload"]:
            snapshot = {"version": 1, "request": model["request"]}
            if digest(canonical(snapshot)) != input_id:
                reasons.add("input_context_mismatch")
            else:
                snapshots[input_id] = snapshot
        for e in observations:
            errors[e["context"].get("run_id", "unscoped")].update(reasons)
        request_id = requests[0]["event_id"] if len(requests) == 1 else None
        position = {k: identity.get(k) for k in POSITION_KEYS}
        position["call_id"] = call
        index[call] = {
            "call_id": call,
            "location": None if reasons else position,
            "position_status": "ambiguous"
            if reasons
            else "awaiting_request"
            if not requests
            else "located"
            if identity.get("run_id")
            else "unscoped",
            "identity_version": 1 if identity.get("call_id") else 0,
            "request_event_id": request_id,
            "input_context_id": input_id,
            "predecessor_call_ids": sorted(predecessors.get(call, set())) if not reasons else [],
            "successor_call_ids": [],
            "missing_causal_event_ids": sorted(unresolved.get(request_id, set())),
            "causality_status": "legacy-unverified"
            if not identity.get("call_id")
            else "incomplete"
            if reasons or not request_id or unresolved.get(request_id)
            else "resolved",
            "reasons": sorted(reasons),
        }
    for call, item in index.items():
        for previous in item["predecessor_call_ids"]:
            if previous in index:
                index[previous]["successor_call_ids"].append(call)
            if previous not in index or index[previous]["position_status"] in {
                "ambiguous",
                "awaiting_request",
            }:
                item["causality_status"] = "incomplete"

    groups, turns = defaultdict(list), {}
    for e in events:
        if turn := e["context"].get("user_turn_id"):
            groups[turn].append(e)
    for turn, rows in sorted(groups.items()):
        starts = [e for e in rows if e["kind"] == "user_turn.start"]
        ends = [e for e in rows if e["kind"] == "user_turn.end"]
        issues = []
        if len(starts) != 1:
            issues.append("missing_user_turn_start" if not starts else "conflicting_user_turn")
        if len(ends) != 1:
            issues.append("missing_user_turn_end" if not ends else "conflicting_user_turn")
        if len({e["context"].get("trace_id") for e in rows}) != 1:
            issues.append("conflicting_user_turn_trace")
        run_ids = sorted({e["context"]["run_id"] for e in rows if e["context"].get("run_id")})
        for run in run_ids:
            errors[run].update(issue for issue in issues if issue.startswith("conflicting_"))
        start = starts[0] if len(starts) == 1 else None
        turns[turn] = {
            "user_turn_id": turn,
            "trace_id": start["context"].get("trace_id") if start else None,
            "session_id": start["context"].get("session_id") if start else None,
            "input_event_id": start["event_id"] if start else None,
            "end_event_id": ends[0]["event_id"] if len(ends) == 1 else None,
            "input": start["payload"] if start else None,
            "root_run_id": start["context"].get("run_id") if start else None,
            "run_ids": run_ids,
            "call_ids": sorted(
                call
                for call, item in index.items()
                if (item["location"] or {}).get("user_turn_id") == turn
            ),
            "reasons": sorted(set(issues)),
        }
        if conflicts := {issue for issue in issues if issue.startswith("conflicting_")}:
            for call in turns[turn]["call_ids"]:
                index[call]["position_status"] = "ambiguous"
                index[call]["location"] = None
                index[call]["causality_status"] = "incomplete"
                index[call]["reasons"] = sorted(set(index[call]["reasons"]) | conflicts)
    return index, turns, snapshots, errors
