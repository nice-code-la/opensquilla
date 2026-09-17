"""Local transactional ID inventory. No network, no reconstruction from event records."""

from __future__ import annotations

import json
from contextlib import nullcontext

from .identity import PROFILE, declaration, kind, record, ref
from .protocol import ProtocolError, canonical


class IdentityRegistry:
    def __init__(self, journal):
        self.journal = journal
        self.namespace = journal.source_id
        with journal.transaction():
            location = str(journal.root.resolve())
            journal.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('identity_owner_path',?)", (location,)
            )
            owner = journal.db.execute(
                "SELECT value FROM meta WHERE key='identity_owner_path'"
            ).fetchone()[0]
            if owner != location:
                raise ValueError("copied_identity_journal_requires_new_capture_root")
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS identity_scopes(id TEXT PRIMARY KEY, "
                "closure_id TEXT UNIQUE NOT NULL, closed INTEGER NOT NULL DEFAULT 0)"
            )
            journal.db.execute(
                "CREATE TABLE IF NOT EXISTS identity_members(scope_id TEXT, id TEXT, "
                "PRIMARY KEY(scope_id,id))"
            )

    def id(self, typ, key=None):
        try:
            return ref(self.namespace, typ, key)
        except ProtocolError:
            if not isinstance(key, str):
                raise
            # Opaque external IDs may contain slashes/Unicode. Allocate a local
            # identity by exact native-key equality; never encode business prose.
            alias = "identity_alias:" + json.dumps([typ, key], ensure_ascii=False)
            with self.journal.lock:
                with (
                    nullcontext() if self.journal.db.in_transaction else self.journal.transaction()
                ):
                    row = self.journal.db.execute(
                        "SELECT value FROM meta WHERE key=?", (alias,)
                    ).fetchone()
                    if row:
                        return row[0]
                    for _ in range(32):
                        identity = ref(self.namespace, typ)
                        allocated = self.journal.db.execute(
                            "SELECT 1 FROM meta WHERE key LIKE 'identity_alias:%' AND value=?",
                            (identity,),
                        ).fetchone()
                        if not allocated and self._get(identity) is None:
                            break
                    else:
                        raise ValueError("identity_allocation_collision")
                    self.journal._capacity(len(alias.encode()) + 256)
                    self.journal.db.execute("INSERT INTO meta VALUES(?,?)", (alias, identity))
                    return identity

    def _get(self, identity):
        row = self.journal.db.execute(
            "SELECT body FROM records WHERE id=? AND type='id_node'", (identity,)
        ).fetchone()
        return json.loads(row[0])["value"] if row else None

    def get(self, identity):
        with self.journal.lock:
            return self._get(identity)

    def _put(self, node):
        old = self._get(node["id"])
        if old:
            if canonical(old) != canonical(node):
                raise ValueError("identity_conflict")
            return node["id"]
        scope = node["scope_id"]
        if scope:
            row = self.journal.db.execute(
                "SELECT closed FROM identity_scopes WHERE id=?", (scope,)
            ).fetchone()
            if not row or row[0]:
                raise ValueError("identity_scope_not_open")
            self.journal.db.execute(
                "INSERT OR IGNORE INTO identity_members VALUES(?,?)", (scope, node["id"])
            )
        value = record(node)
        self.journal._capacity(len(canonical(value)) * 2)
        self.journal._insert(value, scope)
        return node["id"]

    def put(self, typ, scope=None, *, key=None, **fields):
        with self.journal.transaction():
            if key is None:
                for _ in range(32):
                    identity = self.id(typ)
                    if self._get(identity) is None:
                        break
                else:
                    raise ValueError("identity_allocation_collision")
            else:
                identity = self.id(typ, key)
            node = declaration(identity, scope, **fields)
            return self._put(node)

    def open(self, run_id):
        with self.journal.transaction():
            self.journal.db.execute(
                "INSERT OR IGNORE INTO identity_scopes(id,closure_id) VALUES(?,?)",
                (run_id, self.id("closure")),
            )
            row = self.journal.db.execute(
                "SELECT closed,closure_id FROM identity_scopes WHERE id=?", (run_id,)
            ).fetchone()
            if row[0]:
                raise ValueError("cannot_reopen_identity_scope")
            return row[1]

    def append_call(self, run, lane, attempt, context, key, fallback):
        """Atomically declare a physical call and advance its explicit serial lane."""
        with self.journal.transaction():
            head = "identity_lane_head:" + lane
            row = self.journal.db.execute("SELECT value FROM meta WHERE key=?", (head,)).fetchone()
            prior = row[0] if row else None
            operation = self._get(attempt)["operation_id"]
            retry = (
                prior
                if prior and self._get(self._get(prior)["attempt_id"])["operation_id"] == operation
                else None
            )
            call = self._put(
                declaration(
                    self.id("call", key),
                    run,
                    attempt_id=attempt,
                    context_id=context,
                    previous_id=prior,
                    retry_id=retry,
                    fallback_id=fallback if fallback == retry else None,
                )
            )
            self.journal.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (head, call))
            return call

    def reserve(self, run_id, identity):
        """Include a known future receipt in the inventory without pretending it exists."""
        with self.journal.transaction():
            row = self.journal.db.execute(
                "SELECT closed FROM identity_scopes WHERE id=?", (run_id,)
            ).fetchone()
            if not row or row[0]:
                raise ValueError("identity_scope_not_open")
            self.journal.db.execute(
                "INSERT OR IGNORE INTO identity_members VALUES(?,?)", (run_id, identity)
            )

    def seal(self, run_id, status="completed", *, failed=False):
        with self.journal.transaction():
            row = self.journal.db.execute(
                "SELECT closed,closure_id FROM identity_scopes WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("unknown_identity_scope")
            if row[0]:
                return row[1]
            terminal = {"completed": "run_success", "cancelled": "run_cancelled"}.get(
                status, "run_failure"
            )
            self._put(declaration(self.id(terminal), run_id, run_id=run_id))
            if failed:
                self._put(declaration(self.id("gap"), run_id, affected_id=run_id))
            # Derive obligations from starts, not just the terminal records that
            # happened to be emitted. Preserve omission as a gap; never invent success.
            observed = [
                json.loads(r[0])["value"]
                for r in self.journal.db.execute(
                    "SELECT body FROM records WHERE type='id_node' AND run_id=?", (run_id,)
                )
            ]
            obligations = (
                (
                    "attempt",
                    {"attempt_success", "attempt_failure", "attempt_interrupted"},
                    "attempt_id",
                ),
                ("delivery", {"delivered", "delivery_failed"}, "delivery_id"),
                ("call", {"call_success", "call_failure", "call_interrupted"}, "call_id"),
                (
                    "tool_execution",
                    {"tool_success", "tool_failure", "tool_cancelled"},
                    "execution_id",
                ),
                (
                    "candidate",
                    {"candidate_result", "candidate_failure", "candidate_cancelled"},
                    "candidate_id",
                ),
                ("tool_attempt", {"tool_execution", "tool_rejection"}, "attempt_id"),
            )
            for typ, terminal_types, field in obligations:
                for item in observed:
                    if (
                        kind(item["id"]) == typ
                        and sum(
                            kind(n["id"]) in terminal_types and n.get(field) == item["id"]
                            for n in observed
                        )
                        != 1
                    ):
                        self._put(declaration(self.id("gap"), run_id, affected_id=item["id"]))
            # A complete response claim requires the response_received observation.
            for item in observed:
                if kind(item["id"]) == "call" and any(
                    kind(n["id"]) == "call_success" and n.get("call_id") == item["id"]
                    for n in observed
                ):
                    if (
                        sum(
                            kind(n["id"]) == "received" and n.get("call_id") == item["id"]
                            for n in observed
                        )
                        != 1
                    ):
                        self._put(declaration(self.id("gap"), run_id, affected_id=item["id"]))
            ids = [
                r[0]
                for r in self.journal.db.execute(
                    "SELECT id FROM identity_members WHERE scope_id=? ORDER BY id", (run_id,)
                )
            ]
            parts = []
            for offset in range(0, max(1, len(ids)), 256):
                part_id = self.id("closure_part")
                parts.append(part_id)
                self._put(
                    declaration(
                        part_id, run_id, closure_id=row[1], required_ids=ids[offset : offset + 256]
                    )
                )
            gaps = [identity for identity in ids if kind(identity) == "gap"]
            self._put(
                declaration(
                    row[1],
                    run_id,
                    run_id=run_id,
                    profile_id=PROFILE,
                    part_ids=parts,
                    child_ids=[],
                    gap_ids=gaps,
                )
            )
            self.journal.db.execute("UPDATE identity_scopes SET closed=1 WHERE id=?", (run_id,))
            return row[1]

    def declarations(self):
        with self.journal.lock:
            return [
                json.loads(row[0])["value"]
                for row in self.journal.db.execute(
                    "SELECT body FROM records WHERE type='id_node' ORDER BY id"
                )
            ]

    def accept_local_turn(self, branch, key=None):
        """Authority for the in-process Agent API: atomically accept and advance."""
        with self.journal.transaction():
            alias = "identity_turn_alias:" + json.dumps([branch, key]) if key else None
            known = self.journal.db.execute(
                "SELECT value FROM meta WHERE key=?", (alias,)
            ).fetchone()
            if known:
                if known[0] == "conflict":
                    raise ValueError("turn_alias_conflict")
                return known[0]
            identity = self.id("turn", key)
            if key is None and self._get(identity):
                raise ValueError("identity_allocation_collision")
            if old := self._get(identity):
                if old["branch_id"] != branch:
                    identity = self.id("turn")
                else:
                    if alias:
                        self.journal.db.execute(
                            "INSERT OR IGNORE INTO meta VALUES(?,?)", (alias, identity)
                        )
                    return identity
            meta_key = "identity_turn_head:" + branch
            prior = self.journal.db.execute(
                "SELECT value FROM meta WHERE key=?", (meta_key,)
            ).fetchone()
            source_id = self.id("input")
            if self._get(source_id):
                raise ValueError("identity_allocation_collision")
            source = self._put(declaration(source_id, native_id=None))
            self._put(
                declaration(
                    identity,
                    branch_id=branch,
                    previous_id=prior[0] if prior else None,
                    input_id=source,
                )
            )
            self.journal.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (meta_key, identity))
            if alias:
                self.journal.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES(?,?)", (alias, identity)
                )
            return identity

    def bind_turn_alias(self, branch, key, turn):
        """Caller holds the journal transaction, copying an authoritative native receipt."""
        alias = "identity_turn_alias:" + json.dumps([branch, key])
        old = self.journal.db.execute("SELECT value FROM meta WHERE key=?", (alias,)).fetchone()
        value = turn if not old or old[0] == turn else "conflict"
        self.journal.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (alias, value))

    def resolve_turn(self, branch, key):
        with self.journal.lock:
            row = self.journal.db.execute(
                "SELECT value FROM meta WHERE key=?",
                ("identity_turn_alias:" + json.dumps([branch, key]),),
            ).fetchone()
            if row:
                return self._get(row[0]) if row[0] != "conflict" else None
            # Read-only compatibility for journals created before alias indexing.
            value = self._get(self.id("turn", key))
            return value if value and value["branch_id"] == branch else None

    def call_bundle(self, call_id, limit=128):
        """A bounded current-call prefix. All omitted dependencies remain in outbox.

        Nodes the platform has already durably acknowledged are not repeated: the
        chain is still walked through them so the unacknowledged remainder is found.
        """
        from .identity import references

        pending, seen, result = [call_id], set(), []
        while pending and len(result) < limit:
            identity = pending.pop(0)
            if identity in seen:
                continue
            seen.add(identity)
            with self.journal.lock:
                row = self.journal.db.execute(
                    "SELECT body, acknowledged FROM records WHERE id=? AND type='id_node'",
                    (identity,),
                ).fetchone()
            if row is None:
                continue
            node = json.loads(row[0])["value"]
            if not row[1] or identity == call_id:
                result.append(record(node))
            # Never recursively unroll earlier calls/turns or child completion.
            pending.extend(
                target
                for field, target in references(node)
                if field
                not in {"previous_id", "retry_id", "fallback_id", "completion_id", "dispatch_id"}
            )
        return sorted(result, key=lambda row: row["id"])
