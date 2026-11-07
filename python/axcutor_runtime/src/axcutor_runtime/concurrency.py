"""Owned task cleanup shared by Workflow and Activity code.

Call only while unwinding a failure/timeout/cancellation: repeated cancellation
must not interrupt resource cleanup. The caller retains and raises the cause.
Ordered gather is deterministic; Workflow races use temporalio.workflow.wait.
"""

import asyncio
from collections.abc import Sequence
from typing import Any


async def cancel_and_drain(tasks: Sequence[asyncio.Future[Any]]) -> list[Any]:
    for task in tasks:
        if not task.done():
            task.cancel()
    joined = asyncio.gather(*tasks, return_exceptions=True)
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            continue
    return list(joined.result())
