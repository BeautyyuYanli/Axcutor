"""Protocol-independent execution management; adapters live in CLI and Playground."""

from axcutor_runtime.bundle import CompilationError

from .host import (
    AxcutorHost,
    Execution,
    ExecutionAlreadyExists,
    ExecutionNotFound,
    HostError,
    InputValidationError,
    InvalidRequest,
    RuntimeUnavailable,
    SignalNotWaiting,
    SignalValidationError,
)

__all__ = [
    "AxcutorHost",
    "CompilationError",
    "Execution",
    "ExecutionAlreadyExists",
    "ExecutionNotFound",
    "HostError",
    "InputValidationError",
    "InvalidRequest",
    "RuntimeUnavailable",
    "SignalNotWaiting",
    "SignalValidationError",
]
