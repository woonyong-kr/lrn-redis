"""Representative differential checks for every advertised command.

This is not a claim of complete Redis compatibility. It verifies one
deterministic success-path case for each command exposed by the dispatcher and
fails if either service errors, a response differs, or the 53-command manifest
is incomplete.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis


RESULTS_DIR = Path("results")
SUPPORTED_COMMANDS = {
    "APPEND", "DECR", "DEL", "EXISTS", "EXPIRE", "FLUSHALL", "GET",
    "HDEL", "HEXISTS", "HGET", "HGETALL", "HKEYS", "HLEN", "HMGET",
    "HMSET", "HSET", "HVALS", "INCR", "INCRBY", "KEYS", "LINDEX",
    "LLEN", "LPOP", "LPUSH", "LRANGE", "LSET", "MGET", "MSET",
    "PERSIST", "PEXPIREAT", "PING", "RPOP", "RPUSH", "SADD", "SCARD",
    "SDIFF", "SET", "SINTER", "SISMEMBER", "SMEMBERS", "SREM",
    "STRLEN", "SUNION", "TTL", "TYPE", "ZADD", "ZCARD", "ZRANGE",
    "ZRANGEBYSCORE", "ZRANK", "ZREM", "ZREVRANGE", "ZSCORE",
}
SORTED_RESPONSE_COMMANDS = {
    "HKEYS",
    "HVALS",
    "KEYS",
    "SDIFF",
    "SINTER",
    "SMEMBERS",
    "SUNION",
}


@dataclass(frozen=True)
class CommandCase:
    name: str
    setup: tuple[tuple[Any, ...], ...]
    command: tuple[Any, ...]


def cases() -> list[CommandCase]:
    future_ms = int((time.time() + 60) * 1000)
    zset = (("ZADD", "z", 1, "one", 2, "two", 3, "three"),)
    hash_value = (("HSET", "h", "a", "1", "b", "2"),)
    list_value = (("RPUSH", "list", "a", "b", "c"),)
    set_value = (("SADD", "set", "a", "b", "c"),)
    return [
        CommandCase("APPEND", (("SET", "s", "a"),), ("APPEND", "s", "bc")),
        CommandCase("DECR", (("SET", "n", "2"),), ("DECR", "n")),
        CommandCase("DEL", (("SET", "k", "v"),), ("DEL", "k")),
        CommandCase("EXISTS", (("MSET", "a", "1", "b", "2"),), ("EXISTS", "a", "b", "c")),
        CommandCase("EXPIRE", (("SET", "k", "v"),), ("EXPIRE", "k", 60)),
        CommandCase("FLUSHALL", (("SET", "k", "v"),), ("FLUSHALL",)),
        CommandCase("GET", (("SET", "k", "v"),), ("GET", "k")),
        CommandCase("HDEL", hash_value, ("HDEL", "h", "a")),
        CommandCase("HEXISTS", hash_value, ("HEXISTS", "h", "a")),
        CommandCase("HGET", hash_value, ("HGET", "h", "a")),
        CommandCase("HGETALL", hash_value, ("HGETALL", "h")),
        CommandCase("HKEYS", hash_value, ("HKEYS", "h")),
        CommandCase("HLEN", hash_value, ("HLEN", "h")),
        CommandCase("HMGET", hash_value, ("HMGET", "h", "a", "missing")),
        CommandCase("HMSET", (), ("HMSET", "h", "a", "1", "b", "2")),
        CommandCase("HSET", (), ("HSET", "h", "a", "1", "b", "2")),
        CommandCase("HVALS", hash_value, ("HVALS", "h")),
        CommandCase("INCR", (("SET", "n", "1"),), ("INCR", "n")),
        CommandCase("INCRBY", (("SET", "n", "1"),), ("INCRBY", "n", 4)),
        CommandCase("KEYS", (("MSET", "a", "1", "b", "2"),), ("KEYS", "*")),
        CommandCase("LINDEX", list_value, ("LINDEX", "list", -1)),
        CommandCase("LLEN", list_value, ("LLEN", "list")),
        CommandCase("LPOP", list_value, ("LPOP", "list")),
        CommandCase("LPUSH", (), ("LPUSH", "list", "a", "b")),
        CommandCase("LRANGE", list_value, ("LRANGE", "list", 0, -1)),
        CommandCase("LSET", list_value, ("LSET", "list", 1, "updated")),
        CommandCase("MGET", (("MSET", "a", "1", "b", "2"),), ("MGET", "a", "missing", "b")),
        CommandCase("MSET", (), ("MSET", "a", "1", "b", "2")),
        CommandCase("PERSIST", (("SET", "k", "v", "EX", 60),), ("PERSIST", "k")),
        CommandCase("PEXPIREAT", (("SET", "k", "v"),), ("PEXPIREAT", "k", future_ms)),
        CommandCase("PING", (), ("PING",)),
        CommandCase("RPOP", list_value, ("RPOP", "list")),
        CommandCase("RPUSH", (), ("RPUSH", "list", "a", "b")),
        CommandCase("SADD", (), ("SADD", "set", "a", "b")),
        CommandCase("SCARD", set_value, ("SCARD", "set")),
        CommandCase("SDIFF", set_value + (("SADD", "other", "b"),), ("SDIFF", "set", "other")),
        CommandCase("SET", (), ("SET", "k", "v")),
        CommandCase("SINTER", set_value + (("SADD", "other", "b", "c"),), ("SINTER", "set", "other")),
        CommandCase("SISMEMBER", set_value, ("SISMEMBER", "set", "b")),
        CommandCase("SMEMBERS", set_value, ("SMEMBERS", "set")),
        CommandCase("SREM", set_value, ("SREM", "set", "b")),
        CommandCase("STRLEN", (("SET", "s", "abc"),), ("STRLEN", "s")),
        CommandCase("SUNION", set_value + (("SADD", "other", "d"),), ("SUNION", "set", "other")),
        CommandCase("TTL", (("SET", "k", "v", "EX", 60),), ("TTL", "k")),
        CommandCase("TYPE", (("SET", "k", "v"),), ("TYPE", "k")),
        CommandCase("ZADD", (), ("ZADD", "z", 1, "one", 2, "two")),
        CommandCase("ZCARD", zset, ("ZCARD", "z")),
        CommandCase("ZRANGE", zset, ("ZRANGE", "z", 0, -1, "WITHSCORES")),
        CommandCase("ZRANGEBYSCORE", zset, ("ZRANGEBYSCORE", "z", 1, 2)),
        CommandCase("ZRANK", zset, ("ZRANK", "z", "two")),
        CommandCase("ZREM", zset, ("ZREM", "z", "two")),
        CommandCase("ZREVRANGE", zset, ("ZREVRANGE", "z", 0, 1, "WITHSCORES")),
        CommandCase("ZSCORE", zset, ("ZSCORE", "z", "two")),
    ]


def canonicalize(command: str, value: Any) -> Any:
    if isinstance(value, dict):
        return sorted((canonicalize(command, key), canonicalize(command, item)) for key, item in value.items())
    if isinstance(value, set):
        return sorted(canonicalize(command, item) for item in value)
    if isinstance(value, tuple):
        return [canonicalize(command, item) for item in value]
    if isinstance(value, list):
        normalized = [canonicalize(command, item) for item in value]
        return sorted(normalized, key=repr) if command in SORTED_RESPONSE_COMMANDS else normalized
    if isinstance(value, float):
        return round(value, 9)
    return value


def connect(host: str) -> redis.Redis:
    return redis.Redis(
        host=host,
        port=6379,
        decode_responses=True,
        socket_timeout=5,
    )


def execute(client: redis.Redis, command: tuple[Any, ...]) -> Any:
    return client.execute_command(*command)


def main() -> None:
    reference = connect(os.getenv("REDIS_REFERENCE_HOST", "redis-reference"))
    candidate = connect(os.getenv("MINI_REDIS_HOST", "mini-redis"))
    command_cases = cases()

    names = {case.name for case in command_cases}
    if names != SUPPORTED_COMMANDS or len(command_cases) != len(SUPPORTED_COMMANDS):
        missing = sorted(SUPPORTED_COMMANDS - names)
        extra = sorted(names - SUPPORTED_COMMANDS)
        raise AssertionError(f"invalid command coverage: missing={missing}, extra={extra}")

    results = []
    failures = []
    for case in command_cases:
        for client in (reference, candidate):
            client.flushall()
            for setup_command in case.setup:
                execute(client, setup_command)

        try:
            expected = canonicalize(case.name, execute(reference, case.command))
            actual = canonicalize(case.name, execute(candidate, case.command))
            if case.name == "TTL":
                matched = (
                    isinstance(expected, int)
                    and isinstance(actual, int)
                    and expected >= 0
                    and actual >= 0
                    and abs(expected - actual) <= 1
                )
            else:
                matched = expected == actual
            error = None
        except Exception as exc:
            expected = None
            actual = None
            matched = False
            error = f"{type(exc).__name__}: {exc}"

        result = {
            "command": case.name,
            "matched": matched,
            "reference": expected,
            "mini_redis": actual,
            "error": error,
        }
        results.append(result)
        if not matched:
            failures.append(result)

    RESULTS_DIR.mkdir(exist_ok=True)
    report = {
        "metadata": {
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "one representative success-path differential case per advertised command",
            "not_a_claim_of": "complete Redis protocol or behavioral compatibility",
        },
        "summary": {
            "advertised_commands": len(SUPPORTED_COMMANDS),
            "matched_commands": len(SUPPORTED_COMMANDS) - len(failures),
            "failed_commands": len(failures),
        },
        "results": results,
    }
    (RESULTS_DIR / "compatibility.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"compatibility: {report['summary']['matched_commands']}/"
        f"{report['summary']['advertised_commands']} commands matched"
    )
    reference.close()
    candidate.close()
    if failures:
        raise SystemExit(
            "differential failures: "
            + ", ".join(result["command"] for result in failures)
        )


if __name__ == "__main__":
    main()
