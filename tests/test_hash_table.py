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
    def test_insert_update_lookup_and_miss(self):
        table = OpenAddressHashTable()

        assert table.set("name", "mini-redis") is True
        assert table.get("name") == "mini-redis"
        assert table.contains("name") is True
        assert len(table) == 1

        assert table.set("name", "redis-like") is False
        assert table.get("name") == "redis-like"
        assert len(table) == 1
        assert table.get("missing") is None

    def test_delete_and_repeated_delete(self):
        table = OpenAddressHashTable()
        table.set("name", "mini-redis")

        assert table.delete("name") is True
        assert table.get("name") is None
        assert len(table) == 0
        assert table.delete("name") is False

    def test_lookup_continues_across_tombstone(self):
        table = OpenAddressHashTable()
        first_key, second_key = _find_colliding_keys(2, table.capacity)

        table.set(first_key, "v1")
        table.set(second_key, "v2")
        assert table.delete(first_key) is True

        assert table.get(second_key) == "v2"
        assert table.contains(second_key) is True

    def test_insert_reuses_tombstone_when_no_matching_key_exists_later(self):
        table = OpenAddressHashTable()
        first_key, second_key, third_key = _find_colliding_keys(3, table.capacity)

        table.set(first_key, "v1")
        table.set(second_key, "v2")
        assert table.delete(first_key) is True
        used_before_reuse = table.used

        assert table.set(third_key, "v3") is True
        assert table.get(third_key) == "v3"
        assert table.get(second_key) == "v2"
        assert table.used == used_before_reuse
        assert len(table) == 2

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

    def test_shrink_resize_discards_tombstones_and_preserves_entries(self):
        table = OpenAddressHashTable()

        for index in range(6):
            table.set(f"field-{index}", f"value-{index}")

        assert table.capacity == 16

        assert table.delete("field-0") is True
        assert table.delete("field-1") is True
        assert table.delete("field-2") is True

        assert table.capacity == 8
        assert len(table) == 3
        assert table.used == 3
        for index in range(3, 6):
            assert table.get(f"field-{index}") == f"value-{index}"

    def test_repeated_delete_and_reinsert_edge_case(self):
        table = OpenAddressHashTable()
        first_key, second_key = _find_colliding_keys(2, table.capacity)

        table.set(first_key, "v1")
        table.set(second_key, "v2")
        assert table.delete(first_key) is True
        assert table.delete(first_key) is False

        assert table.set(first_key, "v1-new") is True
        assert table.get(first_key) == "v1-new"
        assert table.get(second_key) == "v2"
        assert len(table) == 2


class TestHash:
    def test_hash_stays_compact_for_small_entries(self):
        hash_value = Hash()

        assert hash_value.set("field", "value") is True
        assert hash_value.is_compact is True
        assert hash_value.get("field") == "value"

    def test_hash_promotes_when_entry_count_exceeds_threshold(self):
        hash_value = Hash()

        for index in range(33):
            hash_value.set(f"field-{index}", f"value-{index}")

        assert hash_value.is_compact is False
        assert len(hash_value) == 33

    def test_hash_promotes_when_value_exceeds_byte_threshold(self):
        hash_value = Hash()

        hash_value.set("field", "x" * 65)

        assert hash_value.is_compact is False
        assert hash_value.get("field") == "x" * 65

    def test_hash_preserves_contents_after_promotion(self):
        hash_value = Hash()

        for index in range(33):
            hash_value.set(f"field-{index}", f"value-{index}")

        for index in range(33):
            assert hash_value.get(f"field-{index}") == f"value-{index}"
        assert sorted(hash_value.items()) == sorted(
            (f"field-{index}", f"value-{index}") for index in range(33)
        )
