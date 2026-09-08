from protocol.encoder import encode_bulk_string
from protocol.parser import parse
from store.datastore import DataStore
from store.redis_object import make_string


def encode_command(*parts) -> bytes:
    encoded_parts = []
    for part in parts:
        if isinstance(part, str):
            raw = part.encode("utf-8")
        else:
            raw = part
        encoded_parts.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    return f"*{len(encoded_parts)}\r\n".encode() + b"".join(encoded_parts)


def test_protocol_round_trip_preserves_invalid_utf8_bytes():
    payload = b"\xff\xfehello\x80"
    command, consumed = parse(encode_command("SET", "bin", payload))

    assert consumed > 0
    assert command is not None
    assert (
        encode_bulk_string(command[2])
        == f"${len(payload)}\r\n".encode() + payload + b"\r\n"
    )
