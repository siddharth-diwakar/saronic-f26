"""Runtime configuration; .env is for local development, environment wins."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from saronic_api.refs import DEFAULT_REPO_URL


class Settings(BaseSettings):
    # Aliased fields read only their SARONIC_* env names; pass the alias as a kwarg too.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    app_env: Literal["development", "test", "production"] = "production"
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    # "worker" switches the container healthcheck to the worker heartbeat.
    process_role: Literal["api", "worker"] = "api"

    # Shared key for the session API (X-API-Key). Required when app_env is production.
    api_key: SecretStr | None = Field(default=None, validation_alias="SARONIC_API_KEY")

    # Brev: only the worker needs the API key.
    brev_api_key: SecretStr | None = None
    brev_launchable_id: str = "env-3KBxE5OmaRkasfoXEDUhXeFleBL"
    brev_cli_path: str = "brev"
    brev_command_timeout_s: float = Field(default=120, gt=0)

    repo_url: str = Field(default=DEFAULT_REPO_URL, validation_alias="SARONIC_REPO_URL")
    default_ref: str = Field(default="main", validation_alias="SARONIC_DEFAULT_REF")
    instance_prefix: str = Field(
        default="saronic",
        validation_alias="SARONIC_INSTANCE_PREFIX",
        min_length=1,
        max_length=40,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    max_active_sessions: int = Field(default=1, ge=1)
    worker_poll_interval_s: float = Field(default=10, gt=0)
    worker_heartbeat_path: str = "/tmp/saronic-worker-heartbeat"
    # How long provisioning may last before the instance is deleted.
    session_startup_timeout_s: float = Field(default=45 * 60, gt=0)
    # How long a running session may go without a reported step or log.
    session_idle_timeout_s: float = Field(default=20 * 60, gt=0)
    # Cap on how long a session may stay running.
    session_max_duration_s: float = Field(default=2 * 60 * 60, gt=0)

    # Bridge tunnels (worker only): one SSH port forward per running session to the
    # simulator's control bridge, which listens on the instance's 127.0.0.1.
    bridge_tunnels_enabled: bool = True
    # The bridge's port on the instance (HARBOR_CONTROL_PORT in compose.stream.yaml).
    bridge_remote_port: int = Field(default=8212, ge=1, le=65535)
    # `brev shell --host` alias suffix: the VM itself, where the host-networked sim listens,
    # rather than a Brev workspace container.
    bridge_ssh_host_suffix: str = "-host"
    # Where the session file goes on the instance; relative paths are under the SSH user's home.
    # The sim container reads it as /workspace/output/.bridge-session.json.
    bridge_remote_session_file: str = Field(
        default="saronic-f26/output/.bridge-session.json", min_length=1
    )
    # Interface the worker's forwarded ports listen on, and the address the API uses to
    # reach them (default: the worker container's own IP, or 127.0.0.1 for a loopback bind).
    bridge_tunnel_bind_host: str = "0.0.0.0"
    bridge_tunnel_advertise_host: str | None = None
    bridge_tunnel_port_start: int = Field(default=18200, ge=1024, le=65000)
    bridge_ssh_path: str = "ssh"
    bridge_request_timeout_s: float = Field(default=5, gt=0)

    # Optional Slack webhook for session failure alerts.
    slack_webhook_url: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
                raise ValueError
            if not url.host or not url.database or not url.username:
                raise ValueError
        except (ArgumentError, ValueError):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL with user, host, and database"
            ) from None
        return value

    @field_validator("slack_webhook_url", "bridge_tunnel_advertise_host", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("slack_webhook_url")
    @classmethod
    def webhook_is_http(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        url = value.get_secret_value()
        if not url.startswith(("https://", "http://")):
            raise ValueError("SLACK_WEBHOOK_URL must be an HTTP URL")
        return value
