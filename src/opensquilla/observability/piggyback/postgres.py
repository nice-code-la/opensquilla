"""Optional PostgreSQL/S3 backend. S3 writes and DB commit both precede acceptance."""

from __future__ import annotations

import json

from .protocol import ProtocolError, canonical, decode, digest, identifier


class PostgresS3Ingest:
    def __init__(self, dsn: str, s3_client, bucket: str):
        import psycopg

        self.connect, self.dsn, self.s3, self.bucket = psycopg.connect, dsn, s3_client, bucket

    def initialize(self):
        """Explicit deployment migration, never run from a client or on each LLM request."""
        with self.connect(self.dsn) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trace_batches(
                tenant TEXT, source TEXT, id TEXT, sha TEXT NOT NULL, envelope BYTEA NOT NULL,
                received TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(tenant,source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS trace_records(
                tenant TEXT, source TEXT, id TEXT, type TEXT NOT NULL, sha TEXT NOT NULL,
                body BYTEA, object_key TEXT, PRIMARY KEY(tenant,source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS trace_observations(
                tenant TEXT, call_id TEXT, kind TEXT, body BYTEA,
                observed TIMESTAMPTZ NOT NULL DEFAULT now())""")

            db.execute("""CREATE TABLE IF NOT EXISTS trace_identity_conflicts(
                tenant TEXT, source TEXT, id TEXT, sha TEXT, body BYTEA,
                PRIMARY KEY(tenant,source,id,sha))""")

    def accept(self, auth_context: str, batch: dict) -> str:
        try:
            return self._accept(auth_context, batch)
        except ProtocolError as exc:
            if str(exc) not in {"record_identity_conflict", "batch_identity_conflict"}:
                raise
            records = decode(batch)
            # A rejected reconstructed identity must remain visible to ID-only
            # readers, just like a conflicting unfragmented declaration.
            from .fragments import assemble

            with self.connect(self.dsn) as db:
                for piece in list(records):
                    if piece["type"] != "record_fragment":
                        continue
                    rows = db.execute(
                        "SELECT body FROM trace_records WHERE tenant=%s AND source=%s "
                        "AND type='record_fragment' AND convert_from(body,'UTF8')::jsonb "
                        "#>> '{value,record_sha256}'=%s",
                        (
                            identifier(auth_context),
                            batch["source_id"],
                            piece["value"]["record_sha256"],
                        ),
                    ).fetchall()
                    siblings = [
                        r
                        for r in records
                        if r["type"] == "record_fragment"
                        and r["value"]["record_sha256"] == piece["value"]["record_sha256"]
                    ]
                    restored = assemble(
                        [*[json.loads(bytes(r[0])) for r in rows], *siblings], batch["source_id"]
                    )
                    if restored:
                        records.append(restored)
            with self.connect(self.dsn) as db:
                db.execute("SET LOCAL synchronous_commit=on")
                for record in records:
                    if record["type"] in {"call_ids", "turn_ids", "id_node", "id_fact"}:
                        raw = canonical(record)
                        db.execute(
                            "INSERT INTO trace_identity_conflicts VALUES(%s,%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (
                                identifier(auth_context),
                                batch["source_id"],
                                record["id"],
                                digest(raw),
                                raw,
                            ),
                        )
            raise

    def _accept(self, auth_context: str, batch: dict) -> str:
        tenant = identifier(auth_context)
        records = decode(batch)
        source, batch_id = batch["source_id"], batch["batch_id"]
        with self.connect(self.dsn) as db:
            db.execute("SET LOCAL synchronous_commit=on")
            # Serialize one source, including concurrent duplicates with different batch IDs.
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (tenant + ":" + source,)
            )
            old = db.execute(
                "SELECT sha FROM trace_batches WHERE tenant=%s AND source=%s AND id=%s",
                (tenant, source, batch_id),
            ).fetchone()
            if old and old[0] != batch["sha256"]:
                raise ProtocolError("batch_identity_conflict")
            if not old:
                for record in records:
                    pending = [record]
                    if record["type"] == "record_fragment":
                        from .fragments import assemble

                        rows = db.execute(
                            "SELECT body FROM trace_records WHERE tenant=%s AND source=%s "
                            "AND type='record_fragment' AND convert_from(body,'UTF8')::jsonb "
                            "#>> '{value,record_sha256}'=%s",
                            (tenant, source, record["value"]["record_sha256"]),
                        ).fetchall()
                        restored = assemble(
                            [*[json.loads(bytes(r[0])) for r in rows], record], source
                        )
                        if restored:
                            pending.append(restored)
                    for item in pending:
                        self._insert_record(db, tenant, source, item)
                db.execute(
                    "INSERT INTO trace_batches(tenant,source,id,sha,envelope) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (tenant, source, batch_id, batch["sha256"], canonical(batch)),
                )
        # A rollback leaves only unreferenced immutable objects, never a false receipt.
        return batch_id

    def _insert_record(self, db, tenant, source, record):
        raw = canonical(record)
        sha = digest(raw)
        previous = db.execute(
            "SELECT sha,body FROM trace_records WHERE tenant=%s AND source=%s AND id=%s",
            (tenant, source, record["id"]),
        ).fetchone()
        if previous:
            if previous[0] != sha:
                from .identity_facts import equivalent_records

                if previous[1] is None or not equivalent_records(
                    json.loads(bytes(previous[1])), record
                ):
                    raise ProtocolError("record_identity_conflict")
            return
        object_key = None
        body = raw
        if record["type"] == "blob":
            object_key = f"traces/{digest(tenant.encode())}/{source}/{sha}"
            self.s3.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=raw,
                ContentType="application/json",
                Metadata={"sha256": sha},
            )
            body = None
        db.execute(
            "INSERT INTO trace_records VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (tenant, source, record["id"], record["type"], sha, body, object_key),
        )

    def records(self, tenant: str, source: str) -> list[dict]:
        with self.connect(self.dsn) as db:
            rows = db.execute(
                "SELECT body,object_key,sha FROM trace_records "
                "WHERE tenant=%s AND source=%s ORDER BY id",
                (identifier(tenant), identifier(source)),
            ).fetchall()
        result = []
        for body, key, sha in rows:
            raw = (
                self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                if key
                else bytes(body)
            )
            if digest(raw) != sha:
                raise ProtocolError("stored_content_corruption")
            result.append(json.loads(raw))
        return result

    def sources(self, tenant: str) -> list[str]:
        with self.connect(self.dsn) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT source FROM trace_batches WHERE tenant=%s ORDER BY source",
                    (identifier(tenant),),
                )
            ]

    def call_sources(self, tenant: str, call_id: str) -> list[str]:
        # Event bodies remain in PostgreSQL; this never scans S3 content blocks.
        with self.connect(self.dsn) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT source FROM trace_records WHERE type='event' AND tenant=%s "
                    "AND convert_from(body,'UTF8')::jsonb #>> '{value,context,execution_id}'=%s "
                    "AND convert_from(body,'UTF8')::jsonb #>> '{value,kind}' LIKE 'llm.%%' "
                    "ORDER BY source",
                    (identifier(tenant), identifier(call_id)),
                )
            ]

    def observe(self, tenant: str, call_id: str, kind: str, value: dict):
        with self.connect(self.dsn) as db:
            db.execute(
                "INSERT INTO trace_observations(tenant,call_id,kind,body) VALUES(%s,%s,%s,%s)",
                (tenant, call_id, kind, canonical(value)),
            )

    def execution_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        """Read only ID declarations and conflict variants; never events or bodies."""
        with self.connect(self.dsn) as db:
            params = (identifier(tenant), identifier(source)) if source else (identifier(tenant),)
            condition = " AND source=%s" if source else ""
            rows = db.execute(
                "SELECT body FROM trace_records WHERE tenant=%s AND type IN ('id_node','id_fact')"
                + condition
                + " ORDER BY source,id",
                params,
            ).fetchall()
            rows += db.execute(
                "SELECT body FROM trace_identity_conflicts WHERE tenant=%s AND "
                "convert_from(body,'UTF8')::jsonb ->> 'type' IN ('id_node','id_fact')"
                + condition
                + " ORDER BY source,id,sha",
                params,
            ).fetchall()
        return [json.loads(bytes(row[0]))["value"] for row in rows]

    def call_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        with self.connect(self.dsn) as db:
            rows = db.execute(
                "SELECT body FROM trace_records WHERE tenant=%s AND type='call_ids'"
                + (" AND source=%s" if source else "")
                + " ORDER BY source,id",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
            rows += db.execute(
                "SELECT body FROM trace_identity_conflicts "
                "WHERE tenant=%s AND id LIKE 'call_ids:%%'"
                + (" AND source=%s" if source else "")
                + " ORDER BY source,id,sha",
                (identifier(tenant), identifier(source)) if source else (identifier(tenant),),
            ).fetchall()
        return [json.loads(bytes(row[0]))["value"] for row in rows]

    def turn_identities(self, tenant: str, source: str | None = None) -> list[dict]:
        with self.connect(self.dsn) as db:
            params = (identifier(tenant), identifier(source)) if source else (identifier(tenant),)
            condition = " AND source=%s" if source else ""
            rows = db.execute(
                "SELECT body FROM trace_records WHERE tenant=%s AND type='turn_ids'"
                + condition
                + " ORDER BY source,id",
                params,
            ).fetchall()
            rows += db.execute(
                "SELECT body FROM trace_identity_conflicts "
                "WHERE tenant=%s AND id LIKE 'turn_ids:%%'" + condition + " ORDER BY source,id,sha",
                params,
            ).fetchall()
        return [json.loads(bytes(row[0]))["value"] for row in rows]

    def close(self):
        pass  # connections are transaction-scoped

    def observations(self, tenant: str, call_ids: list[str]) -> list[dict]:
        with self.connect(self.dsn) as db:
            rows = db.execute(
                "SELECT call_id,kind,body FROM trace_observations "
                "WHERE tenant=%s AND call_id=ANY(%s) ORDER BY observed",
                (tenant, call_ids),
            ).fetchall()
        return [
            {"call_id": row[0], "kind": row[1], "payload": json.loads(bytes(row[2]))}
            for row in rows
        ]
