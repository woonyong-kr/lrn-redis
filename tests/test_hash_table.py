from store.hash_table import Hash, OpenAddressHashTable, murmurhash3_32


def _find_colliding_keys(count: int, capacity: int) -> list[str]:
    buckets: dict[int, list[str]] = {}
    candidate = 0

    while True:
        key = f"field:{candidate}"
        bucket = murmurhash3_32(key) & (capacity - 1)
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(key)
        if len(buckets[bucket]) >= count:
            return buckets[bucket][:count]
        candidate += 1


class TestOpenAddressHashTable:
    def test_lookup_continues_across_tombstone(self):
        table = OpenAddressHashTable()
        first_key, second_key = _find_colliding_keys(2, table.capacity)

        table.set(first_key, "v1")
        table.set(second_key, "v2")
        assert table.delete(first_key) is True

        assert table.get(second_key) == "v2"
        assert table.contains(second_key) is True

    def test_collision_heavy_inserts_preserve_all_contents(self):
        table = OpenAddressHashTable()
        colliding_keys = _find_colliding_keys(5, table.capacity)

        for index, key in enumerate(colliding_keys):
            table.set(key, f"value-{index}")

        for index, key in enumerate(colliding_keys):
            assert table.get(key) == f"value-{index}"
        assert len(table) == 5

    def test_grow_resize_preserves_live_entries(self):
        table = OpenAddressHashTable()

        for index in range(6):
            table.set(f"field-{index}", f"value-{index}")

        assert table.capacity == 16
        assert len(table) == 6
        for index in range(6):
            assert table.get(f"field-{index}") == f"value-{index}"
