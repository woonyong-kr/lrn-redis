"""Own and reap only the child server used by this shopping-cart demonstration."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import redis

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def running(folder, **settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINI_REDIS_")}
    env.update(
        MINI_REDIS_APPENDONLY="true",
        MINI_REDIS_AOF_FSYNC="always",
        MINI_REDIS_AOF_FILE=str(folder / "cart.aof"),
    )
    env.update(settings)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = redis.Redis(
        host="127.0.0.1",
        port=port,
        protocol=2,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=0.2,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(
                        proc.stderr.read().decode()
                        if proc.poll() is not None
                        else "server start timeout"
                    )
                time.sleep(0.02)
        yield client
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stderr.close()


def demo():
    with tempfile.TemporaryDirectory(prefix="lrn-redis-") as temp:
        folder = Path(temp)
        with running(folder) as r:
            r.hset("cart:alice", mapping={"book": "2", "pen": "1"})
            r.set("coupon", "discount", px=80)
            assert r.hgetall("cart:alice") == {"book": "2", "pen": "1"}
            deadline = time.monotonic() + 2
            while r.get("coupon") is not None:
                if time.monotonic() > deadline:
                    raise AssertionError("TTL did not expire")
                time.sleep(0.02)
            print(
                json.dumps(
                    {
                        "stage": "write+expiry",
                        "cart": r.hgetall("cart:alice"),
                        "coupon": r.get("coupon"),
                    }
                )
            )
        with running(folder) as r:
            assert r.hgetall("cart:alice") == {"book": "2", "pen": "1"}
            assert r.get("coupon") is None
            print(json.dumps({"stage": "AOF restart", "cart": r.hgetall("cart:alice")}))
        with tempfile.TemporaryDirectory(prefix="lrn-redis-evict-") as evict:
            with running(
                Path(evict),
                MINI_REDIS_MAXMEMORY="800",
                MINI_REDIS_MAXMEMORY_POLICY="allkeys-lru",
            ) as r:
                for i in range(10):
                    r.set(f"item:{i}", "x" * 200)
                keys = r.keys("*")
                assert 0 < len(keys) < 10 and "item:9" in keys
                print(json.dumps({"stage": "eviction", "remaining": keys}))


if __name__ == "__main__":
    demo()
