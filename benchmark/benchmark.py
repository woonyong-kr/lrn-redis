"""Strict Redis versus mini-redis latency comparison.

Every declared scenario must run for both services. Connection failures,
unsupported commands, failed postconditions, and operation errors terminate the
process with a non-zero exit code instead of disappearing from the report.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from random import Random
from typing import Callable

import redis


RESULTS_DIR = Path("results")
REFERENCE_IMAGE = os.getenv(
    "REDIS_REFERENCE_IMAGE",
    "redis:7.4.9-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99",
)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class BenchmarkConfig:
    rounds: int = _env_int("BENCH_ROUNDS", 5)
    iterations: int = _env_int("BENCH_ITERATIONS", 1000)
    warmup: int = _env_int("BENCH_WARMUP", 100, minimum=0)
    key_count: int = _env_int("BENCH_KEY_COUNT", 200)
    value_size: int = _env_int("BENCH_VALUE_SIZE", 64)
    pipeline_batch: int = _env_int("BENCH_PIPELINE_BATCH", 50)
    leaderboard_players: int = _env_int("BENCH_LEADERBOARD_PLAYERS", 500)
    socket_timeout_seconds: int = _env_int("BENCH_SOCKET_TIMEOUT_SECONDS", 5)


CONFIG = BenchmarkConfig()


@dataclass
class ScenarioResult:
    service: str
    scenario: str
    iterations: int
    round: int = 1
    operations_per_iteration: int = 1
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list, repr=False)

    def percentile(self, fraction: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * fraction), len(ordered) - 1)
        return ordered[index]

    def to_record(self) -> dict[str, object]:
        elapsed_seconds = sum(self.latencies_ms) / 1000
        operations = self.iterations * self.operations_per_iteration
        return {
            "service": self.service,
            "scenario": self.scenario,
            "round": self.round,
            "iterations": self.iterations,
            "operations": operations,
            "errors": self.errors,
            "mean_ms": round(statistics.mean(self.latencies_ms), 4),
            "p50_ms": round(statistics.median(self.latencies_ms), 4),
            "p95_ms": round(self.percentile(0.95), 4),
            "p99_ms": round(self.percentile(0.99), 4),
            "throughput_ops_sec": round(operations / elapsed_seconds, 1),
        }


def _seed(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big")


def _values(name: str, count: int, size: int | None = None) -> list[str]:
    rng = Random(_seed(name))
    length = CONFIG.value_size if size is None else size
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ["".join(rng.choices(alphabet, k=length)) for _ in range(count)]


def run_scenario(
    service: str,
    scenario: str,
    operation: Callable[[], None],
    *,
    iterations: int | None = None,
    warmup: int | None = None,
    operations_per_iteration: int = 1,
) -> ScenarioResult:
    measured = CONFIG.iterations if iterations is None else iterations
    warmup_count = CONFIG.warmup if warmup is None else warmup

    for _ in range(warmup_count):
        operation()

    result = ScenarioResult(
        service=service,
        scenario=scenario,
        iterations=measured,
        operations_per_iteration=operations_per_iteration,
    )
    for _ in range(measured):
        started_at = time.perf_counter()
        try:
            operation()
        except Exception:
            result.errors += 1
            raise
        finally:
            result.latencies_ms.append((time.perf_counter() - started_at) * 1000)
    return result


def scenario_get(client: redis.Redis, service: str) -> ScenarioResult:
    keys = [f"get:{index}" for index in range(CONFIG.key_count)]
    values = _values("get-values", CONFIG.key_count)
    for key, value in zip(keys, values):
        client.set(key, value)
    workload = [keys[index % len(keys)] for index in range(CONFIG.iterations + CONFIG.warmup)]
    index = 0

    def operation() -> None:
        nonlocal index
        key = workload[index]
        index += 1
        value = client.get(key)
        if value is None:
            raise AssertionError(f"GET lost preloaded key {key}")

    return run_scenario(service, "string_get", operation)


def scenario_set(client: redis.Redis, service: str) -> ScenarioResult:
    keys = [f"set:{index % CONFIG.key_count}" for index in range(CONFIG.iterations + CONFIG.warmup)]
    values = _values("set-values", len(keys))
    index = 0

    def operation() -> None:
        nonlocal index
        if client.set(keys[index], values[index]) is not True:
            raise AssertionError("SET did not return OK")
        index += 1

    return run_scenario(service, "string_set", operation)


def scenario_hash_getall(client: redis.Redis, service: str) -> ScenarioResult:
    keys = [f"session:{index}" for index in range(CONFIG.key_count)]
    mapping = {"user_id": "42", "role": "member", "state": "active"}
    for key in keys:
        client.hset(key, mapping=mapping)
    workload = [keys[index % len(keys)] for index in range(CONFIG.iterations + CONFIG.warmup)]
    index = 0

    def operation() -> None:
        nonlocal index
        response = client.hgetall(workload[index])
        index += 1
        if response != mapping:
            raise AssertionError(f"HGETALL returned {response!r}")

    return run_scenario(service, "hash_hgetall", operation)


def scenario_increment(client: redis.Redis, service: str) -> ScenarioResult:
    keys = [f"counter:{index}" for index in range(CONFIG.key_count)]
    workload = [keys[index % len(keys)] for index in range(CONFIG.iterations + CONFIG.warmup)]
    index = 0

    def operation() -> None:
        nonlocal index
        response = client.incr(workload[index])
        index += 1
        if not isinstance(response, int) or response < 1:
            raise AssertionError(f"INCR returned {response!r}")

    return run_scenario(service, "string_incr", operation)


def scenario_list_push(client: redis.Redis, service: str) -> ScenarioResult:
    values = _values("list-push", CONFIG.iterations + CONFIG.warmup)
    index = 0

    def operation() -> None:
        nonlocal index
        response = client.lpush("queue:push", values[index])
        index += 1
        if not isinstance(response, int) or response < 1:
            raise AssertionError(f"LPUSH returned {response!r}")

    return run_scenario(service, "list_lpush", operation)


def scenario_list_pop(client: redis.Redis, service: str) -> ScenarioResult:
    values = _values("list-pop", CONFIG.iterations + CONFIG.warmup)
    client.rpush("queue:pop", *values)

    def operation() -> None:
        if client.lpop("queue:pop") is None:
            raise AssertionError("LPOP returned nil before the workload ended")

    return run_scenario(service, "list_lpop", operation)


def scenario_sorted_set(client: redis.Redis, service: str) -> ScenarioResult:
    players = [f"player:{index}" for index in range(CONFIG.leaderboard_players)]
    for index, player in enumerate(players):
        client.zadd("leaderboard", {player: float(index)})
    workload = [
        (players[index % len(players)], float(index + len(players)))
        for index in range(CONFIG.iterations + CONFIG.warmup)
    ]
    index = 0

    def operation() -> None:
        nonlocal index
        player, score = workload[index]
        index += 1
        client.zadd("leaderboard", {player: score})
        top = client.zrevrange("leaderboard", 0, 9, withscores=True)
        if not top or len(top) > 10:
            raise AssertionError(f"ZREVRANGE returned {top!r}")

    return run_scenario(service, "zset_update_and_top10", operation)


def scenario_pipeline(
    client: redis.Redis,
    service: str,
    *,
    pipelined: bool,
) -> ScenarioResult:
    batch = CONFIG.pipeline_batch
    batches = max(CONFIG.iterations // batch, 1)
    warmup_batches = CONFIG.warmup // batch
    values = _values(
        f"pipeline-{pipelined}",
        (batches + warmup_batches) * batch,
    )
    offset = 0

    def operation() -> None:
        nonlocal offset
        current = values[offset : offset + batch]
        offset += batch
        if pipelined:
            pipe = client.pipeline(transaction=False)
            for index, value in enumerate(current):
                pipe.set(f"pipeline:{index}", value)
            responses = pipe.execute()
        else:
            responses = [
                client.set(f"individual:{index}", value)
                for index, value in enumerate(current)
            ]
        if responses != [True] * batch:
            raise AssertionError("pipeline workload returned a failed SET")

    label = "pipeline_batched" if pipelined else "pipeline_individual"
    return run_scenario(
        service,
        label,
        operation,
        iterations=batches,
        warmup=warmup_batches,
        operations_per_iteration=batch,
    )


SCENARIOS: list[Callable[[redis.Redis, str], ScenarioResult]] = [
    scenario_get,
    scenario_set,
    scenario_hash_getall,
    scenario_increment,
    scenario_list_push,
    scenario_list_pop,
    scenario_sorted_set,
]


def connect(host: str, port: int) -> redis.Redis:
    return redis.Redis(
        host=host,
        port=port,
        decode_responses=True,
        socket_timeout=CONFIG.socket_timeout_seconds,
    )


def wait_until_ready(name: str, client: redis.Redis) -> None:
    last_error: Exception | None = None
    for _ in range(30):
        try:
            if client.ping():
                return
        except Exception as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not become ready: {last_error}")


def write_report(results: list[ScenarioResult], redis_version: str) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    records = [result.to_record() for result in results]
    expected_per_round = len(SCENARIOS) * 2 + 4
    expected = expected_per_round * CONFIG.rounds
    if len(records) != expected:
        raise AssertionError(f"expected {expected} result rows, got {len(records)}")
    if any(record["errors"] for record in records):
        raise AssertionError("benchmark completed with operation errors")

    report = {
        "metadata": {
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "reference_image": REFERENCE_IMAGE,
            "reference_redis_version": redis_version,
            "python_version": platform.python_version(),
            "redis_py_version": version("redis"),
            "platform": platform.platform(),
            "config": asdict(CONFIG),
            "scope": "single client, sequential commands, persistence disabled",
        },
        "results": records,
    }
    (RESULTS_DIR / "performance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (RESULTS_DIR / "performance.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    targets = [
        (
            "redis-reference",
            connect(
                os.getenv("REDIS_REFERENCE_HOST", "redis-reference"),
                int(os.getenv("REDIS_REFERENCE_PORT", "6379")),
            ),
        ),
        (
            "mini-redis",
            connect(
                os.getenv("MINI_REDIS_HOST", "mini-redis"),
                int(os.getenv("MINI_REDIS_PORT", "6379")),
            ),
        ),
    ]
    for name, client in targets:
        wait_until_ready(name, client)

    results: list[ScenarioResult] = []
    for round_number in range(1, CONFIG.rounds + 1):
        print(f"round {round_number}/{CONFIG.rounds}")
        for scenario in SCENARIOS:
            for service, client in targets:
                client.flushall()
                result = scenario(client, service)
                result.round = round_number
                results.append(result)
                print(
                    f"{service:15} {result.scenario:28} "
                    f"p95={result.percentile(0.95):8.3f}ms"
                )

        for service, client in targets:
            client.flushall()
            result = scenario_pipeline(client, service, pipelined=False)
            result.round = round_number
            results.append(result)
            client.flushall()
            result = scenario_pipeline(client, service, pipelined=True)
            result.round = round_number
            results.append(result)

    redis_version = targets[0][1].info("server")["redis_version"]
    write_report(results, redis_version)
    for _, client in targets:
        client.close()


if __name__ == "__main__":
    main()
