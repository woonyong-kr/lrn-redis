import time

from commands.dispatcher import dispatch
from commands.generic_cmds import cmd_del, cmd_expire
from commands.string_cmds import cmd_get, cmd_set
from protocol.encoder import RespError, SimpleString
from store.datastore import DataStore
from store.expiry import ExpiryManager
from store.persistence import PersistenceManager, RDB_MAGIC
from store.redis_object import make_string


def test_aof_replays_latest_state_and_ttl(tmp_path):
    aof_path = tmp_path / "appendonly.aof"

    store = DataStore()
    expiry = ExpiryManager(store)
    persistence = PersistenceManager(
        store,
        expiry,
        aof_enabled=True,
        aof_path=str(aof_path),
    )

    assert cmd_set(store, expiry, ["foo", "bar"]) == SimpleString("OK")
    persistence.record_command(["SET", "foo", "bar"], SimpleString("OK"))
    assert dispatch(["INCR", "counter"], store, expiry) == 1
    persistence.record_command(["INCR", "counter"], 1)
    assert cmd_expire(store, expiry, ["foo", "60"]) == 1
    persistence.record_command(["EXPIRE", "foo", "60"], 1)
    assert cmd_del(store, expiry, ["counter"]) == 1
    persistence.record_command(["DEL", "counter"], 1)
    persistence.close()

    assert aof_path.read_bytes().startswith(b"*")

    restored_store = DataStore()
    restored_expiry = ExpiryManager(restored_store)
    restored = PersistenceManager(
        restored_store,
        restored_expiry,
        aof_enabled=True,
        aof_path=str(aof_path),
    )

    assert cmd_get(restored_store, restored_expiry, ["foo"]) == b"bar"
    assert restored_store.exists("counter") is False
    assert restored_expiry.get_ttl("foo") > 0
    restored.close()


def test_rdb_restores_multiple_data_types(tmp_path):
    rdb_path = tmp_path / "dump.rdb"

    store = DataStore()
    expiry = ExpiryManager(store)
    persistence = PersistenceManager(
        store,
        expiry,
        rdb_enabled=True,
        rdb_path=str(rdb_path),
    )

    store.set("plain", make_string("value"))
    store.hset("user:1", "name", "alice")
    store.rpush("letters", "a", "b")
    store.sadd("tags", "redis", "python")
    store.zadd("scores", 10.0, "alice")
    expiry.set_expiry("plain", 60)
    persistence.save_rdb()
    persistence.close()

    assert rdb_path.read_bytes().startswith(RDB_MAGIC)

    restored_store = DataStore()
    restored_expiry = ExpiryManager(restored_store)
    restored = PersistenceManager(
        restored_store,
        restored_expiry,
        rdb_enabled=True,
        rdb_path=str(rdb_path),
    )

    assert restored_store.get("plain").value == b"value"
    assert restored_store.hget("user:1", "name") == "alice"
    assert restored_store.lrange("letters", 0, -1) == ["a", "b"]
    assert restored_store.smembers("tags") == {"redis", "python"}
    assert restored_store.zrange("scores", 0, -1) == ["alice"]
    assert restored_expiry.get_ttl("plain") > 0
    restored.close()


def test_sampled_expiry_removes_expired_keys_in_multiple_passes():
    store = DataStore()
    expiry = ExpiryManager(store, sample_size=5, max_passes=10)

    for index in range(15):
        key = f"expired:{index}"
        store.set(key, make_string("gone"))
        expiry.set_expiry_at(key, time.time() - 1)

    for index in range(5):
        key = f"live:{index}"
        store.set(key, make_string("stay"))
        expiry.set_expiry_at(key, time.time() + 60)

    removed = expiry.evict_expired_samples()
    while removed < 15:
        removed += expiry.evict_expired_samples()

    assert removed == 15
    assert sorted(store.keys("live:*")) == [f"live:{index}" for index in range(5)]
    assert store.keys("expired:*") == []


def test_noeviction_rejects_write_when_limit_is_exceeded():
    store = DataStore(maxmemory_bytes=40, eviction_policy="noeviction")
    expiry = ExpiryManager(store)

    result = dispatch(["SET", "oversized", "x" * 64], store, expiry)

    assert result == RespError("OOM command not allowed when used memory > 'maxmemory'")
    assert store.exists("oversized") is False


def test_allkeys_lru_evicts_the_oldest_key():
    probe_store = DataStore()
    ExpiryManager(probe_store)
    probe_store.set("a", make_string("a" * 20))
    time.sleep(0.001)
    probe_store.set("b", make_string("b" * 20))
    limit = probe_store.used_memory + 64

    store = DataStore(maxmemory_bytes=limit, eviction_policy="allkeys-lru")
    ExpiryManager(store)

    store.set("a", make_string("a" * 20))
    time.sleep(0.001)
    store.set("b", make_string("b" * 20))
    assert store.get("b").value == b"b" * 20
    time.sleep(0.001)
    store.set("c", make_string("c" * 20))

    assert store.exists("a") is False
    assert store.exists("b") is True
    assert store.exists("c") is True


def test_volatile_ttl_evicts_key_with_nearest_expiry():
    probe_store = DataStore()
    probe_expiry = ExpiryManager(probe_store)
    probe_store.set("soon", make_string("a" * 20))
    probe_store.set("later", make_string("b" * 20))
    probe_expiry.set_expiry_at("soon", time.time() + 10)
    probe_expiry.set_expiry_at("later", time.time() + 100)
    probe_store.recompute_memory_usage()
    limit = probe_store.used_memory + 64

    store = DataStore(maxmemory_bytes=limit, eviction_policy="volatile-ttl")
    expiry = ExpiryManager(store)

    store.set("soon", make_string("a" * 20))
    store.set("later", make_string("b" * 20))
    expiry.set_expiry_at("soon", time.time() + 10)
    expiry.set_expiry_at("later", time.time() + 100)
    store.recompute_memory_usage()
    store.set("new", make_string("c" * 20))

    assert store.exists("soon") is False
    assert store.exists("later") is True
    assert store.exists("new") is True
