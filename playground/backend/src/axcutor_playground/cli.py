"""Independent server entry point for the persistent Playground workbench."""

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from temporalio.client import Client

from .coordinator import Playground
from .http import create_playground_app
from .workspace import Workspace


async def _run(args: argparse.Namespace) -> None:
    client = await Client.connect(args.address, namespace=args.namespace)
    playground = Playground(client, Workspace(args.data, args.examples))
    await uvicorn.Server(
        uvicorn.Config(
            create_playground_app(playground, args.static), host=args.host, port=args.port
        )
    ).serve()


def main() -> None:
    parser = argparse.ArgumentParser(prog="axcutor-playground")
    parser.add_argument("--address", default=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    parser.add_argument("--namespace", default=os.getenv("TEMPORAL_NAMESPACE", "default"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", type=Path, default=Path(".axcutor"))
    parser.add_argument("--examples", type=Path, default=Path("examples"))
    parser.add_argument("--static", type=Path, default=Path("playground/dist"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        logging.exception("Playground command failed")
        raise SystemExit(1) from None
