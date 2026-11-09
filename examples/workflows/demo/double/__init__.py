"""A module Activity; the delay makes live frames visible in the workbench."""

import asyncio

from ._contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    await asyncio.sleep(input.delay_seconds)
    return Output(value=input.value * 2)
