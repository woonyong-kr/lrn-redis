"""Compare against a private, disposable Redis container, never an existing DB."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.compatibility import cases, canonicalize
from demo import running

IMAGE = "redis:7.4.9-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"


@contextmanager
def reference():
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            "127.0.0.1::6379",
            IMAGE,
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        text=True,
    ).strip()
    try:
        ports = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .NetworkSettings.Ports}}",
                    container,
                ],
                text=True,
            )
        )
        port = int(ports["6379/tcp"][0]["HostPort"])
        client = redis.Redis(
            host="127.0.0.1",
            port=port,
            protocol=2,
            decode_responses=True,
            socket_timeout=2,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield client
        client.close()
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "3", container],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def main():
    reports = []
    with (
        reference() as ref,
        tempfile.TemporaryDirectory() as folder,
        running(Path(folder)) as candidate,
    ):
        for case in cases():
            for client in (ref, candidate):
                client.flushall()
                for setup in case.setup:
                    client.execute_command(*setup)
            expected = canonicalize(case.name, ref.execute_command(*case.command))
            actual = canonicalize(case.name, candidate.execute_command(*case.command))
            matched = (
                abs(expected - actual) <= 1
                if case.name == "TTL"
                else expected == actual
            )
            reports.append(
                {
                    "command": case.name,
                    "matched": matched,
                    "reference": expected,
                    "candidate": actual,
                }
            )
        print(
            json.dumps(
                {
                    "reference_image": IMAGE,
                    "cases": reports,
                    "matched": sum(r["matched"] for r in reports),
                    "total": len(reports),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if not all(r["matched"] for r in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
