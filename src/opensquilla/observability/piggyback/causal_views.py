"""Deterministic loop/ensemble views; no identity inference from text or arrival order."""

from collections import defaultdict


def build_causal_views(events, model_contexts):
    by_id = {e["event_id"]: e for e in events}
    groups = {
        key: defaultdict(list)
        for key in (
            "iteration_id",
            "outer_attempt_id",
            "ensemble_round_id",
            "member_attempt_id",
        )
    }
    for event in events:
        for key, grouped in groups.items():
            if event["context"].get(key):
                grouped[event["context"][key]].append(event)
    errors = defaultdict(set)

    def payload(rows, kind):
        return next((e["payload"] for e in rows if e["kind"] == kind), {})

    def exists(rows, kind):
        return any(e["kind"] == kind for e in rows)

    def calls(key, identity):
        return sorted(
            call for call, value in model_contexts.items() if value["identity"].get(key) == identity
        )

    def check(rows, condition, reason):
        if not condition and rows:
            errors[rows[0]["context"].get("run_id", "unscoped")].add(reason)

    for event in events:
        required = []
        kind = event["kind"]
        if kind.startswith("loop."):
            required.append("iteration_id")
        if kind.startswith("loop.attempt."):
            required.append("outer_attempt_id")
        if kind.startswith("ensemble."):
            required.append("ensemble_round_id")
        if kind.startswith("ensemble.member."):
            required.extend(["member_attempt_id", "logical_call_id", "role"])
        if kind in {"ensemble.candidate.start", "ensemble.candidate.end"}:
            required.append("candidate_id")
        if kind == "ensemble.selection":
            required.append("aggregation_id")
        check([event], all(event["context"].get(k) for k in required), "missing_causal_identity")

    attempts = {}
    for identity, rows in sorted(groups["outer_attempt_id"].items()):
        start, end = payload(rows, "loop.attempt.start"), payload(rows, "loop.attempt.end")
        check(
            rows,
            exists(rows, "loop.attempt.start") and exists(rows, "loop.attempt.end"),
            "unclosed_loop_attempt",
        )
        physical = calls("outer_attempt_id", identity)
        check(
            rows,
            set(end.get("physical_call_ids", [])) == set(physical),
            "loop_physical_call_mismatch",
        )
        attempts[identity] = {
            "identity": rows[0]["context"],
            "start": start,
            "end": end,
            "physical_call_ids": physical,
        }

    iterations = {}
    for identity, rows in sorted(groups["iteration_id"].items()):
        end = payload(rows, "loop.iteration.end")
        actual = sorted(
            k for k, v in attempts.items() if v["identity"].get("iteration_id") == identity
        )
        check(
            rows,
            exists(rows, "loop.iteration.start") and exists(rows, "loop.iteration.end"),
            "unclosed_iteration",
        )
        check(
            rows, set(end.get("attempt_ids", [])) == set(actual), "loop_attempt_manifest_mismatch"
        )
        iterations[identity] = {
            "run_id": rows[0]["context"].get("run_id"),
            "index": int(rows[0]["context"].get("iteration_index", 0)),
            "start": payload(rows, "loop.iteration.start"),
            "end": end,
            "attempts": sorted(
                [attempts[k] for k in actual],
                key=lambda a: int(a["identity"].get("outer_attempt_index", 0)),
            ),
            "state_commits": [e for e in rows if e["kind"] == "loop.state.committed"],
        }

    members = {}
    for identity, rows in sorted(groups["member_attempt_id"].items()):
        end = payload(rows, "ensemble.member.end")
        physical = calls("member_attempt_id", identity)
        check(
            rows,
            exists(rows, "ensemble.member.start") and exists(rows, "ensemble.member.end"),
            "unclosed_ensemble_member",
        )
        check(
            rows,
            set(end.get("physical_call_ids", [])) == set(physical),
            "member_physical_call_mismatch",
        )
        members[identity] = {
            "identity": rows[0]["context"],
            "start": payload(rows, "ensemble.member.start"),
            "end": end,
            "physical_call_ids": physical,
        }

    for member in members.values():
        previous_id = member["start"].get("previous_attempt_id")
        if previous_id:
            previous = members.get(previous_id, {})
            check(
                groups["member_attempt_id"][member["identity"]["member_attempt_id"]],
                previous.get("identity", {}).get("logical_call_id")
                == member["identity"].get("logical_call_id")
                and previous.get("identity", {}).get("ensemble_round_id")
                == member["identity"].get("ensemble_round_id")
                and int(previous.get("identity", {}).get("member_attempt_index", -1))
                < int(member["identity"].get("member_attempt_index", -1)),
                "member_retry_mismatch",
            )

    candidates = {
        e["context"]["candidate_id"]: e
        for e in events
        if e["kind"] == "ensemble.candidate.end" and e["context"].get("candidate_id")
    }
    rounds = {}
    for identity, rows in sorted(groups["ensemble_round_id"].items()):
        end = payload(rows, "ensemble.round.end")
        scheduled = [e["payload"] for e in rows if e["kind"] == "ensemble.candidate.scheduled"]
        selected = [e for e in rows if e["kind"] == "ensemble.selection"]
        actual = sorted(
            k for k, m in members.items() if m["identity"].get("ensemble_round_id") == identity
        )
        check(
            rows,
            exists(rows, "ensemble.round.start") and exists(rows, "ensemble.round.end"),
            "unclosed_ensemble_round",
        )
        check(
            rows,
            set(end.get("member_attempt_ids", [])) == set(actual),
            "ensemble_member_manifest_mismatch",
        )
        check(
            rows,
            set(end.get("selection_event_ids", [])) == {e["event_id"] for e in selected},
            "ensemble_selection_manifest_mismatch",
        )
        check(
            rows,
            set(end.get("candidate_ids", [])) == {p["candidate_id"] for p in scheduled},
            "ensemble_schedule_mismatch",
        )
        for plan in scheduled:
            candidate = candidates.get(plan["candidate_id"])
            check(rows, candidate is not None, "unfinished_candidate")
            if candidate:
                actual_candidate_attempts = {
                    k
                    for k, m in members.items()
                    if m["identity"].get("candidate_id") == plan["candidate_id"]
                }
                check(
                    rows,
                    set(candidate["payload"].get("member_attempt_ids", []))
                    == actual_candidate_attempts,
                    "missing_candidate_attempt",
                )
        for selection in selected:
            for position, item in enumerate(selection["payload"].get("input_candidates", []), 1):
                source = candidates.get(item.get("candidate_id"))
                check(rows, source is not None, "missing_candidate_origin")
                check(rows, item.get("position") == position, "candidate_position_mismatch")
                if source:
                    check(
                        rows,
                        source["event_id"] == item.get("result_event_id")
                        and by_id.get(item.get("result_event_id")) == source,
                        "candidate_origin_mismatch",
                    )
                    accepted = item.get("accepted_member_attempt_id")
                    check(
                        rows,
                        bool(source["payload"].get("ok"))
                        and accepted in members
                        and members[accepted]["identity"].get("candidate_id")
                        == item["candidate_id"]
                        and members[accepted]["end"].get("status") == "done"
                        and accepted == source["payload"].get("accepted_member_attempt_id"),
                        "candidate_attempt_mismatch",
                    )
        accepted = end.get("accepted_member_attempt_id")
        if end.get("status") == "completed":
            check(
                rows,
                accepted in members
                and members[accepted]["identity"].get("ensemble_round_id") == identity
                and members[accepted]["end"].get("status") == "done"
                and members[accepted]["identity"].get("role") == end.get("final_role"),
                "missing_accepted_aggregator",
            )
        rounds[identity] = {
            "identity": rows[0]["context"],
            "scheduled_candidates": scheduled,
            "candidates": {p["candidate_id"]: candidates.get(p["candidate_id"]) for p in scheduled},
            "selections": selected,
            "members": {k: members[k] for k in actual},
            "end": end,
        }
    return iterations, rounds, errors
