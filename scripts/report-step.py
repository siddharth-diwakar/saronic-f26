#!/usr/bin/env python3
"""Report a session step or startup log from a Brev instance to the backend.

Usage:
  report-step.py STEP [--failed-step STEP --error-code CODE [--error-detail TEXT]] [< log tail]
  report-step.py --log SOURCE < log body        (bootstrap, docker_build, isaac_launch)

Does nothing unless SARONIC_BACKEND_URL, SARONIC_SESSION_ID, and
SARONIC_INGEST_TOKEN are all set, and never fails the caller: startup must not
depend on the backend being reachable. A failed step reads its log tail from
stdin. The token travels in a header, never in arguments or output.
"""
import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ATTEMPTS = 3
TIMEOUT_SECONDS = 10
MAX_LOG_LINES = 200  # The backend keeps the last 200 lines; send no more.


def read_tail():
    if sys.stdin.isatty():
        return None
    return "\n".join(sys.stdin.read().splitlines()[-MAX_LOG_LINES:])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", nargs="?")
    parser.add_argument("--failed-step")
    parser.add_argument("--error-code")
    parser.add_argument("--error-detail")
    parser.add_argument("--log", metavar="SOURCE", help="Send stdin as a startup log instead of a step")
    args = parser.parse_args()
    if bool(args.step) == bool(args.log):
        parser.error("give either a STEP or --log SOURCE")
    backend = os.environ.get("SARONIC_BACKEND_URL", "").rstrip("/")
    session = os.environ.get("SARONIC_SESSION_ID", "")
    token = os.environ.get("SARONIC_INGEST_TOKEN", "")
    if not (backend and session and token):
        return
    if args.log:
        path, label = "logs", f"{args.log} log"
        body = {"source": args.log, "body": read_tail() or ""}
    else:
        path, label = "events", f"step {args.step}"
        body = {"step": args.step}
        for key in ("failed_step", "error_code", "error_detail"):
            if getattr(args, key):
                body[key] = getattr(args, key)
        if args.step == "failed":
            tail = read_tail()
            if tail:
                body["log_tail"] = tail
    request = Request(
        f"{backend}/sessions/{quote(session, safe='')}/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Ingest-Token": token},
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS):
                print(f"Reported {label}", file=sys.stderr)
                return
        except HTTPError as error:
            # 4xx (bad token, out-of-order step) will not succeed on retry.
            print(f"Backend rejected {label}: HTTP {error.code}", file=sys.stderr)
            if error.code < 500:
                return
        except (URLError, OSError) as error:
            print(f"Could not report {label}: {type(error).__name__}", file=sys.stderr)
        if attempt < ATTEMPTS:
            time.sleep(2 * attempt)


if __name__ == "__main__":
    main()
