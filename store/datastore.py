"""In-memory keyspace used by the server.

The store owns logical values, last-access timestamps, memory accounting, and
eviction. Command handlers stay thin and delegate actual mutations here.
"""

from __future__ import annotations

import copy
import fnmatch
import random
import time
from typing import Callable, List, Optional

from store.errors import MemoryLimitError
from store.hash_table import Hash
from store.memory import deep_getsizeof
from store.redis_object import (
    RedisObject,
    TYPE_HASH,
    TYPE_LIST,
    TYPE_NONE,
    TYPE_SET,
    TYPE_ZSET,
    make_list,
    make_set,
    make_zset,
)
from store.skiplist import ZSet


_MISSING = object()
SUPPORTED_EVICTION_POLICIES = {
    "noeviction",
    "allkeys-random",
    "allkeys-lru",
    "volatile-ttl",
}


class DataStore:
    """Holds the live keyspace and the metadata needed around it."""

    def __init__(self, maxmemory_bytes: int = 0, eviction_policy: str = "noeviction"):
        if eviction_policy not in SUPPORTED_EVICTION_POLICIES:
            raise ValueError("unsupported eviction policy: %s" % eviction_policy)
        self._data: dict[str, RedisObject] = {}
        self._delete_hooks: list[Callable[[str], None]] = []
        self._expiry_manager = None
        self._persistence_manager = None
        self.maxmemory_bytes = maxmemory_bytes
        self.eviction_policy = eviction_policy
        self._used_memory = 0
        self._key_sizes: dict[str, int] = {}
        self._last_access: dict[str, float] = {}
        self._rng = random.Random(0)

    # ─────────────────────────────────────────
    # 범용 메서드
    # ─────────────────────────────────────────

    def register_delete_hook(self, hook: Callable[[str], None]) -> None:
        """키 삭제 시 호출할 훅을 등록합니다."""
        self._delete_hooks.append(hook)

    def bind_expiry_manager(self, expiry_manager) -> None:
        """만료 확인용 ExpiryManager를 연결합니다."""
        self._expiry_manager = expiry_manager

    def bind_persistence_manager(self, persistence_manager) -> None:
        """AOF/RDB 관리자를 연결합니다."""
        self._persistence_manager = persistence_manager

    @property
    def used_memory(self) -> int:
        return self._used_memory

    def iter_items(self):
        for key in list(self._data.keys()):
            self._purge_if_expired(key)
        return list(self._data.items())

    def restore(self, key: str, obj: RedisObject) -> None:
        """영속화 로드 시 메모리 한도 검사 없이 키를 복원합니다."""
        self._data[key] = obj
        self._last_access[key] = time.monotonic()
        self._sync_memory_for_key(key)

    def recompute_memory_usage(self) -> None:
        self._key_sizes = {
            key: self._estimate_key_size(key, obj)
            for key, obj in self._data.items()
        }
        self._used_memory = sum(self._key_sizes.values())

    def enforce_memory_limit(self) -> None:
        self._enforce_maxmemory(None)

    def _snapshot_key(self, key: str, *, copy_value: Optional[bool] = None):
        obj = self._data.get(key, _MISSING)
        if copy_value is None:
            copy_value = self.maxmemory_bytes > 0
        backup = (
            copy.deepcopy(obj)
            if copy_value and obj is not _MISSING
            else obj
        )
        return backup, self._last_access.get(key)

    def _restore_key_snapshot(self, key: str, snapshot) -> None:
        current_size = self._key_sizes.pop(key, 0)
        self._used_memory = max(0, self._used_memory - current_size)
        obj, access_at = snapshot
        if obj is _MISSING:
            self._data.pop(key, None)
            self._last_access.pop(key, None)
        else:
            self._data[key] = obj
            if access_at is None:
                self._last_access.pop(key, None)
            else:
                self._last_access[key] = access_at
            self._sync_memory_for_key(key)

    def _touch_key(self, key: str) -> None:
        if key in self._data:
            self._last_access[key] = time.monotonic()

    def _sync_memory_for_key(self, key: str) -> None:
        previous_size = self._key_sizes.get(key, 0)
        if key not in self._data:
            self._key_sizes.pop(key, None)
            self._used_memory = max(0, self._used_memory - previous_size)
            return

        current_size = self._estimate_key_size(key, self._data[key])
        self._key_sizes[key] = current_size
        self._used_memory += current_size - previous_size

    def _finalize_mutation(self, key: str, snapshot) -> None:
        if key in self._data:
            self._touch_key(key)
            self._sync_memory_for_key(key)

        try:
            self._enforce_maxmemory(key)
        except MemoryLimitError:
            self._restore_key_snapshot(key, snapshot)
            raise

    def _estimate_key_size(self, key: str, obj: RedisObject) -> int:
        expiry_at = None if self._expiry_manager is None else self._expiry_manager.get_expiry_at(key)
        return deep_getsizeof((key, obj, self._last_access.get(key), expiry_at))

    def _cleanup_expired_before_eviction(self) -> None:
        if self._expiry_manager is None:
            return
        self._expiry_manager.evict_expired_samples()

    def _select_eviction_candidate(self) -> Optional[str]:
        if not self._data:
            return None

        if self.eviction_policy == "allkeys-random":
            return self._rng.choice(list(self._data.keys()))

        if self.eviction_policy == "allkeys-lru":
            return min(
                self._data.keys(),
                key=lambda key: self._last_access.get(key, 0.0),
            )

        if self.eviction_policy == "volatile-ttl":
            if self._expiry_manager is None:
                return None

            candidates = [
                key
                for key in self._expiry_manager.iter_expiring_keys()
                if key in self._data
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda key: self._expiry_manager.get_expiry_at(key) or float("inf"))

        return None

    def _enforce_maxmemory(self, current_key: Optional[str]) -> None:
        if self.maxmemory_bytes <= 0:
            return

        if current_key is not None and current_key in self._key_sizes:
            if self._key_sizes[current_key] > self.maxmemory_bytes:
                raise MemoryLimitError("OOM command not allowed when used memory > 'maxmemory'")

        self._cleanup_expired_before_eviction()

        if self._used_memory <= self.maxmemory_bytes:
            return

        if self.eviction_policy == "noeviction":
            raise MemoryLimitError("OOM command not allowed when used memory > 'maxmemory'")

        while self._used_memory > self.maxmemory_bytes:
            candidate = self._select_eviction_candidate()
            if candidate is None:
                raise MemoryLimitError("OOM command not allowed when used memory > 'maxmemory'")
            self.delete(candidate, reason="eviction")

    def _record_auto_delete(self, key: str, reason: str) -> None:
        if self._persistence_manager is None:
            return
        if reason in ("expiry", "eviction"):
            self._persistence_manager.record_delete(key)

    def _purge_if_expired(self, key: str) -> None:
        if self._expiry_manager is None:
            return
        if self._expiry_manager.is_expired(key):
            self.delete(key, reason="expiry")

    def get(self, key: str) -> Optional[RedisObject]:
        """
        키에 저장된 RedisObject를 반환합니다.
        키가 없으면 None을 반환합니다.
        """
        self._purge_if_expired(key)
        obj = self._data.get(key)
        if obj is not None:
            self._touch_key(key)
        return obj

    def set(self, key: str, obj: RedisObject) -> None:
        """
        키에 RedisObject를 저장합니다.
        기존 값이 있으면 덮어씁니다.
        """
        snapshot = self._snapshot_key(key, copy_value=False)
        self._data[key] = obj
        self._finalize_mutation(key, snapshot)

    def delete(self, key: str, reason: str = "command") -> int:
        """
        키를 삭제합니다.
        반환: 삭제된 키의 수 (1 또는 0)
        """
        if key in self._data:
            removed_size = self._key_sizes.pop(key, 0)
            del self._data[key]
            self._last_access.pop(key, None)
            self._used_memory = max(0, self._used_memory - removed_size)
            for hook in self._delete_hooks:
                hook(key)
            self._record_auto_delete(key, reason)
            return 1
        return 0

    def delete_many(self, keys: List[str]) -> int:
        """
        여러 키를 삭제합니다.
        반환: 실제로 삭제된 키의 수
        """
        count = 0
        for key in keys:
            count += self.delete(key)
        return count

    def exists(self, key: str) -> bool:
        """
        키의 존재 여부를 반환합니다.
        """
        self._purge_if_expired(key)
        exists = key in self._data
        if exists:
            self._touch_key(key)
        return exists

    def get_type(self, key: str) -> str:
        """
        키에 저장된 값의 Redis 타입을 반환합니다.
        반환값: "string" | "hash" | "list" | "set" | "zset" | "none"
        """
        self._purge_if_expired(key)
        obj = self._data.get(key)
        if obj is None:
            return TYPE_NONE
        self._touch_key(key)
        return obj.type

    def keys(self, pattern: str = "*") -> List[str]:
        """
        패턴에 매칭되는 키 목록을 반환합니다.
        pattern="*" 이면 전체 키를 반환합니다.
        """
        for key in list(self._data.keys()):
            self._purge_if_expired(key)

        if pattern == "*":
            return list(self._data.keys())
        return [key for key in self._data if fnmatch.fnmatch(key, pattern)]

    def flush(self, reason: str = "command") -> None:
        """
        모든 데이터를 삭제합니다. (FLUSHALL)
        """
        keys = list(self._data.keys())
        self._data.clear()
        self._key_sizes.clear()
        self._last_access.clear()
        self._used_memory = 0
        for key in keys:
            for hook in self._delete_hooks:
                hook(key)
            self._record_auto_delete(key, reason)

    # ─────────────────────────────────────────
    # Hash 전용 메서드
    # ─────────────────────────────────────────

    def _get_hash_table(self, key: str) -> Optional[Hash]:
        obj = self.get(key)
        if obj is None:
            return None
        if obj.type != TYPE_HASH:
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")

        if isinstance(obj.value, Hash):
            return obj.value

        migrated = Hash()
        if isinstance(obj.value, dict):
            for field, value in obj.value.items():
                migrated.set(field, value)
        obj.value = migrated
        obj.encoding = "hashtable"
        return migrated

    def _get_list_object(self, key: str, create: bool = False) -> Optional[RedisObject]:
        obj = self.get(key)
        if obj is None:
            if not create:
                return None
            obj = make_list()
            self._data[key] = obj
            return obj

        if obj.type != TYPE_LIST:
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return obj

    def _get_set_object(self, key: str, create: bool = False) -> Optional[RedisObject]:
        obj = self.get(key)
        if obj is None:
            if not create:
                return None
            obj = make_set()
            self._data[key] = obj
            return obj

        if obj.type != TYPE_SET:
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return obj

    def _get_zset_object(self, key: str, create: bool = False) -> Optional[RedisObject]:
        obj = self.get(key)
        if obj is None:
            if not create:
                return None
            obj = make_zset()
            self._data[key] = obj
            return obj

        if obj.type != TYPE_ZSET:
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")

        if not isinstance(obj.value, ZSet):
            if isinstance(obj.value, dict):
                obj.value = ZSet.from_items(obj.value.items())
            else:
                obj.value = ZSet.from_items(obj.value.items())
        return obj

    @staticmethod
    def _normalize_range(length: int, start: int, stop: int) -> Optional[tuple[int, int]]:
        if length == 0:
            return None

        if start < 0:
            start += length
        if stop < 0:
            stop += length

        if start < 0:
            start = 0
        if stop < 0:
            return None
        if start >= length:
            return None
        if stop >= length:
            stop = length - 1
        if start > stop:
            return None

        return start, stop

    def hget(self, key: str, field: str) -> Optional[str]:
        """Hash에서 특정 필드의 값을 반환합니다."""
        hash_value = self._get_hash_table(key)
        if hash_value is None:
            return None
        return hash_value.get(field)

    def hset(self, key: str, field: str, value: str) -> int:
        """
        Hash에 필드를 설정합니다.
        반환: 새로 추가된 필드면 1, 업데이트면 0
        """
        snapshot = self._snapshot_key(key)
        hash_value = self._get_hash_table(key)
        if hash_value is None:
            hash_value = Hash()
            self._data[key] = RedisObject(TYPE_HASH, "hashtable", hash_value)
        added = 1 if hash_value.set(field, value) else 0
        self._finalize_mutation(key, snapshot)
        return added

    def hdel(self, key: str, *fields: str) -> int:
        """Hash에서 필드를 삭제합니다. 반환: 삭제된 수"""
        snapshot = self._snapshot_key(key)
        hash_value = self._get_hash_table(key)
        if hash_value is None:
            return 0

        deleted = 0
        for field in fields:
            if hash_value.delete(field):
                deleted += 1

        if len(hash_value) == 0:
            self.delete(key)
        else:
            self._finalize_mutation(key, snapshot)
        return deleted

    def hgetall(self, key: str) -> dict:
        """Hash의 모든 필드와 값을 반환합니다."""
        hash_value = self._get_hash_table(key)
        if hash_value is None:
            return {}
        return {field: value for field, value in hash_value.items()}

    def hexists(self, key: str, field: str) -> bool:
        """Hash에 필드가 존재하는지 확인합니다."""
        hash_value = self._get_hash_table(key)
        if hash_value is None:
            return False
        return hash_value.contains(field)

    # ─────────────────────────────────────────
    # List 전용 메서드
    # ─────────────────────────────────────────

    def lpush(self, key: str, *values: str) -> int:
        snapshot = self._snapshot_key(key)
        list_obj = self._get_list_object(key, create=True)
        for value in values:
            list_obj.value.appendleft(value)
        self._finalize_mutation(key, snapshot)
        return len(list_obj.value)

    def rpush(self, key: str, *values: str) -> int:
        snapshot = self._snapshot_key(key)
        list_obj = self._get_list_object(key, create=True)
        for value in values:
            list_obj.value.append(value)
        self._finalize_mutation(key, snapshot)
        return len(list_obj.value)

    def lpop(self, key: str) -> Optional[str]:
        snapshot = self._snapshot_key(key)
        list_obj = self._get_list_object(key)
        if list_obj is None or not list_obj.value:
            return None

        value = list_obj.value.popleft()
        if not list_obj.value:
            self.delete(key)
        else:
            self._finalize_mutation(key, snapshot)
        return value

    def rpop(self, key: str) -> Optional[str]:
        snapshot = self._snapshot_key(key)
        list_obj = self._get_list_object(key)
        if list_obj is None or not list_obj.value:
            return None

        value = list_obj.value.pop()
        if not list_obj.value:
            self.delete(key)
        else:
            self._finalize_mutation(key, snapshot)
        return value

    def lrange(self, key: str, start: int, stop: int) -> List[str]:
        list_obj = self._get_list_object(key)
        if list_obj is None:
            return []

        values = list(list_obj.value)
        normalized = self._normalize_range(len(values), start, stop)
        if normalized is None:
            return []

        range_start, range_stop = normalized
        return values[range_start:range_stop + 1]

    def llen(self, key: str) -> int:
        list_obj = self._get_list_object(key)
        if list_obj is None:
            return 0
        return len(list_obj.value)

    def lindex(self, key: str, index: int) -> Optional[str]:
        list_obj = self._get_list_object(key)
        if list_obj is None:
            return None

        length = len(list_obj.value)
        if index < 0:
            index += length
        if index < 0 or index >= length:
            return None
        return list_obj.value[index]

    def lset(self, key: str, index: int, value: str) -> None:
        snapshot = self._snapshot_key(key)
        list_obj = self._get_list_object(key)
        if list_obj is None:
            raise KeyError("ERR no such key")

        length = len(list_obj.value)
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("ERR index out of range")

        list_obj.value[index] = value
        self._finalize_mutation(key, snapshot)

    # ─────────────────────────────────────────
    # Set 전용 메서드
    # ─────────────────────────────────────────

    def sadd(self, key: str, *members: str) -> int:
        snapshot = self._snapshot_key(key)
        set_obj = self._get_set_object(key, create=True)
        added = 0
        for member in members:
            if member not in set_obj.value:
                set_obj.value.add(member)
                added += 1
        self._finalize_mutation(key, snapshot)
        return added

    def srem(self, key: str, *members: str) -> int:
        snapshot = self._snapshot_key(key)
        set_obj = self._get_set_object(key)
        if set_obj is None:
            return 0

        removed = 0
        for member in members:
            if member in set_obj.value:
                set_obj.value.remove(member)
                removed += 1

        if not set_obj.value:
            self.delete(key)
        else:
            self._finalize_mutation(key, snapshot)
        return removed

    def smembers(self, key: str) -> set:
        set_obj = self._get_set_object(key)
        if set_obj is None:
            return set()
        return set(set_obj.value)

    def sismember(self, key: str, member: str) -> bool:
        set_obj = self._get_set_object(key)
        if set_obj is None:
            return False
        return member in set_obj.value

    def scard(self, key: str) -> int:
        set_obj = self._get_set_object(key)
        if set_obj is None:
            return 0
        return len(set_obj.value)

    def sinter(self, *keys: str) -> set:
        if not keys:
            return set()

        members = self.smembers(keys[0])
        for key in keys[1:]:
            members &= self.smembers(key)
        return members

    def sunion(self, *keys: str) -> set:
        members: set = set()
        for key in keys:
            members |= self.smembers(key)
        return members

    def sdiff(self, *keys: str) -> set:
        if not keys:
            return set()

        members = self.smembers(keys[0])
        for key in keys[1:]:
            members -= self.smembers(key)
        return members

    # ─────────────────────────────────────────
    # Sorted Set 전용 메서드
    # ─────────────────────────────────────────

    def zadd(self, key: str, score: float, member: str) -> int:
        snapshot = self._snapshot_key(key)
        zset_obj = self._get_zset_object(key, create=True)
        added = zset_obj.value.set(member, score)
        self._finalize_mutation(key, snapshot)
        return added

    def zrem(self, key: str, member: str) -> int:
        snapshot = self._snapshot_key(key)
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return 0

        removed = zset_obj.value.remove(member)
        if removed == 0:
            return 0

        if len(zset_obj.value) == 0:
            self.delete(key)
        else:
            self._finalize_mutation(key, snapshot)
        return removed

    def zscore(self, key: str, member: str) -> Optional[float]:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return None
        return zset_obj.value.get_score(member)

    def zrange(self, key: str, start: int, stop: int) -> List[str]:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return []

        return [member for member, _ in zset_obj.value.range_entries(start, stop)]

    def zrank(self, key: str, member: str) -> Optional[int]:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return None
        return zset_obj.value.rank(member)

    def zcard(self, key: str) -> int:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return 0
        return len(zset_obj.value)

    def zrange_withscores(
        self,
        key: str,
        start: int,
        stop: int,
        *,
        reverse: bool = False,
    ) -> List[tuple[str, float]]:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return []

        if reverse:
            return zset_obj.value.revrange_entries(start, stop)
        return zset_obj.value.range_entries(start, stop)

    def zrangebyscore(self, key: str, minimum: float, maximum: float) -> List[str]:
        zset_obj = self._get_zset_object(key)
        if zset_obj is None:
            return []

        return [
            member
            for member, _ in zset_obj.value.range_by_score(minimum, maximum)
        ]
