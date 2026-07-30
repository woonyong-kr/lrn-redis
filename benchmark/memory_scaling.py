"""Measure the cost of overwriting one key as the keyspace grows."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-root", type=Path, default=Path.cwd())
    parser.add_argument("--key-counts", default="100,1000,5000")
    parser.add_argument("--operations", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    module_root = args.module_root.resolve()
    sys.path.insert(0, str(module_root))

    from store.datastore import DataStore
    from store.redis_object import make_string

    results = []
    for key_count in (int(value) for value in args.key_counts.split(",")):
        samples = []
        for _ in range(args.rounds):
            store = DataStore()
            for index in range(key_count):
                key = f"key:{index}"
                store._data[key] = make_string("x" * 32)
                store._last_access[key] = 0.0
            store.recompute_memory_usage()

            for index in range(5):
                store.set("key:0", make_string("w" * (32 + index % 2)))

            started_at = time.perf_counter()
            for index in range(args.operations):
                store.set("key:0", make_string("v" * (32 + index % 2)))
            samples.append((time.perf_counter() - started_at) * 1000)

        results.append(
            {
                "key_count": key_count,
                "operations": args.operations,
                "rounds": args.rounds,
                "median_ms": round(statistics.median(samples), 4),
            }
        )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
