"""Bounded browser-test backend using the real host, Worker and Temporal Server.

Only the test launcher owns this process. Readiness is one JSON stdout line;
SIGTERM closes HTTP, drains the Worker and stops the ephemeral Temporal Server.
No test-only route or production execution cache is introduced.
"""

import asyncio
import json
import signal
import socket
from datetime import timedelta

import uvicorn
from axcutor_host import ExecutionService
from axcutor_host.http import create_app
from axcutor_runtime.worker import create_worker
from temporalio.testing import WorkflowEnvironment


async def main() -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with create_worker(environment.client, "playground-browser"):
            service = ExecutionService(
                environment.client,
                "playground-browser",
                rpc_timeout=timedelta(seconds=5),
                options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000},
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(create_app(service), log_level="warning"))
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
