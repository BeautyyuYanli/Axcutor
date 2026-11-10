"""Server entry points for the stateless execution API and persistent workbench.

API mode connects lazily and owns no workspace or Worker. The default workbench
owns its workspace and resident Worker through the ASGI application lifespan.
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from axcutor_host import AxcutorHost
from temporalio.client import Client

from .coordinator import Playground
from .execution_http import create_app
from .http import create_playground_app
from .workspace import Workspace


async def _run(args: argparse.Namespace) -> None:
    client = await Client.connect(
        args.address, namespace=args.namespace, lazy=args.command == "api"
    )
    if args.command == "api":
        app = create_app(AxcutorHost(client, args.task_queue))
    else:
        playground = Playground(client, Workspace(args.workspace, args.examples))
        app = create_playground_app(playground, args.static)
    await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).serve()


def main() -> None:
    """Start the workbench or API; shared flags may appear on either side of api."""
    # Subparsers must only overwrite flags explicitly repeated after the command.
    shared = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    shared.add_argument("--address")
    shared.add_argument("--namespace")
    shared.add_argument("--host")
    shared.add_argument("--port", type=int)
    parser = argparse.ArgumentParser(prog="axcutor-playground", parents=[shared])
    parser.add_argument("--workspace", type=Path, default=Path("playground/workspace"))
    parser.add_argument("--examples", type=Path, default=Path("examples"))
    parser.add_argument("--static", type=Path, default=Path("playground/dist"))
    commands = parser.add_subparsers(dest="command")
    api = commands.add_parser(
        "api", parents=[shared], help="serve executions without a workspace or Worker"
    )
    api.add_argument("--task-queue", default=os.getenv("AXCUTOR_TASK_QUEUE", "axcutor"))
    args = parser.parse_args()
    args.address = getattr(args, "address", os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    args.namespace = getattr(args, "namespace", os.getenv("TEMPORAL_NAMESPACE", "default"))
    args.host = getattr(args, "host", "127.0.0.1")
    args.port = getattr(args, "port", 8000 if args.command == "api" else 8080)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        logging.exception("Playground command failed")
        raise SystemExit(1) from None
