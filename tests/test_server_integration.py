import asyncio
import socket
from contextlib import asynccontextmanager
from typing import Union

import pytest

from server import ClientLimitError, Server


def encode_command(*parts: Union[str, bytes]) -> bytes:
    encoded_parts = []
    for part in parts:
        raw = part if isinstance(part, bytes) else part.encode("utf-8")
        encoded_parts.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    return f"*{len(encoded_parts)}\r\n".encode() + b"".join(encoded_parts)


class RespStream:
    def __init__(self, reader):
        self.reader = reader

    async def read(self):
        line = await self.reader.readuntil(b"\r\n")
        prefix, value = line[:1], line[1:-2]
        if prefix == b"+":
            return value.decode()
        if prefix == b"-":
            return RuntimeError(value.decode())
        if prefix == b":":
            return int(value)
        if prefix == b"$":
            return (
                None
                if int(value) == -1
                else (await self.reader.readexactly(int(value) + 2))[:-2]
            )
        if prefix == b"*":
            return [await self.read() for _ in range(int(value))]
        raise AssertionError(f"Unexpected RESP prefix: {prefix!r}")


@asynccontextmanager
async def running_server(**server_kwargs):
    redis_server = Server(**server_kwargs)
    tcp_server = await asyncio.start_server(redis_server.handle_client, "127.0.0.1", 0)
    host, port = tcp_server.sockets[0].getsockname()[:2]
    try:
        yield host, port
    finally:
        tcp_server.close()
        await tcp_server.wait_closed()


async def open_client(host: str, port: int):
    reader, writer = await asyncio.open_connection(host, port)
    return RespStream(reader), writer


async def open_raw_socket(host: str, port: int, *, recv_buffer_bytes: int = 1024):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buffer_bytes)
    await loop.sock_connect(sock, (host, port))
    return sock


@pytest.mark.asyncio
async def test_concurrent_incr_across_multiple_clients():
    async with running_server() as (host, port):
        client_count = 12
        increments_per_client = 40

        async def worker():
            stream, writer = await open_client(host, port)
            try:
                for _ in range(increments_per_client):
                    writer.write(encode_command("INCR", "counter"))
                    await writer.drain()
                    assert await stream.read() is not None
            finally:
                writer.close()
                await writer.wait_closed()

        await asyncio.gather(*(worker() for _ in range(client_count)))

        stream, writer = await open_client(host, port)
        try:
            writer.write(encode_command("GET", "counter"))
            await writer.drain()
            assert (
                await stream.read()
                == str(client_count * increments_per_client).encode()
            )
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_large_pipeline_preserves_response_order():
    async with running_server(max_commands_per_tick=32) as (host, port):
        stream, writer = await open_client(host, port)
        try:
            pipeline_size = 250
            writer.write(
                b"".join(encode_command("INCR", "pipe") for _ in range(pipeline_size))
            )
            await writer.drain()

            responses = [await stream.read() for _ in range(pipeline_size)]
            assert responses[0] == 1
            assert responses[-1] == pipeline_size
            assert responses == list(range(1, pipeline_size + 1))
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_repeated_connect_disconnect_keeps_server_responsive():
    async with running_server() as (host, port):
        for _ in range(30):
            stream, writer = await open_client(host, port)
            try:
                writer.write(encode_command("PING"))
                await writer.drain()
                assert await stream.read() == "PONG"
            finally:
                writer.close()
                await writer.wait_closed()


@pytest.mark.asyncio
async def test_request_buffer_limit_closes_oversized_partial_request():
    async with running_server(
        read_chunk=64,
        max_input_buffer_bytes=128,
        client_idle_timeout_seconds=1.0,
    ) as (host, port):
        sock = await open_raw_socket(host, port)
        loop = asyncio.get_running_loop()
        try:
            partial = b"*2\r\n$3\r\nGET\r\n$256\r\n" + b"a" * 160
            await loop.sock_sendall(sock, partial)
            await asyncio.sleep(0.1)
            data = await loop.sock_recv(sock, 4096)
            assert b"ERR request buffer limit exceeded" in data
        finally:
            sock.close()


@pytest.mark.asyncio
async def test_slow_client_guard_raises_when_drain_cannot_clear_output_buffer():
    class FakeTransport:
        def __init__(self, size: int):
            self.size = size

        def set_write_buffer_limits(self, high: int, low: int):
            self.high = high
            self.low = low

        def get_write_buffer_size(self) -> int:
            return self.size

    class FakeWriter:
        def __init__(self, size: int):
            self.transport = FakeTransport(size)

        async def drain(self):
            await asyncio.sleep(1.0)

    server = Server(
        max_output_buffer_bytes=1024,
        write_drain_timeout_seconds=0.01,
    )
    writer = FakeWriter(size=4096)

    with pytest.raises(ClientLimitError, match="write drain timeout"):
        await server._flush_output_if_needed(writer)
