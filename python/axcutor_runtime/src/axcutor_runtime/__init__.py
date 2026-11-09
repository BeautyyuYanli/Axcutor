"""Compiler snapshots and deterministic Temporal execution.

Import worker/activity assembly from their own modules outside Workflow code.
The package initializer stays free of worker imports for sandbox isolation.
"""

from typing import TYPE_CHECKING, Any

from .bundle import CompilationError, compile_bundle
from .workflow import AxcutorInterpreterWorkflow

if TYPE_CHECKING:
    from .resources import Handle, resource, resources

__all__ = [
    "AxcutorInterpreterWorkflow",
    "CompilationError",
    "compile_bundle",
    "Handle",
    "resource",
    "resources",
]


def __getattr__(name: str) -> Any:
    # Workflow sandbox imports must not load Worker hooks or business packages.
    if name in {"Handle", "resource", "resources"}:
        import importlib

        module = importlib.import_module(".resources", __name__)
        for export in ("Handle", "resource", "resources"):
            globals()[export] = getattr(module, export)
        return globals()[name]
    raise AttributeError(name)
