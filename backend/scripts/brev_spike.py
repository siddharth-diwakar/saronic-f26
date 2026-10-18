"""One-off measurement of real Brev behaviour for the repo's Launchable.

THIS CREATES A REAL L40S INSTANCE AND COSTS MONEY. Run it by hand, once:

    cd backend
    BREV_API_KEY=... uv run --frozen python scripts/brev_spike.py --yes-this-costs-money

It deploys the Launchable at a resolved commit, polls `brev ls` every 15 s, writes one JSON
line per poll to spike-<timestamp>.jsonl, then deletes the instance (unless --keep) and
records how long deletion took. See backend/README.md for what to do with the results.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from saronic_api.brev import BrevError, BrevInstance, CliBrevClient
from saronic_api.refs import DEFAULT_REPO_URL, RefError, resolve_ref

DEFAULT_LAUNCHABLE_ID = "env-3KBxE5OmaRkasfoXEDUhXeFleBL"
FAILED_STATUSES = {"FAILURE"}
FAILED_BUILD_STATUSES = {"CREATE_FAILED"}


def main() -> int:
    args = parse_args()
    if not args.yes_this_costs_money:
        print("Refusing to run: this creates a real GPU instance. Pass --yes-this-costs-money.")
        return 2
    api_key = os.environ.get("BREV_API_KEY")
    if not api_key:
        print("Set BREV_API_KEY.")
        return 2

    try:
        resolved = resolve_ref(
            args.ref, repo_url=os.environ.get("SARONIC_REPO_URL", DEFAULT_REPO_URL)
        )
    except RefError as exc:
        print(f"Could not resolve --ref: {exc}")
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"saronic-spike-{stamp}"
    cli = {"api_key": api_key, "cli_path": os.environ.get("BREV_CLI_PATH", "brev")}
    brev = CliBrevClient(**cli)
    brev_all = CliBrevClient(**cli, list_all=True)
    spike = Spike(Path(f"spike-{stamp}.jsonl"))
    launchable_id = os.environ.get("BREV_LAUNCHABLE_ID", DEFAULT_LAUNCHABLE_ID)

    print(f"Instance {name}: ref {resolved.ref} -> {resolved.commit_sha}")
    print(f"Recording to {spike.path}")
    spike.record(
        "config",
        name=name,
        ref=resolved.ref,
        commit_sha=resolved.commit_sha,
        launchable_id=launchable_id,
        poll_s=args.poll_s,
    )
    may_exist = False
    try:
        # Set before the call: a create that times out may still have created the instance.
        may_exist = True
        returned = brev.create(
            name=name, launchable_id=launchable_id, params={"SARONIC_REF": resolved.commit_sha}
        )
        spike.record("created", returned_name=returned, same_name=returned == name)
        if returned != name:
            print(f"NOTE: brev create returned {returned!r}, not {name!r}")
            name = returned
        if poll_until_ready(brev, brev_all, spike, name, args):
            print("\nCheck the viewer at http://<instance-ip>:8210 and note when the harbor loads.")
            print("brev ls does not show the IP; find it in the Brev console.")
            if not args.keep:
                # Keep polling (health may change) for a bounded window, then delete.
                print(f"Deleting in {args.viewer_min} min; Ctrl-C to delete now.")
                poll_for(brev, spike, name, args.viewer_min * 60, args.poll_s)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        spike.record("interrupted")
    except BrevError as exc:
        print(f"Brev error: {exc}")
        spike.record("brev_error", message=str(exc))
    finally:
        if may_exist and not args.keep:
            delete_and_wait(brev, spike, name, args)
        elif may_exist:
            print(f"--keep: instance {name} is still running and billing. Delete it with:")
            print(f"  brev delete {name}")
        spike.summary()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes-this-costs-money", action="store_true")
    parser.add_argument("--ref", default="main", help="branch, tag or full SHA (default: main)")
    parser.add_argument("--timeout-min", type=float, default=30)
    parser.add_argument(
        "--viewer-min", type=float, default=10, help="minutes to keep it up for the viewer check"
    )
    parser.add_argument("--delete-timeout-min", type=float, default=15)
    parser.add_argument("--poll-s", type=float, default=15)
    parser.add_argument("--keep", action="store_true", help="don't delete the instance")
    return parser.parse_args()


class Spike:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.start = time.monotonic()
        self.transitions: list[tuple[float, str, str, str]] = []
        self.first_running: float | None = None
        self.first_completed: float | None = None
        self.seen_in_plain_ls: bool | None = None
        self.seen_only_with_all = False
        self.delete_s: float | None = None

    def elapsed(self) -> float:
        return round(time.monotonic() - self.start, 1)

    def record(self, event: str, **fields) -> None:
        line = {"t": self.elapsed(), "at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self.path.open("a") as out:
            out.write(json.dumps(line) + "\n")

    def observe(self, instance: BrevInstance | None, phase: str) -> None:
        state = (
            (instance.status, instance.build_status, instance.health_status)
            if instance
            else ("<absent>", "", "")
        )
        self.record(
            "poll",
            phase=phase,
            found=instance is not None,
            status=state[0],
            build_status=state[1],
            health_status=state[2],
            id=instance.id if instance else None,
            instance_type=instance.instance_type if instance else None,
        )
        if not self.transitions or self.transitions[-1][1:] != state:
            t = self.elapsed()
            self.transitions.append((t, *state))
            print(f"[{t:7.1f}s] {phase}: status={state[0]} build={state[1]!r} health={state[2]!r}")
        if instance and instance.status == "RUNNING" and self.first_running is None:
            self.first_running = self.elapsed()
        if instance and instance.build_status == "COMPLETED" and self.first_completed is None:
            self.first_completed = self.elapsed()

    def summary(self) -> None:
        print("\n=== Spike summary ===")
        print("Ordered (status, build_status, health_status):")
        for t, status, build, health in self.transitions:
            print(f"  {t:7.1f}s  {status:12} {build:14} {health}")
        print(f"Time to RUNNING:        {fmt(self.first_running)}")
        print(f"Time to build COMPLETED: {fmt(self.first_completed)}")
        print(f"Delete took:            {fmt(self.delete_s)}")
        print(f"Seen by `brev ls` without --all: {self.seen_in_plain_ls}")
        if self.seen_only_with_all and not self.seen_in_plain_ls:
            print("  WARNING: only `ls --all` showed it; the worker must use --all.")
        elif self.seen_only_with_all:
            print("  NOTE: early polls showed it only with --all; plain ls caught up later.")
        print(f"Raw log: {self.path}")
        self.record(
            "summary",
            transitions=self.transitions,
            time_to_running_s=self.first_running,
            time_to_completed_s=self.first_completed,
            delete_s=self.delete_s,
            seen_in_plain_ls=self.seen_in_plain_ls,
            seen_only_with_all=self.seen_only_with_all,
        )


def poll_until_ready(brev, brev_all, spike: Spike, name: str, args) -> bool:
    deadline = time.monotonic() + args.timeout_min * 60
    ready_polls = 0
    while time.monotonic() < deadline:
        try:
            instance = find(brev.list_instances(), name)
            if instance is not None:
                spike.seen_in_plain_ls = True
            elif not spike.seen_in_plain_ls:
                spike.seen_in_plain_ls = False
                instance = find(brev_all.list_instances(), name)
                spike.seen_only_with_all = spike.seen_only_with_all or instance is not None
        except BrevError as exc:
            print(f"ls failed: {exc}")
            spike.record("ls_error", message=str(exc))
            time.sleep(args.poll_s)
            continue
        spike.observe(instance, "up")
        if instance and (
            instance.status in FAILED_STATUSES or instance.build_status in FAILED_BUILD_STATUSES
        ):
            print("Instance failed; stopping early.")
            return False
        ready = instance and instance.status == "RUNNING" and instance.build_status == "COMPLETED"
        ready_polls = ready_polls + 1 if ready else 0
        if ready_polls >= 2:
            print("RUNNING + COMPLETED for two polls.")
            return True
        time.sleep(args.poll_s)
    print(f"Timed out after {args.timeout_min} min.")
    spike.record("timeout")
    return False


def poll_for(brev: CliBrevClient, spike: Spike, name: str, seconds: float, poll_s: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            spike.observe(find(brev.list_instances(), name), "viewer")
        except BrevError as exc:
            print(f"ls failed: {exc}")


def delete_and_wait(brev: CliBrevClient, spike: Spike, name: str, args) -> None:
    print(f"\nDeleting {name}...")
    started = time.monotonic()
    deadline = started + args.delete_timeout_min * 60
    try:
        brev.delete(name)
        spike.record("delete_issued")
        while time.monotonic() < deadline:
            time.sleep(args.poll_s)
            try:
                instance = find(brev.list_instances(), name)
            except BrevError as exc:
                print(f"ls failed: {exc}")
                continue
            spike.observe(instance, "delete")
            if instance is None:
                spike.delete_s = round(time.monotonic() - started, 1)
                spike.record("deleted", delete_s=spike.delete_s)
                return
        print(f"Still present after {args.delete_timeout_min} min.")
    except (BrevError, KeyboardInterrupt) as exc:
        print(f"Delete did not finish: {exc!r}")
    print(f"CHECK THE BREV CONSOLE and run `brev delete {name}` if it still exists.")


def find(instances: list[BrevInstance], name: str) -> BrevInstance | None:
    return next((i for i in instances if i.name == name), None)


def fmt(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds:.0f}s ({seconds / 60:.1f} min)"


if __name__ == "__main__":
    sys.exit(main())
