"""Host-side worker assembly. All options affecting replay enter run requests.

Only deterministic native/JSON modules pass through the Workflow sandbox;
business registries live on RunActivities, outside it. Worker kwargs tune SDK
resources, not DSL execution semantics.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .activities import Executor, RunActivities
from .workflow import AxcutorInterpreterWorkflow


def workflow_runner() -> SandboxedWorkflowRunner:
    """Use this same runner for Workers and offline History Replayers."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "axcutor_core", "simplejson"
        )
    )


def create_worker(
    client: Client,
    task_queue: str,
    executors: Mapping[str, Executor] | None = None,
    **kwargs: Any,
) -> Worker:
    """Register the interpreter and executor dispatcher, including builtin Python."""
    activities = RunActivities(executors)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AxcutorInterpreterWorkflow],
        activities=[activities.execute_run],
        workflow_runner=workflow_runner(),
        # Cancellation is delivered through heartbeats. Keep SDK throttling low
        # enough that a configured short heartbeat timeout remains usable.
        max_heartbeat_throttle_interval=timedelta(milliseconds=100),
        default_heartbeat_throttle_interval=timedelta(milliseconds=100),
        **kwargs,
    )
