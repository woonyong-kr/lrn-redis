"""Persistence layer for the mini-redis core profile.

The AOF path writes RESP commands so replay uses the same dispatcher logic as
live traffic. The RDB path is a compact custom snapshot that stores the same
command stream behind a small binary header.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable

from protocol.encoder import RespError, encode_array
from protocol.parser import parse
from store.hash_table import Hash
from store.redis_object import TYPE_HASH, TYPE_LIST, TYPE_SET, TYPE_STRING, TYPE_ZSET


RESP_ENCODING = "utf-8"
RESP_ERRORS = "surrogateescape"
RDB_MAGIC = b"MINIRDB1\r\n"
READ_CHUNK_SIZE = 65536


def _to_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode(RESP_ENCODING, errors=RESP_ERRORS)
    return value


def _format_score(score: float) -> str:
    return format(score, "g")


def _format_pexpireat(expiry_at: float) -> str:
    return str(int(expiry_at * 1000))


class PersistenceManager:
    def __init__(
        self,
        store,
        expiry,
        *,
        aof_enabled: bool = False,
        aof_path: str = "data/appendonly.aof",
        aof_fsync: str = "everysec",
        rdb_enabled: bool = False,
        rdb_path: str = "data/dump.rdb",
        rdb_save_interval_seconds: float = 0.0,
    ):
        if aof_fsync not in ("always", "everysec", "no"):
            raise ValueError("aof_fsync must be always, everysec, or no")
        self.store = store
        self.expiry = expiry
        self.aof_enabled = aof_enabled
        self.aof_path = aof_path
        self.aof_fsync = aof_fsync
        self.rdb_enabled = rdb_enabled
        self.rdb_path = rdb_path
        self.rdb_save_interval_seconds = rdb_save_interval_seconds
        self._disabled = False
        self._last_fsync_at = 0.0
        self._last_rdb_save_at = 0.0
        self._aof_handle = None
        self._saving_snapshot = False

        self.store.bind_persistence_manager(self)

        if self.aof_enabled:
            self._ensure_parent_dir(self.aof_path)
            self._aof_handle = open(self.aof_path, "ab+", buffering=0)
            self._aof_handle.seek(0, os.SEEK_END)

        try:
            self.load()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._aof_handle is not None:
            self._aof_handle.flush()
            if self.aof_fsync in ("always", "everysec"):
                os.fsync(self._aof_handle.fileno())
            self._aof_handle.close()
            self._aof_handle = None

    def suspend(self):
        manager = self

        class _Suspend:
            def __enter__(self_inner):
                manager._disabled = True

            def __exit__(self_inner, exc_type, exc, tb):
                manager._disabled = False

        return _Suspend()

    def load(self) -> None:
        if (
            self.aof_enabled
            and os.path.exists(self.aof_path)
            and os.path.getsize(self.aof_path) > 0
        ):
            self.replay_aof()
            return

        if (
            self.rdb_enabled
            and os.path.exists(self.rdb_path)
            and os.path.getsize(self.rdb_path) > 0
        ):
            self.load_rdb()

    def replay_aof(self) -> None:
        with self.suspend():
            self.store.flush(reason="load")
            with open(self.aof_path, "rb") as handle:
                self._replay_stream(handle)
            self.store.enforce_memory_limit()
        self._last_rdb_save_at = time.time()

    def load_rdb(self) -> None:
        with self.suspend():
            with open(self.rdb_path, "rb") as handle:
                header = handle.read(len(RDB_MAGIC))
                if header != RDB_MAGIC:
                    raise ValueError("invalid RDB header")
                self.store.flush(reason="load")
                self._replay_stream(handle)
            self.store.enforce_memory_limit()
        self._last_rdb_save_at = time.time()

    def save_rdb(self) -> None:
        if not self.rdb_enabled or self._saving_snapshot:
            return

        self._ensure_parent_dir(self.rdb_path)
        temp_path = self.rdb_path + ".tmp"
        self._saving_snapshot = True
        try:
            with open(temp_path, "wb") as handle:
                handle.write(RDB_MAGIC)
                for command in self._snapshot_commands():
                    handle.write(encode_array(command))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.rdb_path)
        finally:
            self._saving_snapshot = False
        self._last_rdb_save_at = time.time()

    def maybe_save_rdb(self) -> None:
        if not self.rdb_enabled or self.rdb_save_interval_seconds <= 0:
            return
        if time.time() - self._last_rdb_save_at < self.rdb_save_interval_seconds:
            return
        self.save_rdb()

    def maintenance(self) -> None:
        """Flush everysec even when clients stop writing; service snapshots too."""
        if (
            self._aof_handle is not None
            and self.aof_fsync == "everysec"
            and time.time() - self._last_fsync_at >= 1.0
        ):
            os.fsync(self._aof_handle.fileno())
            self._last_fsync_at = time.time()
        self.maybe_save_rdb()

    def record_delete(self, key: str) -> None:
        if self._disabled:
            return
        if self.aof_enabled:
            self._append_command(["DEL", key])
        self.maybe_save_rdb()

    def record_command(self, command: list[str], result: Any) -> None:
        if self._disabled:
            return
        if isinstance(result, RespError):
            return

        if self.aof_enabled:
            for canonical in self._canonicalize_command(command, result):
                self._append_command(canonical)

        self.maybe_save_rdb()

    def _append_command(self, command: list[str]) -> None:
        if self._aof_handle is None:
            return

        self._aof_handle.write(encode_array(command))
        self._aof_handle.flush()

        if self.aof_fsync == "always":
            os.fsync(self._aof_handle.fileno())
            self._last_fsync_at = time.time()
        elif self.aof_fsync == "everysec":
            now = time.time()
            if now - self._last_fsync_at >= 1.0:
                os.fsync(self._aof_handle.fileno())
                self._last_fsync_at = now

    def _replay_stream(self, handle) -> None:
        # Evictions are already part of the log. Replaying under the live memory
        # policy would run random eviction a second time on intermediate states.
        limit = self.store.maxmemory_bytes
        self.store.maxmemory_bytes = 0
        try:
            self._read_stream(handle)
        finally:
            self.store.maxmemory_bytes = limit

    def _read_stream(self, handle) -> None:
        from commands.dispatcher import dispatch

        buffer = b""
        while True:
            chunk = handle.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            buffer += chunk

            while buffer:
                command, consumed = parse(buffer)
                if command is None:
                    break
                buffer = buffer[consumed:]
                result = dispatch(command, self.store, self.expiry)
                if isinstance(result, RespError):
                    raise ValueError(f"failed to replay command {command!r}: {result}")

        if buffer.strip():
            raise ValueError("truncated persistence stream")

    def _snapshot_commands(self) -> Iterable[list[str]]:
        now = time.time()
        for key, obj in sorted(self.store.iter_items(), key=lambda item: item[0]):
            yield from self._commands_for_object(key, obj)

            expiry_at = self.expiry.get_expiry_at(key)
            if expiry_at is not None and expiry_at > now:
                yield ["PEXPIREAT", key, _format_pexpireat(expiry_at)]

    def _commands_for_object(self, key: str, obj) -> Iterable[list[str]]:
        if obj.type == TYPE_STRING:
            yield ["SET", key, _to_text(obj.value)]
            return

        if obj.type == TYPE_HASH:
            entries = (
                obj.value.items()
                if isinstance(obj.value, Hash)
                else list(obj.value.items())
            )
            if entries:
                command = ["HSET", key]
                for field, value in entries:
                    command.extend([field, value])
                yield command
            return

        if obj.type == TYPE_LIST:
            items = list(obj.value)
            if items:
                yield ["RPUSH", key, *items]
            return

        if obj.type == TYPE_SET:
            items = sorted(obj.value)
            if items:
                yield ["SADD", key, *items]
            return

        if obj.type == TYPE_ZSET:
            entries = obj.value.items()
            if entries:
                command = ["ZADD", key]
                for member, score in entries:
                    command.extend([_format_score(score), member])
                yield command
            return

        raise ValueError(f"unsupported type for snapshot: {obj.type}")

    def _canonicalize_command(self, command: list[str], result: Any) -> list[list[str]]:
        cmd_name = command[0].upper()
        args = command[1:]

        if cmd_name == "FLUSHALL":
            return [["FLUSHALL"]]

        if cmd_name == "SET":
            key = args[0]
            if not self.store.exists(key):
                return [["DEL", key]]
            value = args[1]
            commands = [["SET", key, value]]
            expiry_at = self.expiry.get_expiry_at(key)
            if expiry_at is not None:
                commands.append(["PEXPIREAT", key, _format_pexpireat(expiry_at)])
            return commands

        if cmd_name in ("EXPIRE", "PEXPIREAT"):
            key = args[0]
            if not self.store.exists(key):
                return []
            expiry_at = self.expiry.get_expiry_at(key)
            if expiry_at is None:
                return []
            return [["PEXPIREAT", key, _format_pexpireat(expiry_at)]]

        if cmd_name == "PERSIST":
            return [command] if result == 1 else []

        if cmd_name == "MSET":
            # A later pair can evict an earlier pair; persist final surviving
            # values rather than resurrecting every original argument on replay.
            commands = []
            for key in dict.fromkeys(args[::2]):
                obj = self.store.get(key)
                commands.append(
                    ["DEL", key] if obj is None else ["SET", key, _to_text(obj.value)]
                )
            return commands

        if cmd_name in {
            "INCR",
            "DECR",
            "INCRBY",
            "APPEND",
            "DEL",
            "HSET",
            "HMSET",
            "HDEL",
            "LPUSH",
            "RPUSH",
            "LPOP",
            "RPOP",
            "LSET",
            "SADD",
            "SREM",
            "ZADD",
            "ZREM",
        }:
            if cmd_name != "DEL" and not self.store.exists(args[0]):
                return [["DEL", args[0]]]
            return [[cmd_name, *args]]

        return []

    @staticmethod
    def _ensure_parent_dir(path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
