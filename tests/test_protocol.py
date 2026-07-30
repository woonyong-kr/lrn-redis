"""
팀원 A 테스트 - protocol/parser.py, protocol/encoder.py

구현 후 아래 명령어로 테스트:
  pytest tests/test_protocol.py -v
"""

from protocol.parser import parse
from protocol.encoder import (
    encode_simple_string, encode_error, encode_integer,
    encode_bulk_string, encode_array
)


class TestEncoder:
    def test_simple_string(self):
        assert encode_simple_string("OK") == b"+OK\r\n"
        assert encode_simple_string("PONG") == b"+PONG\r\n"

    def test_error(self):
        assert encode_error("ERR unknown") == b"-ERR unknown\r\n"

    def test_integer(self):
        assert encode_integer(0) == b":0\r\n"
        assert encode_integer(1000) == b":1000\r\n"
        assert encode_integer(-1) == b":-1\r\n"

    def test_bulk_string(self):
        assert encode_bulk_string("foobar") == b"$6\r\nfoobar\r\n"
        assert encode_bulk_string(b"foobar") == b"$6\r\nfoobar\r\n"
        assert encode_bulk_string("") == b"$0\r\n\r\n"
        assert encode_bulk_string(None) == b"$-1\r\n"

    def test_array(self):
        assert encode_array([]) == b"*0\r\n"
        assert encode_array(None) == b"*-1\r\n"
        result = encode_array(["foo", "bar"])
        assert result == b"*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"


class TestParser:
    def test_parse_set_command(self):
        data = b"*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"
        command, consumed = parse(data)
        assert command == ["SET", "foo", "bar"]
        assert consumed == len(data)

    def test_parse_get_command(self):
        data = b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n"
        command, consumed = parse(data)
        assert command == ["GET", "foo"]

    def test_parse_incomplete_data(self):
        # 데이터가 불완전하면 None 반환
        data = b"*3\r\n$3\r\nSET\r\n"
        command, consumed = parse(data)
        assert command is None
        assert consumed == 0

    def test_parse_ping(self):
        data = b"*1\r\n$4\r\nPING\r\n"
        command, consumed = parse(data)
        assert command == ["PING"]

    def test_parse_pipeline(self):
        # 두 명령어가 연속으로 오는 경우
        cmd1 = b"*1\r\n$4\r\nPING\r\n"
        cmd2 = b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n"
        data = cmd1 + cmd2

        command1, consumed1 = parse(data)
        assert command1 == ["PING"]

        command2, consumed2 = parse(data[consumed1:])
        assert command2 == ["GET", "foo"]
