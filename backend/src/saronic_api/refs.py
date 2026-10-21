"""Resolve a branch/tag/SHA to the exact commit a Brev session will run.

Validation mirrors scripts/brev-bootstrap.sh so the backend never accepts a ref the
Launchable would reject. Instances always receive the resolved 40-hex SHA.
"""

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_REPO_URL = "https://github.com/siddharth-diwakar/saronic-f26"
GIT_TIMEOUT_S = 10

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSAFE_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_PEELED = "^{}"


@dataclass(frozen=True)
class ResolvedRef:
    ref: str
    commit_sha: str


class RefError(Exception):
    """Base class; messages are safe to return to API clients."""


class InvalidRef(RefError):
    pass


class AmbiguousRef(InvalidRef):
    pass


class RefNotFound(RefError):
    pass


class RefResolutionError(RefError):
    """git or the network failed; the ref may well exist."""


def resolve_ref(
    ref: str,
    *,
    repo_url: str = DEFAULT_REPO_URL,
    runner: Runner = subprocess.run,
    timeout_s: float = GIT_TIMEOUT_S,
) -> ResolvedRef:
    if not ref or _UNSAFE_CHARS_RE.search(ref):
        raise InvalidRef("ref must be non-empty and contain no whitespace or control characters")
    if ref.startswith("-"):
        raise InvalidRef("ref must not start with '-'")
    if _FULL_SHA_RE.fullmatch(ref):
        # Passed through unverified; the bootstrap fetches it by SHA.
        sha = ref.lower()
        return ResolvedRef(ref=sha, commit_sha=sha)

    if ref.startswith(("refs/heads/", "refs/tags/")):
        _check_ref_format(ref, runner, timeout_s)
        candidates = [ref]
    elif ref.startswith("refs/"):
        raise InvalidRef(
            "ref must be a branch, a tag, refs/heads/<x>, refs/tags/<x>, or a full SHA"
        )
    else:
        _check_ref_format(f"refs/heads/{ref}", runner, timeout_s)
        candidates = [f"refs/heads/{ref}", f"refs/tags/{ref}"]

    # ls-remote only prints a peeled tag line when that exact pattern is requested.
    patterns = [*candidates, *(f"{c}{_PEELED}" for c in candidates if c.startswith("refs/tags/"))]
    refs = _ls_remote(repo_url, patterns, runner, timeout_s)

    matches = {}
    for candidate in candidates:
        sha = refs.get(f"{candidate}{_PEELED}") or refs.get(candidate)
        if sha:
            matches[candidate] = sha
    if not matches:
        raise RefNotFound(f"ref {ref!r} was not found")
    if len(matches) > 1:
        raise AmbiguousRef(
            f"ref {ref!r} is both a branch and a tag; use refs/heads/{ref} or refs/tags/{ref}"
        )
    return ResolvedRef(ref=ref, commit_sha=next(iter(matches.values())))


def _check_ref_format(full_ref: str, runner: Runner, timeout_s: float) -> None:
    result = _git(["check-ref-format", full_ref], runner, timeout_s)
    if result.returncode != 0:
        raise InvalidRef("ref is not a valid git branch or tag name")


def _ls_remote(
    repo_url: str, patterns: list[str], runner: Runner, timeout_s: float
) -> dict[str, str]:
    # Patterns all start with "refs/", so none can be parsed as an option.
    result = _git(["ls-remote", repo_url, *patterns], runner, timeout_s)
    if result.returncode != 0:
        raise RefResolutionError("could not list refs from the repository")
    refs = {}
    for line in result.stdout.splitlines():
        sha, _, name = line.partition("\t")
        # Pattern matching is by suffix, so keep exact names only.
        if name in patterns and _FULL_SHA_RE.fullmatch(sha):
            refs[name] = sha.lower()
    return refs


def _git(args: list[str], runner: Runner, timeout_s: float) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # Never prompt for credentials; fail instead.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RefResolutionError(f"git {args[0]} timed out after {timeout_s}s") from None
    except OSError:
        raise RefResolutionError("git is not available") from None
