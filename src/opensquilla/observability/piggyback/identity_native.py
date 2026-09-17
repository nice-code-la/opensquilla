"""Native transcript identity outbox, committed with the business row.

The native SQLite transaction is the authority. Copying its ID-only outbox to
TraceJournal is idempotent and explicitly a second transaction, not an atomic
transaction across two databases. No network operations exist in this module.
"""

from __future__ import annotations

import json
import uuid

from .identity import declaration, parts, record, ref
from .protocol import canonical


async def prepare(conn, entry):
    from .capture import get_capture
    from .identity_runtime import current

    cap = get_capture()
    if not cap or not cap.current_configuration_allows_capture():
        return
    original = entry.turn_context
    await conn.execute("SAVEPOINT trace_native_identity")
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS trace_native_heads(namespace TEXT, session_id TEXT, "
            "turn_id TEXT, PRIMARY KEY(namespace,session_id))"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS trace_native_inputs(namespace TEXT, session_id TEXT, "
            "message_id TEXT, turn_id TEXT, PRIMARY KEY(namespace,session_id,message_id))"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS trace_native_outbox(id TEXT PRIMARY KEY, namespace TEXT, "
            "body BLOB, copied INTEGER NOT NULL DEFAULT 0)"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS trace_native_turn_aliases(namespace TEXT, session_id TEXT, "
            "legacy_key TEXT, turn_id TEXT, PRIMARY KEY(namespace,session_id,legacy_key,turn_id))"
        )
        namespace = cap.identities.namespace

        def node(typ, key=None, **fields):
            return declaration(ref(namespace, typ, key), **fields)

        nodes = []
        authority = node("authority", namespace)
        session = node("session", entry.session_id, authority_id=authority["id"])
        metadata = dict(entry.turn_context or {})
        existing_branch = cap.identities.get(ref(namespace, "branch", entry.session_id))
        fork_id = metadata.get("execution_fork_checkpoint_id") or (
            existing_branch["fork_id"] if existing_branch else None
        )
        branch = node("branch", entry.session_id, session_id=session["id"], fork_id=fork_id)
        nodes.extend((authority, session, branch))
        metadata = dict(entry.turn_context or {})
        if metadata.get("execution_fork_checkpoint_id"):
            if entry.role == "user" and metadata.get("execution_user_turn_id"):
                await conn.execute(
                    "INSERT OR IGNORE INTO trace_native_inputs VALUES(?,?,?,?)",
                    (
                        namespace,
                        entry.session_id,
                        entry.message_id,
                        metadata["execution_user_turn_id"],
                    ),
                )
        elif entry.role == "user":
            async with conn.execute(
                "SELECT turn_id FROM trace_native_inputs WHERE namespace=? AND "
                "session_id=? AND message_id=?",
                (namespace, entry.session_id, entry.message_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                turn_id = existing[0]
                async with conn.execute(
                    "SELECT body FROM trace_native_outbox WHERE id=?", (turn_id,)
                ) as cursor:
                    saved = await cursor.fetchone()
                input_id = json.loads(saved[0])["value"]["input_id"]
            else:
                async with conn.execute(
                    "SELECT turn_id FROM trace_native_heads WHERE namespace=? AND session_id=?",
                    (namespace, entry.session_id),
                ) as cursor:
                    prior = await cursor.fetchone()
                native = node("native_input")
                source = node("input", native_id=native["id"])
                turn = node(
                    "turn",
                    branch_id=branch["id"],
                    previous_id=prior[0] if prior else None,
                    input_id=source["id"],
                )
                turn_id = turn["id"]
                input_id = source["id"]
                nodes.extend((native, source, turn))
                await conn.execute(
                    "INSERT INTO trace_native_inputs VALUES(?,?,?,?)",
                    (namespace, entry.session_id, entry.message_id, turn_id),
                )
                await conn.execute(
                    "INSERT OR REPLACE INTO trace_native_heads VALUES(?,?,?)",
                    (namespace, entry.session_id, turn_id),
                )
            metadata["execution_user_turn_id"] = turn_id
            from opensquilla.provider.types import MessageTraceIds

            message = node("message", source_ids=[input_id])
            nodes.append(message)
            dto = (
                MessageTraceIds.model_validate(metadata["trace_user_ids"])
                if metadata.get("trace_user_ids")
                else MessageTraceIds(message_id=uuid.uuid4().hex)
            )
            metadata["trace_user_ids"] = dto.model_copy(
                update={
                    "execution_message_id": message["id"],
                    "execution_source_ids": (message["id"],),
                }
            ).model_dump(mode="json")
            for key in dto.input_turn_ids:
                await conn.execute(
                    "INSERT OR IGNORE INTO trace_native_turn_aliases VALUES(?,?,?,?)",
                    (namespace, entry.session_id, key, turn_id),
                )
        else:
            message_ids = []
            for row in metadata.get("trace_history_ids", []):
                value = row.get("trace_ids", {}).get("execution_message_id")
                if value:
                    message_ids.append(value)
            if message_ids:
                # Commit is an immutable receipt of this particular native write.
                # Upsert revisions receive new receipt IDs, preserving the old fact.
                native = node("native_input")
                commit = node("commit", message_ids=message_ids, native_id=native["id"])
                nodes.extend((native, commit))
                metadata["execution_commit_id"] = commit["id"]
                _, registry, scope = current()
                if scope:
                    commit["scope_id"] = scope.run
        metadata["execution_session_id"] = session["id"]
        entry.turn_context = metadata
        async with conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0) FROM trace_native_outbox WHERE "
            "namespace=? AND copied=0",
            (namespace,),
        ) as cursor:
            pending = (await cursor.fetchone())[0]
        cap.journal._capacity(pending + sum(len(canonical(record(value))) for value in nodes))
        for value in nodes:
            payload = canonical(record(value))
            async with conn.execute(
                "SELECT body FROM trace_native_outbox WHERE id=?", (value["id"],)
            ) as cursor:
                old = await cursor.fetchone()
            if old and bytes(old[0]) != payload:
                raise ValueError("native_identity_conflict")
            await conn.execute(
                "INSERT OR IGNORE INTO trace_native_outbox(id,namespace,body) VALUES(?,?,?)",
                (value["id"], namespace, payload),
            )
        await conn.execute("RELEASE trace_native_identity")
    except Exception:
        entry.turn_context = original
        await conn.execute("ROLLBACK TO trace_native_identity")
        await conn.execute("RELEASE trace_native_identity")
        cap.fail()


async def sync(conn):
    """Only after a native commit. A failed copy leaves the native outbox intact."""
    from .capture import get_capture

    cap = get_capture()
    if not cap or not cap.current_configuration_allows_capture():
        return
    try:
        async with conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='trace_native_outbox'"
        ) as cursor:
            if not await cursor.fetchone():
                return
        async with conn.execute(
            "SELECT id,body FROM trace_native_outbox WHERE namespace=? AND "
            "copied=0 ORDER BY rowid LIMIT 1024",
            (cap.identities.namespace,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return
        accepted = []
        turn_ids = [row[0] for row in rows if parts(row[0])[1] == "turn"]
        for offset in range(0, len(turn_ids), 500):
            chunk = turn_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with conn.execute(
                "SELECT session_id,message_id,turn_id FROM trace_native_inputs "
                f"WHERE namespace=? AND turn_id IN ({placeholders})",
                (cap.identities.namespace, *chunk),
            ) as cursor:
                accepted.extend(await cursor.fetchall())
        async with conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='trace_native_turn_aliases'"
        ) as cursor:
            has_aliases = await cursor.fetchone()
        aliases = []
        if has_aliases:
            async with conn.execute(
                "SELECT session_id,legacy_key,turn_id FROM trace_native_turn_aliases "
                "WHERE namespace=?",
                (cap.identities.namespace,),
            ) as cursor:
                aliases = await cursor.fetchall()
        with cap.journal.transaction():
            for row in rows:
                cap.identities._put(json.loads(row[1])["value"])
            for session, message, turn in accepted:
                cap.journal.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES(?,?)",
                    ("identity_native_input:" + json.dumps([session, message]), turn),
                )
            for session, key, turn in aliases:
                cap.identities.bind_turn_alias(cap.identities.id("branch", session), key, turn)
        await conn.executemany(
            "UPDATE trace_native_outbox SET copied=1 WHERE id=?", [(row[0],) for row in rows]
        )
        await conn.commit()
    except Exception:
        cap.fail()


async def activation_inputs(owner, values):
    """Resolve supplied native message identities, never choose history by text/time."""
    from .capture import context, get_capture

    cap = get_capture()
    manager = getattr(owner, "_session_manager", None)
    storage = getattr(manager, "_storage", None)
    if not cap or storage is None:
        return {}
    try:
        session = await storage.get_session(values.get("session_key", ""))
        if session is None:
            return {"_execution_native_expected": True}
        result = {
            "_execution_session_id": session.session_id,
            "_execution_native_expected": values.get("run_kind", "default")
            in {"default", "user", "chat"},
        }
        message_ids = context.get().get("_execution_input_ids") or (
            [values["bound_user_message_id"]] if values.get("bound_user_message_id") else []
        )
        if not message_ids:
            return result
        turns = []
        async with storage.read_transaction("trace_identity_inputs") as conn:
            async with conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='trace_native_inputs'"
            ) as cursor:
                if not await cursor.fetchone():
                    return result
            for message_id in message_ids:
                async with conn.execute(
                    "SELECT turn_id FROM trace_native_inputs WHERE namespace=? "
                    "AND session_id=? AND message_id=?",
                    (cap.identities.namespace, session.session_id, message_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    turns.append(row[0])
        if len(turns) == len(message_ids):
            result["_execution_turn_ids"] = turns
        return result
    except Exception:
        cap.fail()
        return {"_execution_native_expected": True}


def committed(entry):
    from .identity_runtime import current

    cap, registry, scope = current()
    if not cap or not scope:
        return
    try:
        metadata = getattr(entry, "turn_context", None) or {}
        turn = metadata.get("execution_user_turn_id")
        if turn and not registry.get(scope.activation):
            group = registry.put("input_group", scope.run, turn_ids=[turn])
            registry.put(
                "activation",
                scope.run,
                key=parts(scope.activation)[2],
                input_group_id=group,
                trigger_id=None,
            )
            scope.turn, scope.input_turns = turn, [turn]
    except Exception:
        cap.fail()


def fork_context(original, checkpoint):
    return (
        {**(original or {}), "execution_fork_checkpoint_id": checkpoint} if checkpoint else original
    )


def fork_checkpoint(parent, child, entries):
    from .identity_runtime import current

    cap, r, scope = current()
    if not cap or not cap.current_configuration_allows_capture():
        return None
    try:
        authority = r.put("authority", key=r.namespace)
        parent_session = r.put("session", key=parent.session_id, authority_id=authority)
        branch_id = r.id("branch", parent.session_id)
        if not r.get(branch_id):
            r.put("branch", key=parent.session_id, session_id=parent_session, fork_id=None)
        messages = []
        for entry in entries:
            metadata = entry.turn_context or {}
            ids = (
                [metadata["trace_user_ids"]]
                if metadata.get("trace_user_ids")
                else [row.get("trace_ids", {}) for row in metadata.get("trace_history_ids", ())]
            )
            values = [
                row.get("execution_message_id") for row in ids if row.get("execution_message_id")
            ]
            if not values:
                gap = r.put("gap", affected_id=None)
                values = [r.put("message", source_ids=[gap])]
            messages.extend(values)
        checkpoint = r.put(
            "checkpoint",
            run_id=scope.run if scope else None,
            branch_id=branch_id,
            message_ids=messages,
        )
        session = r.put("session", key=child.session_id, authority_id=authority)
        branch = r.put("branch", key=child.session_id, session_id=session, fork_id=checkpoint)
        if scope:
            r.reserve(scope.run, branch)
        return checkpoint
    except Exception:
        cap.fail()
        return None
