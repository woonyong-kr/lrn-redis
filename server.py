"""TCP entrypoint for the mini-redis core profile.

Each connection follows the same pipeline:
read RESP bytes into a bounded buffer, dispatch complete commands against the
shared store, persist successful writes, then encode and flush replies.

The server also applies simple client guards so one connection cannot hold the
event loop for too long or keep unlimited request/response buffers in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

try:
    import uvloop
except ImportError:
    uvloop = None

from commands.dispatcher import dispatch
from protocol.encoder import RespError, encode
from protocol.parser import parse
from store.datastore import DataStore
from store.expiry import ExpiryManager
from store.persistence import PersistenceManager


if uvloop is not None:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class ClientLimitError(Exception):
    """Raised when a client exceeds the configured buffer or drain limits."""


def _env_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default

    value = int(raw)
    if value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_float(name: str, default: float, *, min_value: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default

    value = float(raw)
    if value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_memory(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default

    value = raw.strip().lower()
    suffixes = {
        "kb": 1024,
        "k": 1024,
        "mb": 1024 * 1024,
        "m": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
        "g": 1024 * 1024 * 1024,
    }
    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * multiplier)
    return int(value)


DEFAULT_HOST = os.getenv("MINI_REDIS_HOST", "127.0.0.1")
DEFAULT_PORT = _env_int("MINI_REDIS_PORT", 6379)
DEFAULT_READ_CHUNK = _env_int("MINI_REDIS_READ_CHUNK", 4096)
DEFAULT_EXPIRY_LOOP_INTERVAL = (
    _env_float("MINI_REDIS_EXPIRY_LOOP_INTERVAL_MS", 100.0, min_value=1.0) / 1000.0
)
DEFAULT_EXPIRY_SAMPLE_SIZE = _env_int("MINI_REDIS_EXPIRY_SAMPLE_SIZE", 20)
DEFAULT_EXPIRY_MAX_PASSES = _env_int("MINI_REDIS_EXPIRY_MAX_PASSES", 4)
DEFAULT_LOG_LEVEL = os.getenv("MINI_REDIS_LOG_LEVEL", "INFO").upper()
DEFAULT_MAXMEMORY = _env_memory("MINI_REDIS_MAXMEMORY", 0)
DEFAULT_MAXMEMORY_POLICY = os.getenv("MINI_REDIS_MAXMEMORY_POLICY", "noeviction")
DEFAULT_AOF_ENABLED = _env_bool("MINI_REDIS_APPENDONLY", False)
DEFAULT_AOF_FILE = os.getenv("MINI_REDIS_AOF_FILE", "data/appendonly.aof")
DEFAULT_AOF_FSYNC = os.getenv("MINI_REDIS_AOF_FSYNC", "everysec")
DEFAULT_RDB_ENABLED = _env_bool("MINI_REDIS_RDB_ENABLED", False)
DEFAULT_RDB_FILE = os.getenv("MINI_REDIS_RDB_FILE", "data/dump.rdb")
DEFAULT_RDB_SAVE_INTERVAL = _env_float(
    "MINI_REDIS_RDB_SAVE_INTERVAL_SECONDS", 0.0, min_value=0.0
)
DEFAULT_CLIENT_IDLE_TIMEOUT = _env_float(
    "MINI_REDIS_CLIENT_IDLE_TIMEOUT_SECONDS", 30.0, min_value=0.1
)
DEFAULT_WRITE_DRAIN_TIMEOUT = _env_float(
    "MINI_REDIS_WRITE_DRAIN_TIMEOUT_SECONDS", 5.0, min_value=0.1
)
DEFAULT_MAX_INPUT_BUFFER = _env_int(
    "MINI_REDIS_MAX_INPUT_BUFFER_BYTES", 1024 * 1024, min_value=1024
)
DEFAULT_MAX_OUTPUT_BUFFER = _env_int(
    "MINI_REDIS_MAX_OUTPUT_BUFFER_BYTES", 256 * 1024, min_value=1024
)
DEFAULT_MAX_COMMANDS_PER_TICK = _env_int(
    "MINI_REDIS_MAX_COMMANDS_PER_TICK", 128, min_value=1
)

logging.basicConfig(
    level=getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class Server:
    """Owns shared store state and serves RESP connections over TCP."""

    def __init__(
        self,
        *,
        read_chunk: int = DEFAULT_READ_CHUNK,
        expiry_interval_seconds: float = DEFAULT_EXPIRY_LOOP_INTERVAL,
        expiry_sample_size: int = DEFAULT_EXPIRY_SAMPLE_SIZE,
        expiry_max_passes: int = DEFAULT_EXPIRY_MAX_PASSES,
        maxmemory_bytes: int = DEFAULT_MAXMEMORY,
        eviction_policy: str = DEFAULT_MAXMEMORY_POLICY,
        aof_enabled: bool = DEFAULT_AOF_ENABLED,
        aof_path: str = DEFAULT_AOF_FILE,
        aof_fsync: str = DEFAULT_AOF_FSYNC,
        rdb_enabled: bool = DEFAULT_RDB_ENABLED,
        rdb_path: str = DEFAULT_RDB_FILE,
        rdb_save_interval_seconds: float = DEFAULT_RDB_SAVE_INTERVAL,
        client_idle_timeout_seconds: float = DEFAULT_CLIENT_IDLE_TIMEOUT,
        write_drain_timeout_seconds: float = DEFAULT_WRITE_DRAIN_TIMEOUT,
        max_input_buffer_bytes: int = DEFAULT_MAX_INPUT_BUFFER,
        max_output_buffer_bytes: int = DEFAULT_MAX_OUTPUT_BUFFER,
        max_commands_per_tick: int = DEFAULT_MAX_COMMANDS_PER_TICK,
    ):
        self.store = DataStore(
            maxmemory_bytes=maxmemory_bytes,
            eviction_policy=eviction_policy,
        )
        self.expiry = ExpiryManager(
            self.store,
            interval_seconds=expiry_interval_seconds,
            sample_size=expiry_sample_size,
            max_passes=expiry_max_passes,
        )
        self.persistence = PersistenceManager(
            self.store,
            self.expiry,
            aof_enabled=aof_enabled,
            aof_path=aof_path,
            aof_fsync=aof_fsync,
            rdb_enabled=rdb_enabled,
            rdb_path=rdb_path,
            rdb_save_interval_seconds=rdb_save_interval_seconds,
        )
        self.read_chunk = read_chunk
        self.client_idle_timeout_seconds = client_idle_timeout_seconds
        self.write_drain_timeout_seconds = write_drain_timeout_seconds
        self.max_input_buffer_bytes = max_input_buffer_bytes
        self.max_output_buffer_bytes = max_output_buffer_bytes
        self.max_commands_per_tick = max_commands_per_tick

    async def _read_chunk(self, reader: asyncio.StreamReader) -> bytes:
        try:
            return await asyncio.wait_for(
                reader.read(self.read_chunk),
                timeout=self.client_idle_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ClientLimitError("idle timeout") from exc

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(
                writer.drain(),
                timeout=self.write_drain_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ClientLimitError("write drain timeout") from exc

    def _configure_writer_limits(self, writer: asyncio.StreamWriter) -> None:
        transport = getattr(writer, "transport", None)
        if transport is None or not hasattr(transport, "set_write_buffer_limits"):
            return
        low = max(1, self.max_output_buffer_bytes // 2)
        transport.set_write_buffer_limits(high=self.max_output_buffer_bytes, low=low)

    def _write_buffer_size(self, writer: asyncio.StreamWriter) -> int:
        transport = getattr(writer, "transport", None)
        if transport is None or not hasattr(transport, "get_write_buffer_size"):
            return 0
        return transport.get_write_buffer_size()

    async def _flush_output_if_needed(self, writer: asyncio.StreamWriter) -> None:
        if self._write_buffer_size(writer) <= self.max_output_buffer_bytes:
            return
        await self._drain(writer)
        if self._write_buffer_size(writer) > self.max_output_buffer_bytes:
            raise ClientLimitError("output buffer limit exceeded")

    async def _send_protocol_error(
        self, writer: asyncio.StreamWriter, message: str
    ) -> None:
        writer.write(encode(RespError(message)))
        try:
            await self._drain(writer)
        except ClientLimitError:
            pass

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername")
        buffer = b""
        commands_in_tick = 0
        self._configure_writer_limits(writer)

        try:
            while True:
                chunk = await self._read_chunk(reader)
                if not chunk:
                    break

                buffer += chunk
                if len(buffer) > self.max_input_buffer_bytes:
                    await self._send_protocol_error(
                        writer, "ERR request buffer limit exceeded"
                    )
                    break

                while buffer:
                    command, consumed = parse(buffer)
                    if command is None:
                        break

                    buffer = buffer[consumed:]
                    result = dispatch(command, self.store, self.expiry)
                    self.persistence.record_command(command, result)
                    writer.write(encode(result))
                    commands_in_tick += 1

                    await self._flush_output_if_needed(writer)

                    if commands_in_tick >= self.max_commands_per_tick:
                        await self._drain(writer)
                        commands_in_tick = 0
                        await asyncio.sleep(0)

                if commands_in_tick:
                    await self._drain(writer)
                    commands_in_tick = 0

        except (ClientLimitError, ConnectionResetError, BrokenPipeError) as exc:
            logger.info("closing client %s: %s", addr, exc)
        except Exception as exc:
            logger.error("error handling client %s: %s", addr, exc)
            try:
                await self._send_protocol_error(writer, f"ERR server error: {exc}")
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def maintenance_loop(self, interval: float = 0.1) -> None:
        while True:
            self.persistence.maintenance()
            await asyncio.sleep(interval)

    async def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        server = await asyncio.start_server(self.handle_client, host, port)

        logger.info("mini-redis server started on %s:%s", host, port)

        workers = []
        try:
            async with server:
                workers = [
                    asyncio.create_task(coro)
                    for coro in (
                        self.expiry.active_expiry_loop(),
                        self.maintenance_loop(),
                        server.serve_forever(),
                    )
                ]
                await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            try:
                self.persistence.save_rdb()
            finally:
                self.persistence.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="mini-redis server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port")
    args = parser.parse_args()

    async def main():
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
            except NotImplementedError:
                pass
        await Server().start(host=args.host, port=args.port)

    try:
        asyncio.run(main())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
