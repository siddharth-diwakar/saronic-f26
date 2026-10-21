from enum import StrEnum


class SessionStatus(StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# Count toward MAX_ACTIVE_SESSIONS: anything that may hold (or soon hold) a GPU.
ACTIVE_STATUSES = frozenset(
    {
        SessionStatus.REQUESTED,
        SessionStatus.PROVISIONING,
        SessionStatus.RUNNING,
        SessionStatus.STOPPING,
    }
)
# The worker polls Brev for these.
MONITORED_STATUSES = frozenset(
    {SessionStatus.PROVISIONING, SessionStatus.RUNNING, SessionStatus.STOPPING}
)
TERMINAL_STATUSES = frozenset({SessionStatus.STOPPED, SessionStatus.FAILED})


class SessionStep(StrEnum):
    """Timeline steps. `status` drives the worker; steps record how far startup got."""

    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    INSTANCE_UP = "instance_up"
    CONTAINER_BUILT = "container_built"
    SCENE_LOADED = "scene_loaded"
    PHYSICS_RUNNING = "physics_running"
    CAMERAS_READY = "cameras_ready"
    READY = "ready"
    FAILED = "failed"
    TEARDOWN_REQUESTED = "teardown_requested"
    STOPPED = "stopped"


FORWARD_STEPS: tuple[SessionStep, ...] = (
    SessionStep.REQUESTED,
    SessionStep.PROVISIONING,
    SessionStep.INSTANCE_UP,
    SessionStep.CONTAINER_BUILT,
    SessionStep.SCENE_LOADED,
    SessionStep.PHYSICS_RUNNING,
    SessionStep.CAMERAS_READY,
    SessionStep.READY,
)
IN_PROGRESS_STEPS: frozenset[SessionStep] = frozenset(FORWARD_STEPS) - {SessionStep.READY}
# Steps the instance reports through POST /events; the backend records the rest.
INSTANCE_STEPS: tuple[SessionStep, ...] = FORWARD_STEPS[
    FORWARD_STEPS.index(SessionStep.INSTANCE_UP) + 1 :
]
