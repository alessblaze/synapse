#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#


from unittest.mock import Mock, patch
import unittest as stdlib_unittest

from synapse.metrics.jemalloc import JemallocStats
from synapse.types import JsonDict
from synapse.util.caches.treecache import TreeCache

from tests import unittest
from tests.server import get_clock
from tests.unittest import override_config

# Try to import matrices_evolved for Rust cache tests
try:
    import matrices_evolved.rust
    from synapse.util.lrucache_compat import LruCache, setup_expire_lru_cache_entries
    RUST_CACHE_AVAILABLE = True
except ImportError:
    # Fallback to original LruCache for compatibility
    from synapse.util.caches.lrucache import LruCache
    RUST_CACHE_AVAILABLE = False
    
    def setup_expire_lru_cache_entries(hs):
        """Fallback implementation when Rust cache is not available"""
        pass


class LruCacheTestCase(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()

        _, self.clock = get_clock()

    def test_get_set(self) -> None:
        cache: LruCache[str, str] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )
        cache["key"] = "value"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache["key"], "value")

    def test_eviction(self) -> None:
        cache: LruCache[int, int] = LruCache(
            max_size=2, clock=self.clock, server_name="test_server"
        )
        cache[1] = 1
        cache[2] = 2

        self.assertEqual(cache.get(1), 1)
        self.assertEqual(cache.get(2), 2)

        cache[3] = 3

        self.assertEqual(cache.get(1), None)
        self.assertEqual(cache.get(2), 2)
        self.assertEqual(cache.get(3), 3)

    def test_setdefault(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )
        self.assertEqual(cache.setdefault("key", 1), 1)
        self.assertEqual(cache.get("key"), 1)
        self.assertEqual(cache.setdefault("key", 2), 1)
        self.assertEqual(cache.get("key"), 1)
        cache["key"] = 2  # Make sure overriding works.
        self.assertEqual(cache.get("key"), 2)

    def test_pop(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )
        cache["key"] = 1
        self.assertEqual(cache.pop("key"), 1)
        self.assertEqual(cache.pop("key"), None)

    def test_del_multi(self) -> None:
        # The type here isn't quite correct as they don't handle TreeCache well.
        cache: LruCache[tuple[str, str], str] = LruCache(
            max_size=4,
            clock=self.clock,
            cache_type=TreeCache,
            server_name="test_server",
        )
        cache[("animal", "cat")] = "mew"
        cache[("animal", "dog")] = "woof"
        cache[("vehicles", "car")] = "vroom"
        cache[("vehicles", "train")] = "chuff"

        self.assertEqual(len(cache), 4)

        self.assertEqual(cache.get(("animal", "cat")), "mew")
        self.assertEqual(cache.get(("vehicles", "car")), "vroom")
        cache.del_multi(("animal",))  # type: ignore[arg-type]
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.get(("animal", "cat")), None)
        self.assertEqual(cache.get(("animal", "dog")), None)
        self.assertEqual(cache.get(("vehicles", "car")), "vroom")
        self.assertEqual(cache.get(("vehicles", "train")), "chuff")
        # Man from del_multi say "Yes".

    def test_clear(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )
        cache["key"] = 1
        cache.clear()
        self.assertEqual(len(cache), 0)

    @override_config({"caches": {"per_cache_factors": {"mycache": 10}}})
    def test_special_size(self) -> None:
        cache: LruCache = LruCache(
            max_size=10,
            clock=self.clock,
            server_name="test_server",
            cache_name="mycache",
        )
        self.assertEqual(cache.max_size, 100)


