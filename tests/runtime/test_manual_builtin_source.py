"""Compile manual examples and type-check generated builtin/module contracts."""

import re
import subprocess
import sys
from pathlib import Path

import axcutor_core
from axcutor_runtime.builtin import synthesize_contract_module, synthesize_module
from axcutor_runtime.codec import dumps, loads


def test_complete_manual_examples_generate_standalone_typed_python(tmp_path):
    files = []

    def export(root, address):
        body = root["body"]
        if body["kind"] == "run":
            schemas = (
                body["input_schema"],
                root.get("output", body.get("output", {})),
                root.get("branches", body.get("branches", {})),
            )
            if body["run"]["executor"] == "module":
                source = synthesize_contract_module(*schemas)
            elif body["run"]["executor"] == "builtin":
                source = synthesize_module(*schemas, body["run"]["content"]).source
            else:
                return
            filename = tmp_path / (re.sub(r"[^a-zA-Z0-9_]", "_", address) + ".py")
            filename.write_text(source)
            files.append(str(filename))
        if body["kind"] == "graph":
            for name, node in body["graph"]["nodes"].items():
                export(node, address + "_" + name)

    manual = Path(__file__).resolve().parents[2] / "manual/docs"
    for path in sorted(manual.rglob("*.md")):
        for index, block in enumerate(re.findall(r"```yaml\n(.*?)```", path.read_text(), re.S)):
            result = loads(
                axcutor_core.compile_workflow(
                    dumps(
                        {
                            "entry": "main",
                            "packages": {"main": {"format": "yaml", "text": block}},
                        }
                    )
                )
            )
            # Incomplete fragments and explicitly invalid grammar examples are
            # already covered by the Rust manual tests, rather than executable units.
            if result["bundle"]:
                export(result["bundle"]["workflows"]["main"], f"{path.relative_to(manual)}_{index}")
    assert files
    checked = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", *files],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
