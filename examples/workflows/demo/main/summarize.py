"""Summarize the ordered results of concurrent child workflows."""

from .summarize_contract import Input, Output, ReturnType


async def execute(input: Input) -> ReturnType:
    return Output(total=sum(input.doubled))
