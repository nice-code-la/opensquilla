"""Local administration. Only 'serve' starts a server; no command flushes client telemetry."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from .capture import Capture
from .environment import FileEnvironmentAdapter, RecordedContentStore
from .export import html_report, import_legacy, to_atif, to_otlp, write_bundle
from .id_graph import IdTraceReconstructor
from .journal import atomic_write
from .protocol import digest
from .reconstruct import TraceReconstructor
from .transport import Destination


def local_records(path: Path) -> list[dict]:
    database = path / "journal.sqlite3" if path.is_dir() else path
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [
            json.loads(row[0]) for row in db.execute("SELECT body FROM records ORDER BY ordinal")
        ]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opensquilla-trace")
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure", help="write an opt-in local capture configuration")
    configure.add_argument("--output", type=Path, required=True)
    configure.add_argument("--root", type=Path, required=True)
    configure.add_argument("--platform", required=True)
    configure.add_argument("--tenant", required=True)
    configure.add_argument("--credential-env", required=True)
    configure.add_argument("--environment-root", action="append", default=[])
    configure.add_argument("--execution-version", type=int, choices=[3, 4], default=3)
    configure.add_argument(
        "--record-scope",
        choices=["full", "identity"],
        default="identity",
        help="identity (default): store and upload only ID declarations; "
        "full: also events, request/response bodies and environment snapshots",
    )
    configure.add_argument(
        "--retain-acknowledged-seconds",
        type=lambda v: None if v.lower() in {"none", "forever"} else float(v),
        default=0.0,
        help="delete acknowledged local data after this many seconds "
        "(default 0 = right after the platform confirms it; 'forever' keeps it)",
    )
    status = sub.add_parser("status", help="inspect pending records without sending them")
    status.add_argument("--config", type=Path, required=True)
    rebuild = sub.add_parser("reconstruct")
    rebuild.add_argument("--journal", type=Path)
    rebuild.add_argument("--records", type=Path)
    rebuild.add_argument("--legacy-jsonl", type=Path)
    rebuild.add_argument("--output", type=Path, required=True)
    rebuild.add_argument("--html", type=Path)
    stitch = sub.add_parser("stitch-ids", help="reconstruct Call topology from ID fields only")
    stitch.add_argument("--ids", type=Path, required=True)
    stitch.add_argument("--output", type=Path, required=True)
    stitch.add_argument("--schema", choices=["2", "3", "4"], default="2")
    stitch.add_argument("--html", type=Path)
    export = sub.add_parser("export")
    export.add_argument("--bundle", type=Path, required=True)
    export.add_argument("--format", choices=["otlp", "atif"], required=True)
    export.add_argument("--run-id")
    export.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore_content = restore.add_mutually_exclusive_group(required=True)
    restore_content.add_argument("--content-dir", type=Path)
    restore_content.add_argument("--records", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "configure":
        if args.output.exists():
            parser.error("configuration already exists; edit it explicitly")
        secret = os.environ.get(args.credential_env)
        if not secret:
            parser.error("credential environment variable is unset")
        try:
            destination = Destination(args.platform, args.tenant, digest(secret.encode()))
        except ValueError as exc:
            parser.error(
                f"{exc}: TracePoint uploads only to https://tokenrhythm.studio or "
                "https://api.tokenrhythm.studio (local mock platforms need "
                "OPENSQUILLA_TRACE_EXTRA_PLATFORM_HOSTS)"
            )
        config = {
            "enabled": True,
            "root": str(args.root.absolute()),
            "destination": {
                "base_url": destination.base_url,
                "tenant_id": destination.tenant_id,
                "credential_sha256": destination.credential_sha256,
            },
            "quota_bytes": 10 * 1024**3,
            "max_request_bytes": 16 * 1024**2,
            "environment_roots": [str(Path(p).absolute()) for p in args.environment_root],
            "execution_version": args.execution_version,
            "record_scope": args.record_scope,
            "retain_acknowledged_seconds": args.retain_acknowledged_seconds,
        }
        atomic_write(args.output, json.dumps(config, indent=2).encode())
        print(f"Configuration written: {args.output.absolute()}")
    elif args.command == "status":
        config = json.loads(args.config.read_text())
        capture = Capture(config["root"], Destination(**config["destination"]))
        try:
            print(json.dumps(capture.journal.stats(), indent=2))
        finally:
            capture.journal.close()
    elif args.command == "reconstruct":
        if sum(bool(p) for p in (args.journal, args.records, args.legacy_jsonl)) != 1:
            parser.error("choose exactly one of --journal, --records, --legacy-jsonl")
        if args.journal:
            records = local_records(args.journal)
        elif args.records:
            records = json.loads(args.records.read_text())
        else:
            records = import_legacy(
                [
                    json.loads(line)
                    for line in args.legacy_jsonl.read_text().splitlines()
                    if line.strip()
                ]
            )
        bundle = TraceReconstructor().reconstruct(
            records, provenance="local" if args.journal else "unknown"
        )
        write_bundle(bundle, args.output)
        if args.html:
            atomic_write(args.html, html_report(bundle).encode())
        print(json.dumps({"revision": bundle["revision"], "runs": bundle["runs"]}, indent=2))
    elif args.command == "stitch-ids":
        if args.schema in {"3", "4"}:
            from .identity_graph import ExecutionIdReconstructor

            graph = ExecutionIdReconstructor().reconstruct(json.loads(args.ids.read_text()))
            atomic_write(args.output, json.dumps(graph, ensure_ascii=False, indent=2).encode())
            if args.html:
                from .identity_report import html_report as identity_html_report

                atomic_write(args.html, identity_html_report(graph).encode())
            print(
                json.dumps(
                    {
                        "revision": graph["revision"],
                        "calls": len(graph["positions"]),
                        "scopes": graph["scopes"],
                    },
                    indent=2,
                )
            )
            return
        graph = IdTraceReconstructor().reconstruct(json.loads(args.ids.read_text()))
        atomic_write(args.output, json.dumps(graph, ensure_ascii=False, indent=2).encode())
        print(
            json.dumps(
                {
                    "revision": graph["revision"],
                    "calls": len(graph["calls"]),
                    "references_resolved": graph["references_resolved"],
                }
            )
        )
        if not graph["references_resolved"]:
            raise SystemExit(2)
    elif args.command == "export":
        bundle = json.loads(args.bundle.read_text())
        if args.format == "atif" and not args.run_id:
            parser.error("ATIF export requires --run-id")
        output = to_otlp(bundle) if args.format == "otlp" else to_atif(bundle, args.run_id)
        atomic_write(args.output, json.dumps(output, ensure_ascii=False, indent=2).encode())
    elif args.command == "restore":

        class LocalContent:
            def get(self, ref):
                from .protocol import HEX

                if not HEX.fullmatch(ref):
                    raise ValueError("invalid_content_reference")
                data = (args.content_dir / ref).read_bytes()
                if digest(data) != ref:
                    raise ValueError("content_corruption")
                return data

        snapshot = json.loads(args.snapshot.read_text())
        content = (
            RecordedContentStore(json.loads(args.records.read_text()))
            if args.records
            else LocalContent()
        )
        adapter = FileEnvironmentAdapter(content)
        adapter.restore(snapshot, args.target)
        print(json.dumps(adapter.verify(snapshot, args.target)))
    elif args.command == "serve":
        import uvicorn

        from .server import create_platform

        app = create_platform(json.loads(args.config.read_text()))
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
