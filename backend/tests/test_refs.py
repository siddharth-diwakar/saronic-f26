import subprocess

import pytest

from saronic_api.refs import (
    AmbiguousRef,
    InvalidRef,
    RefNotFound,
    RefResolutionError,
    ResolvedRef,
    resolve_ref,
)

REPO = "https://example.invalid/saronic.git"
SHA_MAIN = "1" * 40
SHA_TAG_OBJECT = "2" * 40
SHA_TAGGED = "3" * 40
SHA_FEATURE = "4" * 40
SHA_SHARED = "5" * 40

REMOTE = {
    "refs/heads/main": SHA_MAIN,
    "refs/heads/team/feature": SHA_FEATURE,
    "refs/tags/light": SHA_MAIN,
    "refs/tags/v1": SHA_TAG_OBJECT,
    "refs/tags/v1^{}": SHA_TAGGED,
    "refs/heads/shared": SHA_SHARED,
    "refs/tags/shared": SHA_SHARED,
    # Suffix matches that must be ignored.
    "refs/remotes/origin/main": "6" * 40,
    "refs/heads/x/refs/heads/main": "7" * 40,
}


class FakeGit:
    """Real `git check-ref-format`; canned `git ls-remote`."""

    def __init__(self, remote=REMOTE, ls_remote=None):
        self.remote = remote
        self.ls_remote = ls_remote
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1] == "check-ref-format":
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        assert argv[:3] == ["git", "ls-remote", REPO]
        if self.ls_remote is not None:
            if isinstance(self.ls_remote, BaseException):
                raise self.ls_remote
            return self.ls_remote
        # Emulate git's suffix matching; the resolver must filter exact names.
        lines = [
            f"{sha}\t{name}"
            for name, sha in self.remote.items()
            if any(name == p or name.endswith(f"/{p}") for p in argv[3:])
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    @property
    def ls_remote_argv(self):
        return next(argv for argv, _ in self.calls if argv[1] == "ls-remote")


def resolve(ref, git=None):
    return resolve_ref(ref, repo_url=REPO, runner=git or FakeGit())


def test_branch():
    git = FakeGit()
    assert resolve("main", git) == ResolvedRef("main", SHA_MAIN)
    assert git.ls_remote_argv[3:] == ["refs/heads/main", "refs/tags/main", "refs/tags/main^{}"]
    _, kwargs = git.calls[-1]
    assert kwargs["timeout"] == 10
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "shell" not in kwargs


def test_branch_with_slash():
    assert resolve("team/feature") == ResolvedRef("team/feature", SHA_FEATURE)


def test_lightweight_tag():
    assert resolve("light") == ResolvedRef("light", SHA_MAIN)


def test_annotated_tag_uses_peeled_commit():
    assert resolve("v1") == ResolvedRef("v1", SHA_TAGGED)
    assert resolve("refs/tags/v1") == ResolvedRef("refs/tags/v1", SHA_TAGGED)


def test_explicit_refs_disambiguate():
    git = FakeGit()
    assert resolve("refs/heads/shared", git).commit_sha == SHA_SHARED
    assert git.ls_remote_argv[3:] == ["refs/heads/shared"]
    assert resolve("refs/tags/shared").commit_sha == SHA_SHARED


def test_ambiguous_name():
    with pytest.raises(AmbiguousRef, match="refs/heads/shared"):
        resolve("shared")


def test_not_found():
    with pytest.raises(RefNotFound):
        resolve("missing-branch")
    with pytest.raises(RefNotFound):
        resolve("refs/tags/main")


def test_full_sha_passes_through_without_git():
    git = FakeGit()
    sha = "ABCDEF0123456789abcdef0123456789ABCDEF01"
    assert resolve(sha, git) == ResolvedRef(sha.lower(), sha.lower())
    assert git.calls == []


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "--upload-pack=x",
        "-main",
        "../x",
        "a..b",
        "refs/pull/1/head",
        "refs/remotes/origin/main",
        "main branch",
        " main",
        "main\n",
        "tab\there",
        "nul\x00",
        "a~1",
        "a^",
        "a:b",
        "a@{1}",
        "x.lock",
    ],
)
def test_rejected_input_never_reaches_ls_remote(ref):
    git = FakeGit()
    with pytest.raises(InvalidRef):
        resolve(ref, git)
    assert all(argv[1] != "ls-remote" for argv, _ in git.calls)


def test_abbreviated_sha_is_treated_as_a_name():
    with pytest.raises(RefNotFound):
        resolve("abc1234")


def test_git_failure_and_timeout():
    failed = subprocess.CompletedProcess([], 128, "", "fatal: could not read from remote")
    with pytest.raises(RefResolutionError):
        resolve("main", FakeGit(ls_remote=failed))
    with pytest.raises(RefResolutionError, match="timed out"):
        resolve("main", FakeGit(ls_remote=subprocess.TimeoutExpired(["git"], 10)))


def test_git_missing():
    def no_git(argv, **kwargs):
        raise FileNotFoundError("git")

    with pytest.raises(RefResolutionError, match="not available"):
        resolve("main", no_git)
