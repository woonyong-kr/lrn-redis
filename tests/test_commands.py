"""One public success/error contract per supported command, with independent expected values."""

import pytest
from benchmark.compatibility import cases
from commands.dispatcher import COMMAND_TABLE, dispatch
from protocol.encoder import RespError
from store.datastore import DataStore
from store.expiry import ExpiryManager

EXPECTED = {
    "APPEND": 3,
    "DECR": 1,
    "DEL": 1,
    "EXISTS": 2,
    "EXPIRE": 1,
    "FLUSHALL": "OK",
    "GET": "v",
    "HDEL": 1,
    "HEXISTS": 1,
    "HGET": "1",
    "HGETALL": ["a", "1", "b", "2"],
    "HKEYS": ["a", "b"],
    "HLEN": 2,
    "HMGET": ["1", None],
    "HMSET": "OK",
    "HSET": 2,
    "HVALS": ["1", "2"],
    "INCR": 2,
    "INCRBY": 5,
    "KEYS": ["a", "b"],
    "LINDEX": "c",
    "LLEN": 3,
    "LPOP": "a",
    "LPUSH": 2,
    "LRANGE": ["a", "b", "c"],
    "LSET": "OK",
    "MGET": ["1", None, "2"],
    "MSET": "OK",
    "PERSIST": 1,
    "PEXPIREAT": 1,
    "PING": "PONG",
    "RPOP": "c",
    "RPUSH": 2,
    "SADD": 2,
    "SCARD": 3,
    "SDIFF": ["a", "c"],
    "SET": "OK",
    "SINTER": ["b", "c"],
    "SISMEMBER": 1,
    "SMEMBERS": ["a", "b", "c"],
    "SREM": 1,
    "STRLEN": 3,
    "SUNION": ["a", "b", "c", "d"],
    "TTL": 60,
    "TYPE": "string",
    "ZADD": 2,
    "ZCARD": 3,
    "ZRANGE": ["one", "1", "two", "2", "three", "3"],
    "ZRANGEBYSCORE": ["one", "two"],
    "ZRANK": 1,
    "ZREM": 1,
    "ZREVRANGE": ["three", "3", "two", "2"],
    "ZSCORE": "2",
}


def normalize(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


@pytest.mark.parametrize("case", cases(), ids=lambda case: case.name)
def test_supported_command_success_and_invalid_arity(case):
    assert set(EXPECTED) == set(COMMAND_TABLE) == {c.name for c in cases()}
    store = DataStore()
    expiry = ExpiryManager(store)
    for cmd in case.setup:
        assert not isinstance(dispatch(list(map(str, cmd)), store, expiry), RespError)
    actual = normalize(dispatch(list(map(str, case.command)), store, expiry))
    assert not isinstance(actual, RespError)
    if case.name in {"HKEYS", "HVALS", "KEYS", "SMEMBERS", "SDIFF", "SINTER", "SUNION"}:
        actual = sorted(actual)
    if case.name == "HGETALL":
        actual = [v for pair in sorted(zip(actual[::2], actual[1::2])) for v in pair]
    if case.name == "TTL":
        assert 59 <= actual <= 60
    else:
        assert actual == EXPECTED[case.name]
    invalid = (
        [case.name, "one", "two"] if case.name in {"PING", "FLUSHALL"} else [case.name]
    )
    assert isinstance(dispatch(invalid, store, expiry), RespError)


def test_wrongtype_invalid_value_and_missing_key_do_not_corrupt_state():
    store = DataStore()
    expiry = ExpiryManager(store)

    def run(*cmd):
        return dispatch(list(cmd), store, expiry)

    assert run("GET", "missing") is None
    run("SET", "k", "safe")
    for cmd in [
        ("SET", "k", "bad", "PX", "0"),
        ("INCR", "k"),
        ("HSET", "k", "a", "b"),
        ("LPUSH", "k", "a"),
        ("SADD", "k", "a"),
        ("ZADD", "k", "1", "a"),
    ]:
        assert isinstance(run(*cmd), RespError)
        assert run("GET", "k") == b"safe"
    assert isinstance(run("LSET", "missing", "0", "x"), RespError)
    assert isinstance(run("UNSUPPORTED"), RespError)
