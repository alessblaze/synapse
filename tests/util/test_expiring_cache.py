#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 OpenMarket Ltd
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

from typing import List

from synapse.util.expiring_cache_compat import ExpiringCache

from tests.server import get_clock

from .. import unittest


class ExpiringCacheTestCase(unittest.HomeserverTestCase):
    def test_get_set(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=1,
        )

        cache["key"] = "value"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache["key"], "value")

    def test_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=2,
        )

        cache["key"] = "value"
        cache["key2"] = "value2"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache.get("key2"), "value2")

        cache["key3"] = "value3"
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), "value2")
        self.assertEqual(cache.get("key3"), "value3")

    def test_iterable_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, List[int]] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=5,
            iterable=True,
        )

        cache["key"] = [1]
        cache["key2"] = [2, 3]
        cache["key3"] = [4, 5]

        self.assertEqual(cache.get("key"), [1])
        self.assertEqual(cache.get("key2"), [2, 3])
        self.assertEqual(cache.get("key3"), [4, 5])

        cache["key4"] = [6, 7]
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache.get("key3"), [4, 5])
        self.assertEqual(cache.get("key4"), [6, 7])

    def test_time_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, int] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            expiry_ms=1000,
        )

        print(f"DEBUG: Initial clock time: {clock.time_msec()}")
        cache["key"] = 1
        print(f"DEBUG: After setting key, clock time: {clock.time_msec()}")
        
        reactor.advance(0.5)
        print(f"DEBUG: After advance(0.5), clock time: {clock.time_msec()}")
        cache["key2"] = 2
        print(f"DEBUG: After setting key2, clock time: {clock.time_msec()}")

        print(f"DEBUG: About to get key, clock time: {clock.time_msec()}")
        result1 = cache.get("key")
        print(f"DEBUG: cache.get('key') returned: {result1}")
        self.assertEqual(result1, 1)
        
        result2 = cache.get("key2")
        print(f"DEBUG: cache.get('key2') returned: {result2}")
        self.assertEqual(result2, 2)

        reactor.advance(0.9)
        print(f"DEBUG: After advance(0.9), clock time: {clock.time_msec()}")
        result3 = cache.get("key")
        print(f"DEBUG: cache.get('key') after 0.9s returned: {result3}")
        self.assertEqual(result3, None)
        
        result4 = cache.get("key2")
        print(f"DEBUG: cache.get('key2') after 0.9s returned: {result4}")
        self.assertEqual(result4, 2)

        reactor.advance(1)
        print(f"DEBUG: After advance(1), clock time: {clock.time_msec()}")
        result5 = cache.get("key")
        print(f"DEBUG: cache.get('key') after 1s returned: {result5}")
        self.assertEqual(result5, None)
        
        result6 = cache.get("key2")
        print(f"DEBUG: cache.get('key2') after 1s returned: {result6}")
        self.assertEqual(result6, None)