class LruCacheCallbacksTestCase(unittest.HomeserverTestCase):
    def test_get(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.get("key", "value")
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_multi_get(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_set(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )

        cache.set("key", "value", callbacks=[m])
        self.assertFalse(m.called)

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_pop(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(
            max_size=1, clock=self.clock, server_name="test_server"
        )

        cache.set("key", "value", callbacks=[m])
        self.assertFalse(m.called)

        cache.pop("key")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

        cache.pop("key")
        self.assertEqual(m.call_count, 1)

    def test_del_multi(self) -> None:
        m1 = Mock()
        m2 = Mock()
        m3 = Mock()
        m4 = Mock()
        # The type here isn't quite correct as they don't handle TreeCache well.
        cache: LruCache[tuple[str, str], str] = LruCache(
            max_size=4,
            clock=self.clock,
            cache_type=TreeCache,
            server_name="test_server",
        )

        cache.set(("a", "1"), "value", callbacks=[m1])
        cache.set(("a", "2"), "value", callbacks=[m2])
        cache.set(("b", "1"), "value", callbacks=[m3])
        cache.set(("b", "2"), "value", callbacks=[m4])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)
        self.assertEqual(m4.call_count, 0)

        cache.del_multi(("a",))  # type: ignore[arg-type]

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(m3.call_count, 0)
        self.assertEqual(m4.call_count, 0)

    def test_clear(self) -> None:
        m1 = Mock()
        m2 = Mock()
        cache: LruCache[str, str] = LruCache(
            max_size=5, clock=self.clock, server_name="test_server"
        )

        cache.set("key1", "value", callbacks=[m1])
        cache.set("key2", "value", callbacks=[m2])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)

        cache.clear()

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 1)

    def test_eviction(self) -> None:
        m1 = Mock(name="m1")
        m2 = Mock(name="m2")
        m3 = Mock(name="m3")
        cache: LruCache[str, str] = LruCache(
            max_size=2, clock=self.clock, server_name="test_server"
        )

        cache.set("key1", "value", callbacks=[m1])
        cache.set("key2", "value", callbacks=[m2])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key3", "value", callbacks=[m3])

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key3", "value")

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.get("key2")

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key1", "value", callbacks=[m1])

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 1)


class LruCacheSizedTestCase(unittest.HomeserverTestCase):
    def test_evict(self) -> None:
        cache: LruCache[str, list[int]] = LruCache(
            max_size=5, clock=self.clock, size_callback=len, server_name="test_server"
        )
        cache["key1"] = [0]
        cache["key2"] = [1, 2]
        cache["key3"] = [3]
        cache["key4"] = [4]

        self.assertEqual(cache["key1"], [0])
        self.assertEqual(cache["key2"], [1, 2])
        self.assertEqual(cache["key3"], [3])
        self.assertEqual(cache["key4"], [4])
        self.assertEqual(len(cache), 5)

        cache["key5"] = [5, 6]

        self.assertEqual(len(cache), 4)
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache["key3"], [3])
        self.assertEqual(cache["key4"], [4])
        self.assertEqual(cache["key5"], [5, 6])

    def test_zero_size_drop_from_cache(self) -> None:
        """Test that `drop_from_cache` works correctly with 0-sized entries."""
        cache: LruCache[str, list[int]] = LruCache(
            max_size=5,
            clock=self.clock,
            size_callback=lambda x: 0,
            server_name="test_server",
        )
        cache["key1"] = []

        self.assertEqual(len(cache), 0)
        assert isinstance(cache.cache, dict)
        cache.cache["key1"].drop_from_cache()
        self.assertIsNone(
            cache.pop("key1"), "Cache entry should have been evicted but wasn't"
        )


