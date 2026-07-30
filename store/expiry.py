"""TTL bookkeeping and active expiry sampling.

The server uses lazy expiry on reads plus a lightweight sampled background pass
so TTL-heavy workloads do not require full keyspace scans on every interval.
"""

import asyncio
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from store.datastore import DataStore


class ExpiryManager:
    """Tracks absolute expiry timestamps for keys in the datastore."""

    def __init__(
        self,
        store: "DataStore",
        interval_seconds: float = 0.1,
        sample_size: int = 20,
        max_passes: int = 4,
    ):
        self._store = store
        self._expiry: dict[str, float] = {}
        self._interval_seconds = interval_seconds
        self._sample_size = sample_size
        self._max_passes = max_passes
        self._rng = random.Random(0)
        self._store.bind_expiry_manager(self)
        self._store.register_delete_hook(self.on_key_deleted)

    def set_expiry(self, key: str, seconds: float) -> None:
        self._expiry[key] = time.time() + seconds
        self._store._sync_memory_for_key(key)

    def set_expiry_at(self, key: str, expiry_at: float) -> None:
        self._expiry[key] = expiry_at
        self._store._sync_memory_for_key(key)

    def get_ttl(self, key: str) -> float:
        if not self._store.exists(key):
            return -2
        if key not in self._expiry:
            return -1

        remaining = self._expiry[key] - time.time()
        if remaining <= 0:
            self._store.delete(key, reason="expiry")
            return -2
        return remaining

    def is_expired(self, key: str) -> bool:
        if key not in self._expiry:
            return False
        return time.time() >= self._expiry[key]

    def get_expiry_at(self, key: str):
        return self._expiry.get(key)

    def iter_expiring_keys(self):
        return list(self._expiry.keys())

    def remove_expiry(self, key: str) -> None:
        self._expiry.pop(key, None)
        self._store._sync_memory_for_key(key)

    def on_key_deleted(self, key: str) -> None:
        self._expiry.pop(key, None)

    def evict_expired_samples(self) -> int:
        now = time.time()
        removed = 0

        if not self._expiry:
            return 0

        for _ in range(self._max_passes):
            keys = list(self._expiry.keys())
            if not keys:
                break

            if len(keys) <= self._sample_size:
                sample = keys
            else:
                sample = self._rng.sample(keys, self._sample_size)

            expired = [key for key in sample if now >= self._expiry.get(key, float("inf"))]
            if not expired:
                break

            for key in expired:
                removed += self._store.delete(key, reason="expiry")

            if len(expired) * 4 < len(sample):
                break

        return removed

    async def active_expiry_loop(self) -> None:
        while True:
            self.evict_expired_samples()
            await asyncio.sleep(self._interval_seconds)
