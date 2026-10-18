"""Brev instance lifecycle through the brev CLI, authenticated with an API key.

Behaviour verified against brev-cli v0.6.335 source:
- BREV_API_KEY in the environment authenticates without a login step.
- `create --detached` with piped stdout prints only the instance name(s); progress goes to stderr.
- `ls --json` with API-key auth lists every workspace in the key's org, so `--all` is not needed.
- `delete` of a missing instance exits non-zero with "not found" under API-key auth.
"""

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]

INSTANCE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
INSTANCE_NAME_MAX_LEN = 63
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STDERR_TAIL_LINES = 20
_REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class BrevInstance:
    name: str
    id: str
    status: str  # raw Brev value, e.g. "RUNNING"; unknown values are kept as-is
    build_status: str
    health_status: str
    instance_type: str


class BrevError(Exception):
    """A brev CLI call failed. Messages never contain the API key."""


class BrevNameTaken(BrevError):
    """create() was called with a name that already exists."""


class BrevClient(Protocol):
    def create(self, *, name: str, launchable_id: str, params: dict[str, str]) -> str: ...

    def status(self, name: str) -> BrevInstance | None: ...

    def list_instances(self) -> list[BrevInstance]: ...

    def stop(self, name: str) -> None: ...

    def delete(self, name: str) -> None: ...


def _is_instance_name(name: str) -> bool:
    return len(name) <= INSTANCE_NAME_MAX_LEN and bool(INSTANCE_NAME_RE.fullmatch(name))


def validate_instance_name(name: str) -> None:
    if not _is_instance_name(name):
        raise ValueError(
            f"Invalid Brev instance name {name!r}: must match {INSTANCE_NAME_RE.pattern} "
            f"and be at most {INSTANCE_NAME_MAX_LEN} characters"
        )


class CliBrevClient:
    def __init__(
        self,
        *,
        api_key: str,
        cli_path: str = "brev",
        timeout_s: float = 120,
        runner: Runner = subprocess.run,
        list_all: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("A Brev API key is required")
        self._api_key = api_key
        self._cli_path = cli_path
        self._timeout_s = timeout_s
        self._runner = runner
        # API-key `ls` already lists the whole org; the spike uses this to double-check.
        self._list_all = list_all

    def create(self, *, name: str, launchable_id: str, params: dict[str, str]) -> str:
        validate_instance_name(name)
        args = ["create", name, "--launchable", launchable_id]
        for key, value in params.items():
            if not _PARAM_NAME_RE.fullmatch(key):
                raise ValueError(f"Invalid Launchable parameter name {key!r}")
            args += ["--param", f"{key}={value}"]
        args.append("--detached")

        result = self._run(args)
        if result.returncode != 0:
            raise self._failure("create", result)

        lines = (line.strip() for line in result.stdout.splitlines())
        names = [line for line in lines if _is_instance_name(line)]
        if name in names:
            return name
        # Spike will confirm whether Brev ever renames a single-count create.
        if names:
            logger.warning("brev create returned a different name", extra={"instance_name": name})
            return names[-1]
        logger.warning("brev create printed no name; assuming requested name")
        return name

    def status(self, name: str) -> BrevInstance | None:
        return next((i for i in self.list_instances() if i.name == name), None)

    def list_instances(self) -> list[BrevInstance]:
        result = self._run(["ls", "--json", "--all"] if self._list_all else ["ls", "--json"])
        if result.returncode != 0:
            raise self._failure("ls", result)
        return _parse_instances(result.stdout)

    def stop(self, name: str) -> None:
        validate_instance_name(name)
        result = self._run(["stop", name])
        if result.returncode != 0:
            raise self._failure("stop", result)

    def delete(self, name: str) -> None:
        validate_instance_name(name)
        result = self._run(["delete", name])
        if result.returncode == 0:
            return
        if "not found" in _output(result).lower():
            logger.info("Brev instance already absent", extra={"instance_name": name})
            return
        raise self._failure("delete", result)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # The key travels only in env; argv is safe to log.
        argv = [self._cli_path, *args]
        logger.debug("Running brev CLI: %s", argv)
        try:
            return self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # `from None`: the original exception carries captured output.
            raise BrevError(f"brev {args[0]} timed out after {self._timeout_s}s") from None
        except OSError as exc:
            raise BrevError(
                f"Could not run brev CLI at {self._cli_path!r}: {exc.strerror or type(exc).__name__}"
            ) from None

    def _env(self) -> Mapping[str, str]:
        return {**os.environ, "BREV_API_KEY": self._api_key}

    def _failure(self, command: str, result: subprocess.CompletedProcess[str]) -> BrevError:
        output = _output(result)
        tail = "\n".join(output.strip().splitlines()[-_STDERR_TAIL_LINES:])
        message = f"brev {command} exited with {result.returncode}: {tail}".replace(
            self._api_key, _REDACTED
        )
        if "already exists" in output.lower():
            return BrevNameTaken(message)
        return BrevError(message)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    # Errors normally go to stderr; fall back to stdout if the CLI printed nothing there.
    return result.stderr or result.stdout or ""


def _parse_instances(stdout: str) -> list[BrevInstance]:
    # Skip any non-JSON preamble, such as brev's optional "new version" notice on stdout.
    start = stdout.find("{")
    try:
        if start < 0:
            raise ValueError
        data, _ = json.JSONDecoder().raw_decode(stdout, start)
    except ValueError:
        raise BrevError("brev ls returned malformed JSON") from None
    # A missing key means the format changed; reading it as "no instances" would make
    # callers treat live (billing) instances as gone. null is how Go encodes an empty list.
    if not isinstance(data, dict) or "workspaces" not in data:
        raise BrevError("brev ls returned unexpected JSON")
    workspaces = data["workspaces"]
    if workspaces is None:
        workspaces = []
    if not isinstance(workspaces, list):
        raise BrevError("brev ls returned unexpected JSON")

    instances = []
    for workspace in workspaces:
        if not isinstance(workspace, dict) or not workspace.get("name"):
            logger.warning("Skipping unrecognised brev ls entry")
            continue
        instances.append(
            BrevInstance(
                name=str(workspace["name"]),
                id=str(workspace.get("id") or ""),
                status=str(workspace.get("status") or ""),
                build_status=str(workspace.get("build_status") or ""),
                health_status=str(workspace.get("health_status") or ""),
                instance_type=str(workspace.get("instance_type") or ""),
            )
        )
    return instances
