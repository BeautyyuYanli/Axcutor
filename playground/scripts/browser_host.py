"""Bounded browser-test backend using the real host, Worker and Temporal Server.

Only the test launcher owns this process. Readiness is one JSON stdout line;
SIGTERM closes HTTP, drains the Worker and stops the ephemeral Temporal Server.
No test-only route or production execution cache is introduced.
"""

import asyncio
import json
import signal
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from axcutor_playground.coordinator import Playground
from axcutor_playground.http import create_playground_app
from axcutor_playground.workspace import Workspace
from temporalio.testing import WorkflowEnvironment

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


async def main() -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        with TemporaryDirectory(prefix="axcutor-browser-") as data:
            workspace = Workspace(Path(data), EXAMPLES)
            playground = Playground(environment.client, workspace)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(
                    uvicorn.Config(create_playground_app(playground), log_level="warning")
                )
                serving = asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    async with asyncio.timeout(10):
                        while not server.started:
                            if serving.done():
                                await serving
                                raise RuntimeError("HTTP server exited before startup")
                            await asyncio.sleep(0.01)
                    print(json.dumps({"apiUrl": f"http://127.0.0.1:{port}"}), flush=True)
                    await stopped.wait()
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(serving, 10)


if __name__ == "__main__":
    asyncio.run(main())
