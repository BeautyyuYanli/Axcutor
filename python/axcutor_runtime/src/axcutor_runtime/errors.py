"""Explicit error selectors cross Activity/ChildWorkflow boundaries by type.

Only Recoverable enters DSL retry/catch. Cancellation and engine failures always
unwind. Unknown Temporal failures are engine errors, never guessed from messages.
"""

from temporalio import exceptions

SELECTORS = ("execution", "timeout", "contract", "max_steps")


class Recoverable(Exception):
    def __init__(self, selector: str, message: str = "") -> None:
        if selector not in SELECTORS:
            raise ValueError(f"unknown selector {selector}")
        self.selector = selector
        super().__init__(message or selector)


class EngineError(Exception):
    """An invariant or non-finite arithmetic error, outside DSL recovery."""


class StackOverflowError(EngineError):
    pass


class MaxStepsExceededError(EngineError):
    pass


def boundary_error(error: exceptions.FailureError) -> Exception:
    """Unwrap only SDK cause wrappers and recognized application types."""
    cause: BaseException = error
    while isinstance(cause, (exceptions.ActivityError, exceptions.ChildWorkflowError)):
        if cause.cause is None:
            return EngineError("Temporal boundary has no cause")
        cause = cause.cause
    if isinstance(cause, exceptions.CancelledError):
        return cause
    if isinstance(cause, exceptions.TimeoutError):
        return Recoverable("timeout", str(cause))
    if isinstance(cause, exceptions.ApplicationError):
        if cause.type in SELECTORS:
            return Recoverable(cause.type, cause.message)
        if cause.type == "StackOverflowError":
            return StackOverflowError(cause.message)
        if cause.type == "MaxStepsExceededError":
            return MaxStepsExceededError(cause.message)
        return EngineError(f"{cause.type}: {cause.message}")
    return EngineError(f"unexpected Temporal failure: {type(cause).__name__}")