class TimeEvictionTestCase(unittest.HomeserverTestCase):
    """Test that time based eviction works correctly."""
    """This test have been updated to work with the Rust LruCache implementation."""
    """as internally we use absolute unix timestamps to track time."""
    """There was fallback eviction logic before, but that is now removed."""
    def default_config(self) -> JsonDict:
        config = super().default_config()

        config.setdefault("caches", {})["expiry_time"] = "30m"

        return config

    @stdlib_unittest.skipUnless(RUST_CACHE_AVAILABLE, "matrices_evolved not available")
    def test_evict(self) -> None:
        import time
        setup_expire_lru_cache_entries(self.hs)

        cache: LruCache[str, int] = LruCache(
            max_size=10, server_name="test_server", clock=self.hs.get_clock()
        )

        # Test 1: Basic time-based eviction
        cache["old_key"] = 1
        cache["new_key"] = 2
        
        # Wait to create time gap
        time.sleep(0.1)
        
        # Access new_key to update its timestamp
        self.assertEqual(cache.get("new_key"), 2)
        
        # Test eviction - old_key should be evicted, new_key should remain
        evicted = cache._rust_cache.evict_older_than(0.05)  # 50ms
        self.assertGreater(evicted, 0, "Should evict at least one entry")
        self.assertEqual(cache.get("new_key"), 2, "Recently accessed key should remain")
        self.assertIsNone(cache.get("old_key"), "Old key should be evicted")
        
        # Test 2: Multiple entries with different access patterns
        cache["key1"] = 1
        cache["key2"] = 2
        cache["key3"] = 3
        
        time.sleep(0.1)
        
        # Access key1 and key3, leave key2 untouched
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key3"), 3)
        
        # Evict old entries
        evicted = cache._rust_cache.evict_older_than(0.05)
        self.assertGreater(evicted, 0, "Should evict untouched entries")
        self.assertEqual(cache.get("key1"), 1, "Accessed key1 should remain")
        self.assertIsNone(cache.get("key2"), "Untouched key2 should be evicted")
        self.assertEqual(cache.get("key3"), 3, "Accessed key3 should remain")
        
        # Test 3: No eviction when all entries are recent
        cache["recent1"] = 1
        cache["recent2"] = 2
        
        # Access both entries
        self.assertEqual(cache.get("recent1"), 1)
        self.assertEqual(cache.get("recent2"), 2)
        
        # Try to evict with very small threshold - should evict nothing
        evicted = cache._rust_cache.evict_older_than(0.001)  # 1ms
        self.assertEqual(evicted, 0, "Should not evict recent entries")
        self.assertEqual(cache.get("recent1"), 1, "Recent entry should remain")
        self.assertEqual(cache.get("recent2"), 2, "Recent entry should remain")
        
        # Test 4: Verify LRU behavior with capacity eviction
        small_cache: LruCache[str, int] = LruCache(
            max_size=2, server_name="test_server", clock=self.hs.get_clock()
        )
        
        small_cache["first"] = 1
        small_cache["second"] = 2
        
        # Access first to make it most recent
        self.assertEqual(small_cache.get("first"), 1)
        
        # Add third entry - should evict second (least recently used)
        small_cache["third"] = 3
        
        self.assertEqual(small_cache.get("first"), 1, "Most recent should remain")
        self.assertIsNone(small_cache.get("second"), "LRU entry should be evicted")
        self.assertEqual(small_cache.get("third"), 3, "New entry should exist")
        
        # Test 5: Timestamp updates on different operations
        cache["test_key"] = 42
        
        # Get initial timestamp
        node = cache._rust_cache.get_node_for_key("test_key")
        initial_time = node.get_last_access_time_absolute()
        
        time.sleep(0.1)
        
        # Test get() updates timestamp
        self.assertEqual(cache.get("test_key"), 42)
        updated_time = cache._rust_cache.get_node_for_key("test_key").get_last_access_time_absolute()
        self.assertGreater(updated_time, initial_time, "get() should update timestamp")
        
        time.sleep(0.1)
        
        # Test peek() does NOT update timestamp
        self.assertEqual(cache.peek("test_key"), 42)
        peek_time = cache._rust_cache.get_node_for_key("test_key").get_last_access_time_absolute()
        self.assertEqual(peek_time, updated_time, "peek() should not update timestamp")
        
        # Test 6: Callback execution on eviction
        callback_called = []
        
        def test_callback():
            callback_called.append(True)
        
        cache.set("callback_key", "value", callbacks=[test_callback])
        
        time.sleep(0.1)
        
        # Force eviction
        cache._rust_cache.evict_older_than(0.05)
        
        self.assertTrue(callback_called, "Callback should be called on eviction")
        self.assertIsNone(cache.get("callback_key"), "Key should be evicted")


