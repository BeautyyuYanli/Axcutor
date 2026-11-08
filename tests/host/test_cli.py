"""Exercise file submission through the real command boundary and Temporal Server."""

import asyncio
import signal
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from axcutor_host import ExecutionService
from axcutor_host.cli import load_sources
from axcutor_runtime import compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, call, graph
from temporalio.client import WorkflowFailureError

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/workflows"


async def invoke(*args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from axcutor_host.cli import main; main()",
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode, stdout.decode(), stderr.decode()


@pytest.mark.parametrize("no_wait", [False, True])
async def test_cli_runs_source_snapshot_and_emits_exact_result(server, tmp_path, no_wait):
    queue, workflow_id = str(uuid4()), str(uuid4())
    input_file = tmp_path / "input.json"
    # Cross the subprocess/Temporal boundary without rounding an integer to float.
    items = [10**40, 1, 2]
    input_file.write_text(dumps({"items": items, "delay_seconds": 0}))
    async with create_worker(server.client, queue):
        code, stdout, stderr = await invoke(
            "run",
            EXAMPLES / "main.yaml",
            "--input",
            input_file,
            "--address",
            server.client.service_client.config.target_host,
            "--task-queue",
            queue,
            "--id",
            workflow_id,
            *(["--no-wait"] if no_wait else []),
        )
        assert code == 0, stderr
        lines = [loads(line) for line in stdout.splitlines()]
        assert lines[0]["execution_id"] == workflow_id
        assert "run_id" not in lines[0]
        expected = {
            "branch": None,
            "output": {"doubled": [2 * n for n in items], "total": 2 * sum(items)},
        }
        handle = server.client.get_workflow_handle(workflow_id)
        assert loads(await asyncio.wait_for(handle.result(), 10)) == expected
        assert len(lines) == (1 if no_wait else 2)
        if not no_wait:
            assert lines[1]["status"] == "completed"
            assert lines[1]["result"] == expected


async def test_cli_rejects_invalid_source_before_connecting(tmp_path):
    source = tmp_path / "invalid.yaml"
    source.write_text("input: {}\noutput: {}\ngraph: {}\n")
    code, stdout, stderr = await invoke("run", source, "--address", "127.0.0.1:1")
    assert code == 1
    assert not stdout
    assert "CompilationError" in stderr


def test_file_sources_use_explicit_root_and_only_compile_reachable_documents(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    entry = nested / "main.json"
    entry.write_text(
        dumps(
            graph(
                {"child": call("child.yml", outputs={"value": "value"})},
                ["start -> child", "child -> end"],
                outputs={"value": INTEGER},
            )
        )
    )
    (tmp_path / "child.yml").write_text(
        "input: {}\noutput:\n  value: {type: integer}\n"
        "run:\n  executor: builtin\n  content: return Output(value=7)\n"
    )
    (tmp_path / "unused.json").write_text("not a workflow")
    bundle = loads(compile_bundle(*load_sources(entry, tmp_path)))
    assert bundle["entry"] == "nested/main.json"
    assert set(bundle["workflows"]) == {"nested/main.json", "child.yml"}
    with pytest.raises(ValueError):
        load_sources(tmp_path / "child.yml", nested)


async def test_cli_rejects_invalid_input_before_connecting(tmp_path):
    source = tmp_path / "main.yaml"
    source.write_text(
        "input:\n  n: {type: integer}\noutput: {}\n"
        "run:\n  executor: builtin\n  content: return Output()\n"
    )
    code, stdout, stderr = await invoke("run", source, "--address", "127.0.0.1:1")
    assert code == 1 and not stdout
    assert "InputValidationError" in stderr


@asynccontextmanager
async def resident(*args, exit_codes=(0,)):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from axcutor_host.cli import main; main()",
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise
        assert process.returncode in exit_codes, (stdout, stderr)


async def test_serve_binds_http_and_shuts_down_without_backend():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Uvicorn restores and re-raises SIGTERM after its graceful ASGI shutdown.
    async with resident(
        "serve", "--port", port, "--address", "127.0.0.1:1", exit_codes=(0, -signal.SIGTERM)
    ):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        response = await client.post("/v1/executions", content="{")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.02)
            assert response.status_code == 400
            assert loads(response.text)["error"]["code"] == "invalid_json"


async def test_worker_process_polls_and_handles_sigterm(server):
    queue = str(uuid4())
    async with resident(
        "worker",
        "--address",
        server.client.service_client.config.target_host,
        "--task-queue",
        queue,
    ):
        # Start can queue before the process begins polling; the live query
        # confirms a resident Worker actually processed the source snapshot.
        host = ExecutionService(server.client, queue)
        text = "input: {}\noutput: {}\nrun:\n  executor: builtin\n  content: return Output()\n"
        source = {
            "main.yaml": {
                "format": "yaml",
                "text": text,
            },
        }
        first = await host.submit("main.yaml", source)
        final = await host.wait(first["execution_id"])
        assert final["status"] == "completed", final


@pytest.mark.parametrize("no_wait", [False, True])
async def test_cli_failure_exit_code_depends_on_wait_mode(server, tmp_path, no_wait):
    queue = str(uuid4())
    source = tmp_path / "main.yaml"
    source.write_text(
        "input: {}\noutput: {}\nrun:\n  executor: builtin\n"
        "  content: raise RuntimeError('business failed')\n"
    )
    async with create_worker(server.client, queue):
        code, stdout, stderr = await invoke(
            "run",
            source,
            "--address",
            server.client.service_client.config.target_host,
            "--task-queue",
            queue,
            *(["--no-wait"] if no_wait else []),
        )
        assert code == (0 if no_wait else 1), stderr
        lines = [loads(line) for line in stdout.splitlines()]
        assert len(lines) == (1 if no_wait else 2)
        if no_wait:
            handle = server.client.get_workflow_handle(lines[0]["execution_id"])
            with pytest.raises(WorkflowFailureError):
                await handle.result()
            final = await ExecutionService(server.client, queue).get(handle.id)
        else:
            final = lines[-1]
    assert final["status"] == "failed" and final["error"]["type"] == "execution"
