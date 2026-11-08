"""Host-side commands for a resident Worker and file-based Workflow submission.

Source IDs are POSIX paths relative to source-root, including file extensions.
All sources are snapshotted before submission; Workers never read workflow files.
Temporal owns execution history and results after the submitting process exits.
"""

import argparse
import asyncio
import logging
import os
import signal
from datetime import timedelta
from pathlib import Path

import uvicorn
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from temporalio.client import Client

from .http import create_app
from .service import ExecutionService


def load_sources(
    entry: Path, source_root: Path | None = None
) -> tuple[str, dict[str, dict[str, str]]]:
    """Snapshot YAML/JSON files under one root, using exact relative source IDs."""
    entry = entry.resolve(strict=True)
    root = (source_root or entry.parent).resolve(strict=True)
    entry_id = entry.relative_to(root).as_posix()
    formats = {".yaml": "yaml", ".yml": "yaml", ".json": "json"}
    if entry.suffix not in formats:
        raise ValueError("workflow files must use .yaml, .yml or .json")
    sources = {
        path.relative_to(root).as_posix(): {
            "format": formats[path.suffix],
            "text": path.read_text(encoding="utf-8"),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in formats
    }
    return entry_id, sources


async def _worker(client: Client, task_queue: str) -> None:
    """Drain Activities on SIGINT/SIGTERM before the container's stop deadline."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        async with create_worker(
            client, task_queue, graceful_shutdown_timeout=timedelta(seconds=20)
        ):
            logging.info("Worker polling namespace=%s task_queue=%s", client.namespace, task_queue)
            await stopped.wait()
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


async def _run(args: argparse.Namespace) -> None:
    # Controllers stay lazy so preflight precedes backend contact. The SDK
    # requires Workers to receive an eagerly connected client.
    client = await Client.connect(
        args.address, namespace=args.namespace, lazy=args.command != "worker"
    )
    service = ExecutionService(client, args.task_queue)
    if args.command == "run":
        entry, sources = load_sources(args.workflow, args.source_root)
        inputs = loads(args.input.read_text(encoding="utf-8")) if args.input else {}
        if not isinstance(inputs, dict):
            raise ValueError("workflow input must be a JSON object")
        execution = await service.submit(entry, sources, inputs, args.id)
        print(dumps(execution), flush=True)
        if not args.no_wait:
            execution = await service.wait(execution["execution_id"])
            print(dumps(execution), flush=True)
            if execution["status"] != "completed":
                raise SystemExit(1)
    elif args.command == "worker":
        await _worker(client, args.task_queue)
    elif args.command == "serve":
        await uvicorn.Server(
            uvicorn.Config(create_app(service), host=args.host, port=args.port)
        ).serve()


def main() -> None:
    """Emit Execution JSON; no-wait reports submission, waiting reports its outcome."""
    parser = argparse.ArgumentParser(prog="axcutor")
    commands = parser.add_subparsers(dest="command", required=True)
    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument("--address", default=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    connection.add_argument("--namespace", default=os.getenv("TEMPORAL_NAMESPACE", "default"))
    connection.add_argument("--task-queue", default=os.getenv("AXCUTOR_TASK_QUEUE", "axcutor"))
    commands.add_parser("worker", parents=[connection], help="poll for Workflows and Activities")
    run = commands.add_parser("run", parents=[connection], help="compile and execute a workflow")
    run.add_argument("workflow", type=Path)
    run.add_argument(
        "--source-root", type=Path, help="source ID root (default: workflow directory)"
    )
    run.add_argument("--input", type=Path, help="input JSON file (default: {})")
    run.add_argument("--id", help="Workflow ID (default: a new UUID)")
    run.add_argument("--no-wait", action="store_true", help="exit after submission")
    serve = commands.add_parser("serve", parents=[connection], help="serve the HTTP execution API")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    serve.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        logging.exception("Axcutor command failed")
        raise SystemExit(1) from None
