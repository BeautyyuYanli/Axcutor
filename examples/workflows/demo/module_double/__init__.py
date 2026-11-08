"""Preinstalled module example, using the colocated generated contract."""

from ._contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    return Output(value=input.value * 2)
