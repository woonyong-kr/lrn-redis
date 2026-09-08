import asyncio

import pytest

from commands.dispatcher import dispatch
from protocol.encoder import SimpleString
from server import Server
from store.datastore import DataStore
from store.expiry import ExpiryManager
from store.persistence import PersistenceManager


def test_invalid_fsync_is_rejected_before_opening_file(tmp_path):
    store = DataStore()
    with pytest.raises(ValueError, match="fsync"):
        PersistenceManager(
            store,
            ExpiryManager(store),
            aof_enabled=True,
            aof_path=str(tmp_path / "log"),
            aof_fsync="typo",
        )
    assert not (tmp_path / "log").exists()


def test_truncated_aof_fails_without_changing_source(tmp_path):
    path = tmp_path / "log"
    original = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$5\r\nabc"
    path.write_bytes(original)
    store = DataStore()
    with pytest.raises(ValueError, match="truncated"):
        PersistenceManager(
            store, ExpiryManager(store), aof_enabled=True, aof_path=str(path)
        )
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_periodic_maintenance_runs_without_more_commands(tmp_path, monkeypatch):
    server = Server(aof_enabled=True, aof_path=str(tmp_path / "log"))
    server.persistence.record_command(["SET", "cart", "book"], SimpleString("OK"))
    server.persistence._last_fsync_at = 0
    synced = asyncio.Event()
    monkeypatch.setattr("store.persistence.os.fsync", lambda fd: synced.set())
    task = asyncio.create_task(server.maintenance_loop(interval=0.01))
    try:
        await asyncio.wait_for(synced.wait(), 0.5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.persistence.close()


from commands.dispatcher import COMMAND_TABLE
from protocol.encoder import RespError


@pytest.mark.parametrize("name", sorted(COMMAND_TABLE))
def test_every_supported_command_rejects_invalid_arity(name):
    store = DataStore()
    expiry = ExpiryManager(store)
    command = [name, "one", "two"] if name in ("PING", "FLUSHALL") else [name]
    assert isinstance(dispatch(command, store, expiry), RespError)


def test_random_eviction_removes_selected_old_key(monkeypatch):
    store = DataStore(maxmemory_bytes=800, eviction_policy="allkeys-random")
    expiry = ExpiryManager(store)
    monkeypatch.setattr(store._rng, "choice", lambda keys: sorted(keys)[0])
    for i in range(10):
        assert dispatch(["SET", f"item:{i}", "x" * 200], store, expiry) == SimpleString(
            "OK"
        )
    assert dispatch(["GET", "item:9"], store, expiry) == b"x" * 200
    assert store.used_memory <= 800


def test_aof_does_not_resurrect_a_key_evicted_by_its_own_write(tmp_path, monkeypatch):
    store = DataStore(maxmemory_bytes=800, eviction_policy="allkeys-random")
    expiry = ExpiryManager(store)
    manager = PersistenceManager(
        store, expiry, aof_enabled=True, aof_path=str(tmp_path / "log")
    )
    monkeypatch.setattr(store._rng, "choice", lambda keys: sorted(keys)[-1])
    for key in ["a", "b"]:
        cmd = ["SET", key, "x" * 200]
        result = dispatch(cmd, store, expiry)
        manager.record_command(cmd, result)
    assert store.exists("a") and not store.exists("b")
    manager.close()
    restored = DataStore()
    restored_expiry = ExpiryManager(restored)
    replay = PersistenceManager(
        restored, restored_expiry, aof_enabled=True, aof_path=str(tmp_path / "log")
    )
    assert restored.exists("a") and not restored.exists("b")
    replay.close()
