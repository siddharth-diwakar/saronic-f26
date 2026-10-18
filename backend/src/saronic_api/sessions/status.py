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
