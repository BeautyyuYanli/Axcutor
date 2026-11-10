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
from axcutor_runtime import compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.packages import PackageMount, load_mounts
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, N, action, call, graph
from temporalio.client import WorkflowFailureError

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/workflows"


async def test_contracts_module_mounts_include_resource_only_roots_without_imports(tmp_path):
    root = tmp_path / "workflows"
    root.mkdir()
    (root / "__init__.py").touch()
    (root / "__axcutor__.yaml").write_text(
        "input: {}\noutput: {tx: {type: handle, kind: 'infra.postgres:Tx'}}\n"
        "run: {executor: module, content: .create}\n"
    )
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "__init__.py").write_text("raise AssertionError('must not import')\n")
    postgres = infra / "postgres"
    postgres.mkdir()
    (postgres / "__init__.py").write_text("raise AssertionError('must not import')\n")
    (postgres / "__ax_resources__.yaml").write_text("resources: {Tx: null}\n")
    args = (
        "contracts",
        root,
        "--mount",
        f"flows={root}",
        "--mount",
        f"infra={infra}",
        "--module-mount",
        "flows=uninstalled.flows",
        "--module-mount",
        "infra=uninstalled.infra",
    )
    code, _, stderr = await invoke(*args)
    assert code == 0, stderr
    source = (root / "create_contract.py").read_text()
    assert "from uninstalled.infra.postgres import Tx as " in source
    assert "TYPE_CHECKING" in source
    code, _, stderr = await invoke(*args, "--check")
    assert code == 0, stderr
    code, _, _ = await invoke(*args, "--module-mount", "infra=duplicate")
    assert code == 1


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
async def test_cli_runs_source_snapshot_and_emits_exact_result(
    server, tmp_path, no_wait, monkeypatch
):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    queue, workflow_id = str(uuid4()), str(uuid4())
    input_file = tmp_path / "input.json"
    # Cross the subprocess/Temporal boundary without rounding an integer to float.
    items = [10**40, 1, 2]
    input_file.write_text(dumps({"items": items, "delay_seconds": 0}))
    async with create_worker(server.client, queue):
        code, stdout, stderr = await invoke(
            "run",
            EXAMPLES / "demo/main",
            "--mount",
            f"demo={EXAMPLES / 'demo'}",
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
        doubled = [2 * n for n in items]
        expected = {
            "branch": None,
            "output": {
                "doubled": doubled,
                "total": sum(doubled),
                "spread": max(doubled) - min(doubled),
            },
        }
        handle = server.client.get_workflow_handle(workflow_id)
        assert loads(await asyncio.wait_for(handle.result(), 10)) == expected
        assert len(lines) == (1 if no_wait else 2)
        if not no_wait:
            assert lines[1]["status"] == "completed"
            assert lines[1]["result"] == expected


async def test_cli_module_uses_preinstalled_package_and_generated_contract(
    server, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    queue = str(uuid4())
    input_file = tmp_path / "input.json"
    input_file.write_text(dumps({"value": 7}))
    code, _, stderr = await invoke(
        "contracts",
        EXAMPLES / "demo/module_double",
        "--mount",
        f"demo={EXAMPLES / 'demo'}",
        "--check",
    )
    assert code == 0, stderr
    async with create_worker(server.client, queue, module_mounts={"demo": "demo"}):
        code, stdout, stderr = await invoke(
            "run",
            EXAMPLES / "demo/module_double",
            "--mount",
            f"demo={EXAMPLES / 'demo'}",
            "--input",
            input_file,
            "--address",
            server.client.service_client.config.target_host,
            "--task-queue",
            queue,
        )
    assert code == 0, stderr
    assert loads(stdout.splitlines()[-1])["result"] == {"branch": None, "output": {"value": 14}}


async def test_cli_rejects_duplicate_root_mounts_before_backend(tmp_path):
    root = EXAMPLES / "demo"
    code, stdout, stderr = await invoke(
        "run",
        root / "main",
        "--mount",
        f"demo={root}",
        "--mount",
        f"demo={root}",
        "--address",
        "127.0.0.1:1",
    )
    assert code == 1 and not stdout
    assert "unique ROOT=TARGET" in stderr


async def test_cli_rejects_invalid_source_before_connecting(tmp_path):
    source = tmp_path / "invalid.yaml"
    source.write_text("input: {}\noutput: {}\ngraph: {}\n")
    code, stdout, stderr = await invoke("run", source, "--address", "127.0.0.1:1")
    assert code == 1
    assert not stdout
    assert "CompilationError" in stderr


def test_file_packages_use_explicit_mounts_and_only_compile_reachable_documents(tmp_path):
    root = tmp_path / "demo"
    nested = root / "nested"
    child = root / "child"
    nested.mkdir(parents=True)
    child.mkdir()
    for directory in [root, nested, child]:
        (directory / "__init__.py").write_text("raise RuntimeError('must not import')\n")
    entry = nested / "__axcutor__.yaml"
    entry.write_text(
        dumps(
            graph(
                {"child": call("demo.child", outputs={"value": "value"})},
                ["start -> child", "child -> end"],
                outputs={"value": INTEGER},
            )
        )
    )
    (child / "__axcutor__.yaml").write_text(
        "input: {}\noutput:\n  value: {type: integer}\n"
        "run:\n  executor: builtin\n  content: return Output(value=7)\n"
    )
    (root / "unused.json").write_text("not a workflow")
    mounts = [PackageMount("demo", root)]
    bundle = loads(compile_bundle(*load_mounts(entry, mounts)))
    assert bundle["entry"] == "demo.nested"
    assert set(bundle["workflows"]) == {"demo.nested", "demo.child"}
    with pytest.raises(ValueError):
        load_mounts(entry)


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
            "demo.main": {
                "format": "yaml",
                "text": text,
            },
        }
        first = await host.submit("demo.main", {"demo": {"packages": source}})
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


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_signal_cli_keeps_target_separate_from_server_and_emits_one_result(
    server, tmp_path, wait, fail
):
    queue = str(uuid4())
    host = ExecutionService(server.client, queue)
    doc = graph(
        {
            "decision": {
                "run": {"executor": "signal"},
                "output": {"value": N},
                "branches": {"ok": "accepted"},
            },
            "after": action(
                "import asyncio\nawait asyncio.sleep(0.1)\n"
                + ("raise RuntimeError('after delivery')" if fail else "return Output()")
            ),
        },
        ["start -> decision", "decision.ok -> after", "after -> end"],
        outputs={"value": N},
    )
    output = tmp_path / "output.json"
    output.write_text('{"value":1.00000000000000000000000001}')
    async with create_worker(server.client, queue):
        first = await host.submit(
            "__main__",
            {"__main__": {"packages": {"__main__": {"format": "json", "text": dumps(doc)}}}},
        )
        code, stdout, stderr = await invoke(
            "signal",
            first["execution_id"],
            "__main__/decision",
            "--branch",
            "ok",
            "--output",
            output,
            "--step",
            1,
            "--address",
            server.client.service_client.config.target_host,
            *(["--wait"] if wait else []),
        )
        assert code == (1 if wait and fail else 0), stderr
        assert len(stdout.splitlines()) == 1
        response = loads(stdout)
        assert response["execution_id"] == first["execution_id"]
        if wait:
            assert response["status"] == ("failed" if fail else "completed")
        final = await host.wait(first["execution_id"])
        if not fail:
            assert dumps(final["result"]["output"]) == output.read_text()


@pytest.mark.parametrize("text", ["null", "[]", "1", "true"])
async def test_signal_cli_rejects_nonobject_output_before_backend(tmp_path, text):
    output = tmp_path / "output.json"
    output.write_text(text)
    code, stdout, stderr = await invoke(
        "signal", "missing", "__main__", "--output", output, "--address", "127.0.0.1:1"
    )
    assert code == 1 and not stdout
    assert "output must be an object" in stderr


async def test_signal_cli_defaults_to_empty_output(server):
    queue = str(uuid4())
    host = ExecutionService(server.client, queue)
    doc = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    async with create_worker(server.client, queue):
        first = await host.submit(
            "__main__",
            {"__main__": {"packages": {"__main__": {"format": "json", "text": dumps(doc)}}}},
        )
        code, stdout, stderr = await invoke(
            "signal",
            first["execution_id"],
            "__main__",
            "--wait",
            "--address",
            server.client.service_client.config.target_host,
        )
        assert code == 0, stderr
        assert loads(stdout)["result"] == {"branch": None, "output": {}}
