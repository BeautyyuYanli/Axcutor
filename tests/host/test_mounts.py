"""Mount semantics retain compiler diagnostics and run before backend contact."""

import httpx
import pytest
from axcutor_host import ExecutionService
from axcutor_host.http import create_app
from axcutor_runtime.codec import dumps, loads
from temporalio.client import Client


@pytest.mark.parametrize(
    "change, code, path",
    [
        ("executor", "schema.executor_not_allowed", "/run/executor"),
        ("unknown_resource", "type.unknown_resource", "/input/tx/kind"),
        ("foreign_package", "schema.package_path", ""),
        ("foreign_resource", "schema.package_path", ""),
        ("root", "schema.mount_root", ""),
        ("header", "schema.unknown_field", "/resources/Tx/other"),
        ("invalid_executor", "schema.executor", "/allowed_executors/1"),
    ],
)
async def test_mount_semantic_errors_are_422_before_connecting(change, code, path):
    document = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    unit = {"packages": {}, "allowed_executors": ["signal"]}
    mounts = {"app": unit}
    if change == "executor":
        document["run"] = {"executor": "builtin", "content": "pass"}
    elif change == "unknown_resource":
        document["input"] = {"tx": {"type": "handle", "kind": "infra:Tx"}}
    elif change == "foreign_package":
        unit["packages"]["std._secret"] = {"format": "yaml", "text": ""}
    elif change == "foreign_resource":
        unit["resources"] = {"std._secret": {"format": "yaml", "text": "resources: {Tx: null}"}}
    elif change == "root":
        mounts["_private"] = {}
    elif change == "header":
        unit["resources"] = {"app": {"format": "yaml", "text": "resources: {Tx: {other: true}}"}}
    elif change == "invalid_executor":
        unit["allowed_executors"] = ["signal", "not.valid"]
    unit["packages"]["app"] = {"format": "json", "text": dumps(document)}
    backend = await Client.connect("127.0.0.1:1", lazy=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ExecutionService(backend))),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/executions", content=dumps({"entry": "app", "mounts": mounts})
        )
    assert response.status_code == 422, response.text
    error = loads(response.text)["error"]
    assert error["code"] == "compilation_failed"
    assert any(
        diagnostic["code"] == code and diagnostic["param_path"] == path
        for diagnostic in error["details"]["diagnostics"]
    )
