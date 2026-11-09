"""Host-side worker assembly. All options affecting replay enter run requests.

Only deterministic native/JSON modules pass through the Workflow sandbox;
business registries live on RunActivities, outside it. Worker kwargs tune SDK
resources, not DSL execution semantics.
"""

from collections.abc import Mapping
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
    *,
    module_mounts: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Worker:
    """Register executors and pass resource/throttle kwargs to the SDK unchanged.

    SDK defaults apply unless the host overrides them. Fast cancellation needs
    both a short application heartbeat interval in the execution request and a
    short max_heartbeat_throttle_interval here.
    """
    activities = RunActivities(executors, module_mounts=module_mounts)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AxcutorInterpreterWorkflow],
        activities=[activities.execute_run],
        workflow_runner=workflow_runner(),
        **kwargs,
    )
