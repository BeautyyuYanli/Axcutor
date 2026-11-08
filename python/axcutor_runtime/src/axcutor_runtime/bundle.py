"""Compile immutable package snapshots before submitting any Workflow execution."""

from typing import Any

import axcutor_core

from .codec import JsonObject, dumps, loads


class CompilationError(ValueError):
    """Carries the original structured Rust diagnostics and result text."""

    def __init__(self, result_json: str) -> None:
        self.result_json = result_json
        self.diagnostics: list[JsonObject] = loads(result_json)["diagnostics"]
        summary = "; ".join(f"{d['code']}: {d['message']}" for d in self.diagnostics)
        super().__init__(f"Axcutor compilation failed: {summary}")


def compile_bundle(entry: str, packages: dict[str, dict[str, Any]]) -> str:
    """Compile a dotted package map without filesystem I/O or Python imports."""
    result_json = axcutor_core.compile_workflow(dumps({"entry": entry, "packages": packages}))
    result = loads(result_json)
    if result["bundle"] is None:
        raise CompilationError(result_json)
    return dumps(result["bundle"])
