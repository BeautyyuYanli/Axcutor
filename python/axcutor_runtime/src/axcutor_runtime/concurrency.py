"""Owned task cleanup shared by Workflow and Activity code.

Clean up owned tasks on both successful and exceptional exits. Repeated
cancellation must not interrupt cleanup. On exceptional exits, the caller
retains and raises the original cause.
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
