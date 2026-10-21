import json
import logging
import subprocess

import pytest

from saronic_api.brev import BrevError, BrevInstance, BrevNameTaken, CliBrevClient

API_KEY = "brev_api_test_key_0123456789"
LAUNCHABLE = "env-3KBxE5OmaRkasfoXEDUhXeFleBL"
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def argv(self):
        return self.calls[-1][0]


def done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def client(*results):
    runner = FakeRunner(*results)
    return CliBrevClient(api_key=API_KEY, cli_path="/usr/local/bin/brev", runner=runner), runner


def ls_json(*workspaces, **extra):
    return json.dumps({"workspaces": list(workspaces), **extra})


WORKSPACE = {
    "name": "saronic-abc",
    "id": "ws-1",
    "status": "RUNNING",
    "build_status": "COMPLETED",
    "shell_status": "READY",
    "health_status": "HEALTHY",
    "instance_type": "g6e.xlarge",
    "instance_kind": "gpu",
    "gpu": "L40S",
}


def test_create_argv_and_piped_name():
    brev, runner = client(done(stdout="saronic-abc\n", stderr="Creating instance...\n"))
    name = brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={"SARONIC_REF": SHA})
    assert name == "saronic-abc"
    assert runner.argv == [
        "/usr/local/bin/brev",
        "create",
        "saronic-abc",
        "--launchable",
        LAUNCHABLE,
        "--param",
        f"SARONIC_REF={SHA}",
        "--detached",
    ]


def test_subprocess_options_and_key_only_in_env(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    brev, runner = client(done(stdout=ls_json()))
    brev.list_instances()
    argv, kwargs = runner.calls[0]
    assert not any(API_KEY in arg for arg in argv)
    assert kwargs["env"]["BREV_API_KEY"] == API_KEY
    assert kwargs["env"]["PATH"] == "/usr/bin"
    assert kwargs["timeout"] == 120
    assert kwargs["capture_output"] and kwargs["text"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "shell" not in kwargs


def test_create_falls_back_when_stdout_differs_or_is_empty():
    brev, _ = client(
        done(stdout="saronic-abc-1\n"),
        done(stdout=""),
        done(stdout="A new version of brev has been released!\n"),
    )
    assert brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={}) == "saronic-abc-1"
    assert brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={}) == "saronic-abc"
    assert brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={}) == "saronic-abc"


def test_list_all_flag():
    runner = FakeRunner(done(stdout=ls_json()))
    CliBrevClient(api_key=API_KEY, runner=runner, list_all=True).list_instances()
    assert runner.argv == ["brev", "ls", "--json", "--all"]


@pytest.mark.parametrize("name", ["-rm", "a b", "x" * 64, "", ".hidden", "a/b"])
def test_invalid_instance_names_rejected_before_running(name):
    brev, runner = client()
    with pytest.raises(ValueError):
        brev.create(name=name, launchable_id=LAUNCHABLE, params={})
    with pytest.raises(ValueError):
        brev.delete(name)
    assert runner.calls == []


def test_invalid_param_name_rejected():
    brev, runner = client()
    with pytest.raises(ValueError):
        brev.create(name="ok", launchable_id=LAUNCHABLE, params={"A=B": "x"})
    assert runner.calls == []


def test_create_existing_name_raises_name_taken():
    stderr = "Error: workspace 'saronic-abc' already exists. Use a different name\n"
    brev, _ = client(done(returncode=1, stderr=stderr))
    with pytest.raises(BrevNameTaken):
        brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={})


def test_ls_parses_known_fields_ignores_nodes_and_keeps_unknown_status():
    odd = {"name": "saronic-new", "status": "HIBERNATING", "id": None}
    stdout = ls_json(WORKSPACE, odd, nodes=[{"name": "node-1"}])
    brev, runner = client(done(stdout=stdout))
    assert brev.list_instances() == [
        BrevInstance("saronic-abc", "ws-1", "RUNNING", "COMPLETED", "HEALTHY", "g6e.xlarge"),
        BrevInstance("saronic-new", "", "HIBERNATING", "", "", ""),
    ]
    assert runner.argv == ["/usr/local/bin/brev", "ls", "--json"]


