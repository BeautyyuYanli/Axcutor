"""The file API must preserve buffers, reject aliases and preflight before I/O."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from axcutor_playground.coordinator import Playground
from axcutor_playground.http import create_playground_app, openapi_schema
from axcutor_playground.workspace import Workspace
from axcutor_runtime.codec import dumps, loads
from temporalio.client import Client

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def edits(view):
    return [{"path": file["path"], "text": file["text"]} for file in view["files"]]


@pytest.fixture
async def workspace_client(tmp_path):
    backend = await Client.connect("127.0.0.1:1", lazy=True)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitkeep").write_text("", encoding="utf-8")
    workspace = Workspace(root, EXAMPLES)
    playground = Playground(backend, workspace)
    # File/preflight tests need no Worker or Temporal connection.
    workspace.open()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_playground_app(playground)), base_url="http://test"
    ) as client:
        try:
            yield workspace, playground, client
        finally:
            await playground.close()


async def test_revision_conflicts_prevent_saves_and_runs_and_preserve_templates(workspace_client):
    workspace, playground, client = workspace_client
    initial = loads((await client.get("/v1/workspace")).text)
    assert not any(file["path"].startswith(".") for file in initial["files"])
    assert (workspace.root / ".gitkeep").is_file()
    files = edits(initial)
    files.append({"path": "nested/input.json", "text": '{"number":1.0}\r\n'})
    response = await client.put(
        "/v1/workspace",
        content=dumps(
            {
                "revision": initial["revision"],
                "files": files,
            }
        ),
    )
    assert response.status_code == 200, response.text
    saved = loads(response.text)
    assert saved["revision"] != initial["revision"]
    assert (workspace.root / "nested/input.json").read_bytes() == b'{"number":1.0}\r\n'
    assert (workspace.root / ".gitkeep").is_file()
    for method, path, body in [
        (client.put, "/v1/workspace", {"revision": initial["revision"], "files": edits(initial)}),
        (
            client.post,
            "/v1/workspace/executions",
            {
                "revision": initial["revision"],
                "entry": "workflows/demo/main/__axcutor__.yaml",
            },
        ),
    ]:
        response = await method(path, content=dumps(body))
        assert (
            response.status_code == 409
            and loads(response.text)["error"]["code"] == "workspace_conflict"
        )
    assert playground.view() == saved
    assert not playground.execution_id
    assert not (EXAMPLES / "nested/input.json").exists()
    files = [file for file in files if file["path"] != "nested/input.json"]
    await workspace.save(saved["revision"], files)
    assert not (workspace.root / "nested/input.json").exists()
    assert (workspace.root / ".gitkeep").is_file()


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/escape.py",
        "../escape.py",
        "a/../escape.py",
        "a//b.py",
        "./a.py",
        "a\\b.py",
        "a.txt",
        "a.py/",
        "a\0.py",
        ".gitkeep",
        ".host.lock",
        ".workspace-stage/a.py",
        ".workspace-backup/a.py",
    ],
)
async def test_invalid_paths_do_not_change_any_files(workspace_client, path):
    workspace, _, client = workspace_client
    before = await workspace.get()
    response = await client.put(
        "/v1/workspace",
        content=dumps(
            {
                "revision": before["revision"],
                "files": edits(before) + [{"path": path, "text": ""}],
            }
        ),
    )
    assert response.status_code == 400, response.text
    assert await workspace.get() == before


async def test_generated_files_cannot_be_edited_deleted_or_created(workspace_client):
    workspace, _, client = workspace_client
    before = await workspace.get()
    path = next(file["path"] for file in before["files"] if file["generated"])
    variants = [
        [file for file in edits(before) if file["path"] != path],
        [{**file, "text": "changed"} if file["path"] == path else file for file in edits(before)],
        edits(before) + [{"path": "new_contract.py", "text": ""}],
        edits(before) + [{"path": "folder.py", "text": ""}, {"path": "folder.py/a.py", "text": ""}],
    ]
    for files in variants:
        response = await client.put(
            "/v1/workspace",
            content=dumps(
                {
                    "revision": before["revision"],
                    "files": files,
                }
            ),
        )
        assert response.status_code == 400, response.text
        assert await workspace.get() == before


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "fifo"])
async def test_symlinks_and_special_files_are_rejected(workspace_client, tmp_path, kind):
    workspace, _, client = workspace_client
    if kind == "file_link":
        (workspace.root / "escape.py").symlink_to(EXAMPLES / "workflows/demo/__init__.py")
    elif kind == "directory_link":
        (workspace.root / "escape").symlink_to(EXAMPLES, target_is_directory=True)
    else:
        os.mkfifo(workspace.root / "special.py")
    response = await asyncio.wait_for(client.get("/v1/workspace"), 1)
    assert response.status_code == 400


async def test_preflight_diagnostics_and_input_errors_never_submit(workspace_client):
    workspace, playground, client = workspace_client
    view = await workspace.get()
    files = edits(view) + [{"path": "bad.yaml", "text": "input: {}\noutput: {}\ngraph: {}"}]
    view = await workspace.save(view["revision"], files)
    response = await client.post(
        "/v1/workspace/executions",
        content=dumps(
            {
                "entry": "bad.yaml",
                "revision": view["revision"],
            }
        ),
    )
    assert response.status_code == 422
    assert loads(response.text)["error"]["details"]["diagnostics"]
    for value, status, code in (
        ('{"items":[1.0],"delay_seconds":0}', 422, "input_validation_failed"),
        ('{"items":[],"items":[]}', 400, "invalid_json"),
    ):
        response = await client.post(
            "/v1/workspace/executions",
            content='{"entry":"workflows/demo/main/__axcutor__.yaml","revision":'
            + dumps(playground.view()["revision"])
            + ',"input":'
            + value
            + "}",
        )
        assert response.status_code == status, response.text
        assert loads(response.text)["error"]["code"] == code
    assert not playground.execution_id


async def test_preflight_error_returns_generated_files_and_revision(workspace_client):
    workspace, playground, client = workspace_client
    view = await workspace.get()
    files = [
        dict(file, text=file["text"].replace("value: {type: integer}", "value: {type: number}", 1))
        if file["path"] == "workflows/demo/double/__axcutor__.yaml"
        else file
        for file in edits(view)
    ]
    saved = await workspace.save(view["revision"], files)
    response = await client.post(
        "/v1/workspace/executions",
        json={
            "entry": "workflows/demo/main/__axcutor__.yaml",
            "input": {"items": "wrong", "delay_seconds": 0},
            "revision": saved["revision"],
        },
    )
    assert response.status_code == 422, response.text
    state = loads(response.text)["error"]["details"]["workspace"]
    assert state["revision"] != saved["revision"]
    assert not state["busy"]
    response = await client.put(
        "/v1/workspace",
        json={
            "revision": state["revision"],
            "files": edits(state),
        },
    )
    assert response.status_code == 200
    assert not playground.execution_id


@pytest.mark.parametrize("position", ["input", "output"])
async def test_handle_root_preflight_preserves_idle_state_and_latest_trace(
    workspace_client, position
):
    workspace, playground, client = workspace_client
    previous = {
        "execution_id": "previous",
        "execution": None,
        "source": {"entry_path": "previous.yaml", "entry": "previous", "mounts": {}},
        "spans": [],
        "observed_at": None,
        "complete": True,
        "trace_error": None,
    }
    playground.latest = previous
    document = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "pass"}}
    document[position] = {"tx": {"type": ["handle", "null"], "kind": "infra:Tx"}}
    view = await workspace.get()
    saved = await workspace.save(
        view["revision"],
        edits(view)
        + [
            {"path": "workflows/infra/handle/__axcutor__.yaml", "text": dumps(document)},
            {"path": "workflows/infra/__init__.py", "text": ""},
            {"path": "workflows/infra/handle/__init__.py", "text": ""},
            {"path": "workflows/infra/__ax_resources__.yaml", "text": "resources: {Tx: null}\n"},
        ],
    )
    response = await client.post(
        "/v1/workspace/executions",
        json={
            "entry": "workflows/infra/handle/__axcutor__.yaml",
            "input": {"tx": None} if position == "input" else {},
            "revision": saved["revision"],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "input_validation_failed"
    assert "public locator" in response.text
    assert not playground.view()["busy"] and playground.execution_id is None
    assert playground.latest is previous and playground._completion is None
    current = await workspace.get()
    response = await client.put(
        "/v1/workspace", json={"revision": current["revision"], "files": edits(current)}
    )
    assert response.status_code == 200, response.text
    assert playground.latest is previous


async def test_workspace_persists_and_only_one_host_can_own_it(workspace_client):
    workspace, _, _ = workspace_client
    view = await workspace.get()
    await workspace.save(view["revision"], edits(view) + [{"path": "kept.py", "text": "# kept"}])
    other = Workspace(workspace.root, EXAMPLES)
    with pytest.raises(BlockingIOError):
        other.open()
    workspace.close()
    other.open()
    try:
        assert any(f["path"] == "kept.py" for f in (await other.get())["files"])
    finally:
        other.close()


async def test_specialized_schema_reuses_execution_models(workspace_client):
    _, _, client = workspace_client
    response = await client.get("/openapi.json")
    assert loads(response.text) == openapi_schema()
    assert loads(response.text)["components"]["schemas"]["RunWorkspaceRequest"]["properties"][
        "input"
    ] == {"type": "object", "additionalProperties": True, "default": {}}
    assert set(loads(response.text)["components"]["schemas"]["SubmittedSource"]["required"]) == {
        "entry_path",
        "entry",
        "mounts",
    }
    assert "/v1/executions" not in loads(response.text)["paths"]
    assert (await client.post("/v1/executions", content="{}")).status_code == 404
    assert (await client.post("/v1/workspace", content="{}")).status_code == 405
    assert (
        await client.put("/v1/workspace", content='{"revision":1,"revision":2}')
    ).status_code == 400


@pytest.mark.parametrize("value", [None, [], "inputs/demo.json", True, 1])
async def test_run_input_rejects_nonobjects_without_changing_sources(workspace_client, value):
    workspace, playground, client = workspace_client
    before = await workspace.get()
    assert not any(file["path"] == "inputs/demo.json" for file in before["files"])
    response = await client.post(
        "/v1/workspace/executions",
        json={
            "entry": "workflows/demo/main/__axcutor__.yaml",
            "revision": before["revision"],
            "input": value,
        },
    )
    assert response.status_code == 400, response.text
    assert loads(response.text)["error"]["code"] == "invalid_request"
    assert await workspace.get() == before
    assert playground.latest is None and playground.execution_id is None


async def test_omitted_run_input_uses_empty_object_contract_preflight(workspace_client):
    workspace, playground, client = workspace_client
    response = await client.post(
        "/v1/workspace/executions",
        json={
            "entry": "workflows/demo/main/__axcutor__.yaml",
            "revision": (await workspace.get())["revision"],
        },
    )
    assert response.status_code == 422, response.text
    assert loads(response.text)["error"]["code"] == "input_validation_failed"
    assert playground.latest is None and playground.execution_id is None