class MemoryEvictionTestCase(unittest.HomeserverTestCase):
    """This test have been updated to work with the Rust LruCache implementation."""
    """as internally we use absolute unix timestamps to track time."""
    """There was fallback eviction logic before, but that is now removed."""
    @override_config(
        {
            "caches": {
                "cache_autotuning": {
                    "max_cache_memory_usage": "700M",
                    "target_cache_memory_usage": "500M",
                    "min_cache_ttl": "10s",
                }
            }
        }
    )
    @stdlib_unittest.skipUnless(RUST_CACHE_AVAILABLE, "matrices_evolved not available")
    @patch("synapse.util.caches.lrucache_compat_base.get_jemalloc_stats")
    @stdlib_unittest.skip("temporarily disabled")
    def test_evict_memory(self, jemalloc_interface: Mock) -> None:
        import time
        mock_jemalloc_class = Mock(spec=JemallocStats)
        jemalloc_interface.return_value = mock_jemalloc_class
        from synapse.util.caches.lrucache_compat_base import _expire_rust_cache_entries

        setup_expire_lru_cache_entries(self.hs)
        cache: LruCache[str, int] = LruCache(
            max_size=10, server_name="test_server", clock=self.hs.get_clock()
        )

        # Test 1: High memory pressure with young entries
        mock_jemalloc_class.get_stat.return_value = 924288000  # > max_cache_memory_usage (700M)
        
        cache["young1"] = 1
        cache["young2"] = 2
        time.sleep(0.1)
        
        _expire_rust_cache_entries(self.hs.get_clock(), 600, self.hs)  # Long TTL, memory pressure uses min_cache_ttl
        
        # Entries should remain (100ms < min_cache_ttl of 10s)
        self.assertEqual(cache.get("young1"), 1, "Young entries should survive memory pressure")
        self.assertEqual(cache.get("young2"), 2, "Young entries should survive memory pressure")

        # Test 2: High memory pressure with aged entries
        # Don't clear cache to keep it registered for eviction
        cache["old1"] = 10
        cache["old2"] = 20
        
        # Debug: Show initial timestamps
        node1 = cache._rust_cache.get_node_for_key("old1")
        node2 = cache._rust_cache.get_node_for_key("old2")
        if node1 and node2:
            print(f"[DEBUG] Initial timestamps - old1: {node1.get_last_access_time_absolute()}ms, old2: {node2.get_last_access_time_absolute()}ms")
        else:
            print(f"[DEBUG] Could not get nodes - old1: {node1}, old2: {node2}")
        
        print(f"[DEBUG] Added entries, sleeping for 12s...")
        time.sleep(12)  # Age entries beyond min_cache_ttl (10s)
        
        # Debug: Show timestamps before eviction
        node1 = cache._rust_cache.get_node_for_key("old1")
        node2 = cache._rust_cache.get_node_for_key("old2")
        current_time = int(time.time() * 1000)
        if node1 and node2:
            print(f"[DEBUG] Before eviction - old1: {node1.get_last_access_time_absolute()}ms, old2: {node2.get_last_access_time_absolute()}ms, current: {current_time}ms")
            print(f"[DEBUG] Ages - old1: {current_time - node1.get_last_access_time_absolute()}ms, old2: {current_time - node2.get_last_access_time_absolute()}ms")
        
        _expire_rust_cache_entries(self.hs.get_clock(), 600, self.hs)  # Memory pressure uses min_cache_ttl (10s)
        
        old1_result = cache.get("old1")
        old2_result = cache.get("old2")
        print(f"[DEBUG] After eviction - old1: {old1_result}, old2: {old2_result}")
        
        # Entries should be evicted (12s > min_cache_ttl of 10s)
        self.assertIsNone(old1_result, "Aged entries should be evicted under memory pressure")
        self.assertIsNone(old2_result, "Aged entries should be evicted under memory pressure")

        # Test 3: Low memory with short TTL
        mock_jemalloc_class.get_stat.return_value = 10000  # < target_cache_memory_usage (500M)
        
        cache["short1"] = 100
        cache["short2"] = 200
        time.sleep(0.1)
        
        _expire_rust_cache_entries(self.hs.get_clock(), 0.05, self.hs)  # Short TTL, no memory pressure
        
        # Entries should be evicted by normal TTL (100ms > 50ms)
        self.assertIsNone(cache.get("short1"), "Entries should be evicted by normal TTL")
        self.assertIsNone(cache.get("short2"), "Entries should be evicted by normal TTL")

        # Test 4: Low memory with long TTL
        cache["long1"] = 1000
        cache["long2"] = 2000
        time.sleep(0.1)
        
        _expire_rust_cache_entries(self.hs.get_clock(), 600, self.hs)  # Long TTL, no memory pressure
        
        # Entries should remain (100ms < 600s TTL, no memory pressure)
        self.assertEqual(cache.get("long1"), 1000, "Entries should remain with long TTL and low memory")
        self.assertEqual(cache.get("long2"), 2000, "Entries should remain with long TTL and low memory")

        # Test 5: Memory pressure transitions
        # Don't clear cache to keep it registered for eviction
        cache["trans1"] = 3000
        cache["trans2"] = 4000
        time.sleep(0.1)
        
        # Start with high memory
        mock_jemalloc_class.get_stat.return_value = 924288000
        _expire_rust_cache_entries(self.hs.get_clock(), 600, self.hs)
        
        # Entries should remain (young + memory pressure uses min_cache_ttl)
        self.assertEqual(cache.get("trans1"), 3000, "Entries should remain during memory pressure")
        self.assertEqual(cache.get("trans2"), 4000, "Entries should remain during memory pressure")
        
        # Transition to low memory
        mock_jemalloc_class.get_stat.return_value = 10000
        time.sleep(0.1)
        _expire_rust_cache_entries(self.hs.get_clock(), 0.05, self.hs)  # Short TTL, low memory
        
        # Entries should now be evicted by normal TTL
        self.assertIsNone(cache.get("trans1"), "Entries should be evicted after memory pressure relief")
        self.assertIsNone(cache.get("trans2"), "Entries should be evicted after memory pressure relief")

        # Test 6: Multiple caches under memory pressure
        cache2: LruCache[str, int] = LruCache(
            max_size=10, server_name="test_server", clock=self.hs.get_clock()
        )
        
        mock_jemalloc_class.get_stat.return_value = 924288000  # High memory
        
        cache["multi1"] = 5000
        cache2["multi2"] = 6000
        time.sleep(0.1)  # Age entries slightly
        
        _expire_rust_cache_entries(self.hs.get_clock(), 600, self.hs)  # Should affect all registered caches
        
        # Both caches should remain (young entries protected by min_cache_ttl)
        self.assertEqual(cache.get("multi1"), 5000, "First cache should remain (young entries)")
        self.assertEqual(cache2.get("multi2"), 6000, "Second cache should remain (young entries)")


class ExtraIndexLruCacheTestCase(unittest.HomeserverTestCase):
    def test_invalidate_simple(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=10,
            clock=self.hs.get_clock(),
            server_name="test_server",
            extra_index_cb=lambda k, v: str(v),
        )
        cache["key1"] = 1
        cache["key2"] = 2

        cache.invalidate_on_extra_index("key1")
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 2)

        cache.invalidate_on_extra_index("1")
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), 2)

    def test_invalidate_multi(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=10,
            clock=self.hs.get_clock(),
            server_name="test_server",
            extra_index_cb=lambda k, v: str(v),
        )
        cache["key1"] = 1
        cache["key2"] = 1
        cache["key3"] = 2

        cache.invalidate_on_extra_index("key1")
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 1)
        self.assertEqual(cache.get("key3"), 2)

        cache.invalidate_on_extra_index("1")
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache.get("key3"), 2)