def test_ls_tolerates_preamble_null_workspaces_and_bad_entries():
    stdout = "A new version of brev has been released!\n" + json.dumps(
        {"workspaces": [WORKSPACE, "junk", {"status": "RUNNING"}]}
    )
    brev, _ = client(done(stdout=stdout), done(stdout='{"workspaces": null}'))
    assert [i.name for i in brev.list_instances()] == ["saronic-abc"]
    assert brev.list_instances() == []


@pytest.mark.parametrize(
    "stdout", ["", "not json", "[1, 2]", '{"workspaces": {}}', '{"nodes": []}', "{bad"]
)
def test_ls_malformed_json(stdout):
    brev, _ = client(done(stdout=stdout))
    with pytest.raises(BrevError, match="brev ls"):
        brev.list_instances()


def test_status_finds_by_name_or_none():
    brev, _ = client(done(stdout=ls_json(WORKSPACE)), done(stdout=ls_json(WORKSPACE)))
    assert brev.status("saronic-abc").id == "ws-1"
    assert brev.status("saronic-missing") is None


def test_stop_and_delete_argv():
    brev, runner = client(done(), done())
    brev.stop("saronic-abc")
    assert runner.calls[0][0] == ["/usr/local/bin/brev", "stop", "saronic-abc"]
    brev.delete("saronic-abc")
    assert runner.calls[1][0] == ["/usr/local/bin/brev", "delete", "saronic-abc"]


def test_delete_not_found_is_success_other_errors_raise():
    brev, _ = client(
        done(returncode=1, stderr="Error: workspace saronic-abc not found\n"),
        done(returncode=1, stderr="Error: 500 internal\n"),
    )
    brev.delete("saronic-abc")
    with pytest.raises(BrevError, match="500 internal") as excinfo:
        brev.delete("saronic-abc")
    assert not isinstance(excinfo.value, BrevNameTaken)


def test_stop_not_found_raises():
    brev, _ = client(done(returncode=1, stderr="workspace not found"))
    with pytest.raises(BrevError):
        brev.stop("saronic-abc")


def test_nonzero_exit_keeps_last_20_stderr_lines_and_redacts_key():
    lines = [f"line {i}" for i in range(30)] + [f"auth failed for key {API_KEY}"]
    brev, _ = client(done(returncode=2, stderr="\n".join(lines)))
    with pytest.raises(BrevError) as excinfo:
        brev.list_instances()
    message = str(excinfo.value)
    assert "exited with 2" in message
    assert "line 10" not in message and "line 11" in message
    assert API_KEY not in message and "[REDACTED]" in message


def test_nonzero_exit_uses_stdout_when_stderr_empty():
    brev, _ = client(done(returncode=1, stdout="boom"))
    with pytest.raises(BrevError, match="boom"):
        brev.stop("saronic-abc")


def test_timeout_raises_brev_error_without_output():
    timeout = subprocess.TimeoutExpired(["brev"], 120, output=API_KEY, stderr=API_KEY)
    brev, _ = client(timeout)
    with pytest.raises(BrevError, match="timed out after 120") as excinfo:
        brev.list_instances()
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert API_KEY not in str(excinfo.value)


def test_missing_cli_raises_brev_error():
    brev, _ = client(FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(BrevError, match="Could not run brev CLI"):
        brev.list_instances()


def test_key_never_logged_or_in_repr(caplog):
    caplog.set_level(logging.DEBUG, logger="saronic_api.brev")
    brev, _ = client(
        done(stdout="other-name\n"),
        done(returncode=1, stderr="workspace not found"),
        done(returncode=1, stderr=f"bad key {API_KEY}"),
    )
    brev.create(name="saronic-abc", launchable_id=LAUNCHABLE, params={"SARONIC_REF": SHA})
    brev.delete("saronic-abc")
    with pytest.raises(BrevError):
        brev.delete("saronic-abc")
    assert caplog.records
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(brev)


def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        CliBrevClient(api_key="")
