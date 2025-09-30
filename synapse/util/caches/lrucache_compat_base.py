#
# Copyright (C) 2025 Aless Microsystems
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

# The emojis here used for a specific reason: they make it easy to spot in large flowing logs.
# There is a lot of logging in this module to help diagnose issues with the cache.
from typing import Any, Callable, Collection, Dict, Generic, Optional, Set, TypeVar, Union, overload
from matrices_evolved.rust import create_rust_lru_cache, create_async_rust_lru_cache
from twisted.internet import defer
import logging
import os
import asyncio
import threading
from enum import Enum

# Module-level sentinel for cache miss detection
class _Sentinel(Enum):
    sentinel = object()

SENTINEL = _Sentinel.sentinel

logger = logging.getLogger(__name__)
LRU_DEBUG = os.environ.get("SYNAPSE_AMS_LRU_DEBUG") == "1"
LRU_INFO = os.environ.get("SYNAPSE_AMS_LRU_INFO") == "1"
LRU_ASYNC_DEBUG = os.environ.get("SYNAPSE_AMS_LRU_ASYNC_DEBUG") == "1"

# Startup logging to verify environment variables
if LRU_DEBUG or LRU_INFO or LRU_ASYNC_DEBUG:
    logger.info(f"🚀 Rust LRU Cache module loaded - DEBUG={LRU_DEBUG}, INFO={LRU_INFO}, ASYNC_DEBUG={LRU_ASYNC_DEBUG}")
else:
    # Always log at least once to show the module is loaded
    logger.debug("🚀 Rust LRU Cache module loaded (no debug flags set)")

def log_error(msg):
    if not LRU_DEBUG:
        return
    logger.error(msg)

def log_info(msg):
    if not LRU_INFO:
        return
    logger.info(msg)

def log_debug(msg):
    if not LRU_DEBUG:
        return
    logger.debug(msg)

def log_async_debug(msg):
    if not LRU_ASYNC_DEBUG:
        return
    logger.info(f"🔄 ASYNC: {msg}")

def log_async_info(msg):
    if not LRU_ASYNC_DEBUG:
        return
    logger.info(f"⚡ ASYNC: {msg}")

KT = TypeVar("KT")
VT = TypeVar("VT")
T = TypeVar("T")

class MockMetrics:
    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def inc_hits(self): 
        self.hits += 1
    
    def inc_misses(self): 
        self.misses += 1
    
    def inc_evictions(self, reason, count=1): 
        self.evictions += count
    
    def inc_memory_usage(self, size): pass
    def dec_memory_usage(self, size): pass
    def clear_memory_usage(self): pass
    
    def record_cache_hit(self, cache_name=None): 
        self.inc_hits()
    
    def record_cache_miss(self, cache_name=None): 
        self.inc_misses()

class CacheNode:
    def __init__(self, cache, key, clock=None, prune_unread_entries=True):
        self._cache = cache
        self._key = key
        self._global_list_node = None
        
        # Add to global time-based eviction list if enabled
        if prune_unread_entries:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT, _TimedListNode, USE_GLOBAL_LIST
                if USE_GLOBAL_LIST and clock:
                    self._global_list_node = _TimedListNode.insert_after(self, GLOBAL_ROOT)
                    self._global_list_node.update_last_access(clock)
            except Exception:
                pass
    
    def drop_from_cache(self):
        # Remove from cache and ensure it's actually gone
        result = self._cache.pop(self._key, None)
        
        # Remove from global list if we were added
        if self._global_list_node:
            try:
                self._global_list_node.remove_from_list()
            except Exception:
                pass
        
        return result is not None
    
    def update_last_access(self, clock):
        # Update last access time for time-based eviction
        if self._global_list_node:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT
                self._global_list_node.move_after(GLOBAL_ROOT)
                self._global_list_node.update_last_access(clock)
            except Exception:
                pass

class CacheWrapper(dict):
    def __init__(self, rust_cache, clock=None, prune_unread_entries=True):
        super().__init__()
        self._rust_cache = rust_cache
        self._clock = clock
        self._prune_unread_entries = prune_unread_entries
        self._nodes = {}  # Track CacheNode objects
        
        # Create nodes for existing entries
        if prune_unread_entries and clock:
            try:
                for key in rust_cache:
                    self._nodes[key] = CacheNode(rust_cache, key, clock, prune_unread_entries)
            except (KeyError, RuntimeError):
                # Empty cache or iteration not supported
                pass
    
    def __getitem__(self, key):
        if key not in self._rust_cache:
            raise KeyError(key)
        
        # Create or get existing CacheNode
        if key not in self._nodes:
            self._nodes[key] = CacheNode(self._rust_cache, key, self._clock, self._prune_unread_entries)
        
        node = self._nodes[key]
        # Update last access time
        if self._clock:
            node.update_last_access(self._clock)
        
        return node
    
    def __setitem__(self, key, value):
        # When a new item is added, create a node for it
        if self._prune_unread_entries and self._clock and key not in self._nodes:
            self._nodes[key] = CacheNode(self._rust_cache, key, self._clock, self._prune_unread_entries)
    
    def __contains__(self, key):
        return key in self._rust_cache
    
    def _cleanup_node(self, key):
        # Remove node tracking when key is removed from cache
        self._nodes.pop(key, None)

class TreeCache:
    def __init__(self, rust_cache):
        self._rust_cache = rust_cache
    
    def get(self, key, default=None):
        return self._rust_cache.get_with_tuple(key, default)
    
    def set(self, key, value, callbacks=None):
        return self._rust_cache.set_with_tuple(key, value, callbacks or [])
    
    def __getitem__(self, key):
        if key not in self._rust_cache:
            raise KeyError(key)
        return self._rust_cache[key]
    
    def __setitem__(self, key, value):
        self.set(key, value)
    
    def __contains__(self, key):
        return key in self._rust_cache

class LruCache(Generic[KT, VT]):
    def __init__(
        self,
        *,
        max_size: int,
        server_name: Optional[str] = None,
        cache_name: Optional[str] = None,
        cache_type: Any = None,
        size_callback: Optional[Callable[[VT], int]] = None,
        metrics_collection_callback: Optional[Callable[[], None]] = None,
        apply_cache_factor_from_config: bool = True,
        clock: Any = None,
        prune_unread_entries: bool = True,
        extra_index_cb: Optional[Callable[[KT, VT], KT]] = None,
        keylen: int = 1,
        tree: bool = False,
    ):
        # Set attributes first before registration
        self.cache_name = cache_name
        self.apply_cache_factor_from_config = apply_cache_factor_from_config
        self._original_max_size = max_size
        
        # Apply cache factor like original
        if apply_cache_factor_from_config:
            try:
                from synapse.config import cache as cache_config
                factor = cache_config.properties.default_factor_size
                self.max_size = int(max_size * factor)
                log_info(f"🔧 Cache factor applied: {max_size} * {factor} = {self.max_size}")
            except Exception as e:
                self.max_size = int(max_size)
                log_info(f"🔧 Cache factor failed: {e}, using original size {self.max_size}")
        else:
            self.max_size = int(max_size)
            log_info(f"🔧 Cache factor disabled, using original size {self.max_size}")
            
        # Store callbacks for compatibility
        self._size_callback = size_callback
        self._extra_index_cb = extra_index_cb
        self._extra_index = {}
        
        # Create metrics if needed
        if cache_name and server_name:
            try:
                from synapse.util.caches import register_cache
                self.metrics = register_cache(
                    cache_type="rust_lru_cache",
                    cache_name=cache_name,
                    cache=self,
                    server_name=server_name,
                    collect_callback=metrics_collection_callback,
                )
            except Exception as e:
                log_error(f"❌ Failed to register Rust cache '{cache_name}' with cleanup system: {e}")
                self.metrics = MockMetrics()
        else:
            self.metrics = MockMetrics() if cache_name else None
            
        self._rust_cache = create_rust_lru_cache(self.max_size, cache_name, self.metrics, None, self._size_callback)
        log_info(f"🏗️ Created Rust LruCache '{cache_name}' with max_size={self.max_size}")
        
        # Auto-detect TreeCache mode
        from synapse.util.caches.treecache import TreeCache as PyTreeCache
        self._tree = tree or (cache_type is PyTreeCache) or keylen > 1
        
        # Expose cache attribute like original
        if self._tree:
            self.cache = TreeCache(self._rust_cache)
            # Add get_multi method for TreeCache
            self.get_multi = self._get_multi_impl
        else:
            self.cache = CacheWrapper(self._rust_cache, clock, prune_unread_entries)
            
        # Add to global cleanup list
        if prune_unread_entries:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT, _TimedListNode
                from synapse.util import Clock
                from twisted.internet import reactor
                
                class CacheEntry:
                    def __init__(self, cache_instance):
                        self.cache = cache_instance
                    def drop_from_cache(self):
                        try:
                            count = self.cache.clear()
                            log_info(f"🧹 Clearing Sync Cache:")
                            log_info(f"🧹 Global cleanup cleared {count} entries from sync cache '{self.cache.cache_name}'")
                        except Exception as e:
                            log_error(f"❌ Global cleanup failed for sync cache '{self.cache.cache_name}': {e}")
                
                cache_entry = CacheEntry(self)
                self._cleanup_node = _TimedListNode.insert_after(cache_entry, GLOBAL_ROOT)
                self._cleanup_node.update_last_access(Clock(reactor))
            except Exception as e:
                log_error(f"❌ Failed to add Rust cache '{cache_name}' to global cleanup list: {e}")

    @overload
    def get(self, key: KT, default: None = None, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Optional[VT]: ...

    @overload
    def get(self, key: KT, default: T, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Union[T, VT]: ...

    def get(self, key: KT, default: Optional[T] = None, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Union[None, T, VT]:
        if LRU_INFO:
            log_info(f"🔍 LruCache.get({self.cache_name}): {key}")
        try:
            result = self._rust_cache.get(key, default=default, callbacks=list(callbacks) if callbacks else None)
            
            # Update access time for time-based eviction if it's a hit
            if result != default and update_last_access and not self._tree and hasattr(self.cache, '__getitem__'):
                try:
                    # This will update the last access time
                    _ = self.cache[key]
                except KeyError:
                    pass
            
            # Rust cache already handles metrics via record_cache_hit/miss
            if LRU_INFO:
                log_info(f"LruCache.get({self.cache_name}): {'✅ HIT' if result != default else '❌ MISS'}")
            return result
        except Exception as e:
            if LRU_DEBUG:
                log_debug(f"❌ Rust cache get failed for key {key}: {e}")
            raise
    
    def set(self, key: KT, value: VT, callbacks: Collection[Callable[[], None]] = ()) -> None:
        if LRU_INFO:
            log_info(f"🔍 LruCache.set({self.cache_name}): {key}")
        try:
            # Handle extra index callback
            if self._extra_index_cb:
                index_key = self._extra_index_cb(key, value)
                mapped_keys = self._extra_index.setdefault(index_key, set())
                mapped_keys.add(key)
            
            self._rust_cache.set(key, value, list(callbacks) if callbacks else [])
            
            # Notify cache wrapper of new entry for time-based eviction
            if not self._tree and hasattr(self.cache, '__setitem__'):
                self.cache[key] = value
            
            if LRU_INFO:
                log_info(f"✅ LruCache.set({self.cache_name}): stored")
        except Exception as e:
            if LRU_DEBUG:
                log_debug(f"❌ Rust cache set failed for key {key}: {e}")
            raise
    
    def setdefault(self, key: KT, value: VT) -> VT:
        existing = self.get(key)
        if existing is not None:
            return existing
        self.set(key, value)
        return value
    
    @overload
    def pop(self, key: KT, default: None = None) -> Optional[VT]: ...

    @overload
    def pop(self, key: KT, default: T) -> Union[T, VT]: ...

    def pop(self, key: KT, default: Optional[T] = None) -> Union[None, T, VT]:
        log_info(f"🔍 LruCache.pop({self.cache_name}): {key}")
        try:
            # Handle extra index cleanup
            if self._extra_index_cb and key in self._rust_cache:
                try:
                    value = self._rust_cache.get(key)
                    if value is not None:
                        index_key = self._extra_index_cb(key, value)
                        mapped_keys = self._extra_index.get(index_key)
                        if mapped_keys:
                            mapped_keys.discard(key)
                            if not mapped_keys:
                                self._extra_index.pop(index_key, None)
                except Exception:
                    pass  # Ignore errors in cleanup
            
            result = self._rust_cache.pop(key, default)
            log_info(f"LruCache.pop({self.cache_name}): {'✅ FOUND' if result != default else '❌ NOT_FOUND'}")
            return result
        except Exception as e:
            log_debug(f"❌ Rust cache pop failed for key {key}: {e}")
            raise
    
    def del_multi(self, key: KT) -> None:
        log_info(f"🔍 LruCache.del_multi({self.cache_name}): {key}")
        try:
            if self._tree and isinstance(key, tuple):
                # For TreeCache mode, use prefix deletion
                self._rust_cache.invalidate_prefix(key)
            else:
                # Regular single key deletion
                self._rust_cache.invalidate(key)
            log_info(f"✅ LruCache.del_multi({self.cache_name}): invalidated")
        except Exception as e:
            log_debug(f"❌ Rust cache del_multi failed for key {key}: {e}")
            raise
    
    def invalidate(self, key: KT) -> None:
        self.del_multi(key)
    

    
    def _get_multi_impl(self, key: tuple, default=None, update_metrics: bool = True):
        """Returns a generator yielding all entries under the given key prefix.
        
        Can only be used if backed by a tree cache.
        """
        try:
            # Use Rust's efficient prefix lookup
            results = self._rust_cache.get_prefix_children(key)
            if results:
                return results
            return default
        except Exception as e:
            log_debug(f"❌ get_multi failed for key {key}: {e}")
            return default
    
    def contains(self, key: KT) -> bool:
        return key in self._rust_cache
    
    def invalidate_on_extra_index(self, index_key: KT) -> None:
        """Invalidates all entries that match the given extra index key."""
        if not self._extra_index_cb:
            return
            
        keys = self._extra_index.pop(index_key, None)
        if not keys:
            return
            
        for key in keys:
            try:
                self._rust_cache.invalidate(key)
            except Exception as e:
                log_debug(f"❌ Failed to invalidate key {key}: {e}")
    
    def clear(self) -> None:
        try:
            count = self._rust_cache.clear()
            log_info(f"🧹 LruCache.clear({self.cache_name}): cleared {count} entries")
        except Exception as e:
            log_error(f"❌ Rust cache clear failed: {e}")
            raise
    
    def len(self) -> int:
        return len(self._rust_cache)
    
    def __len__(self) -> int:
        return len(self._rust_cache)
    
    def __contains__(self, key: KT) -> bool:
        return key in self._rust_cache
    
    def __getitem__(self, key: KT) -> VT:
        return self._rust_cache[key]
    
    def __setitem__(self, key: KT, value: VT) -> None:
        self.set(key, value)
    
    def __delitem__(self, key: KT) -> None:
        result = self.pop(key, SENTINEL)
        if result is SENTINEL:
            raise KeyError(key)
    
    def set_cache_factor(self, factor: float) -> None:
        if not getattr(self, 'apply_cache_factor_from_config', True):
            return
        new_size = int(getattr(self, '_original_max_size', self.max_size) * factor)
        if new_size != self.max_size:
            self.max_size = new_size
            try:
                self._rust_cache.resize(new_size)
                log_info(f"🔄 Cache {self.cache_name} resized to {new_size}")
            except Exception as e:
                log_error(f"❌ Failed to resize cache {self.cache_name}: {e}")
    
    def get_cache_type(self) -> str:
        """Returns 'RUST' to indicate this is a Rust-backed cache."""
        return "RUST"
    
    def __del__(self) -> None:
        try:
            self.clear()
        except:
            pass

## Must be wondering why? it would help for workers management in future. Event workers if they consists in same base it would be easier to manage.
## Also with multithreading sync caches would be blocking, async caches would be non-blocking simultaneous queues via twisted.
## will think later about it.
class AsyncLruCache(Generic[KT, VT]):
    """An asynchronous wrapper around a subset of the LruCache API.

    Uses true async Rust implementation for non-blocking operations.
    """
    
    def __init__(self, *args: Any, **kwargs: Any):
        # Create async Rust cache directly
        original_name = kwargs.get('cache_name', 'unnamed')
        if 'cache_name' in kwargs and kwargs['cache_name']:
            kwargs['cache_name'] = f"{kwargs['cache_name']}_async"
        
        # Apply cache factor like sync version
        max_size = kwargs.get('max_size', 1000)
        if kwargs.get('apply_cache_factor_from_config', True):
            try:
                from synapse.config import cache as cache_config
                max_size = int(max_size * cache_config.properties.default_factor_size)
            except:
                pass
        kwargs['max_size'] = max_size
        
        # Store properties for compatibility first
        self.max_size = max_size
        self.cache_name = kwargs.get('cache_name')  # This is the modified name with _async suffix
        self.apply_cache_factor_from_config = kwargs.get('apply_cache_factor_from_config', True)
        self._original_max_size = kwargs.get('max_size', 1000)
        self.metrics = kwargs.get('metrics')
        
        # Store parameters for timed eviction patterns
        self._clock = kwargs.get('clock')
        self._prune_unread_entries = kwargs.get('prune_unread_entries', True)
        self._cache_type = kwargs.get('cache_type')
        self._keylen = kwargs.get('keylen', 1)
        self._tree = kwargs.get('tree', False)
        self._extra_index_cb = kwargs.get('extra_index_cb')
        self._extra_index = {}
        
        # Try to create true async Rust cache using global event loop
        try:
            # Try to get current event loop
            import asyncio
            try:
                current_loop = asyncio.get_running_loop()
                self._rust_cache = create_async_rust_lru_cache(max_size, self.cache_name, self.metrics)
                self._global_loop = current_loop
                self._is_async = True
                log_async_info(f"Created true async Rust cache for {self.cache_name} using current loop")
            except RuntimeError:
                # No running loop, try to get event loop
                try:
                    current_loop = asyncio.get_event_loop()
                    self._rust_cache = create_async_rust_lru_cache(max_size, self.cache_name, self.metrics)
                    self._global_loop = current_loop
                    self._is_async = True
                    log_async_info(f"Created true async Rust cache for {self.cache_name} using event loop")
                except Exception:
                    raise RuntimeError("No asyncio event loop available")
        except (RuntimeError, ImportError, AttributeError) as e:
            # Fallback to sync cache with fake async wrapper
            filtered_kwargs = {k: v for k, v in kwargs.items() if k not in ['cache_name', 'max_size']}
            self._lru_cache = LruCache(max_size=max_size, cache_name=self.cache_name, **filtered_kwargs)
            self._is_async = False
            log_async_info(f"Fallback to sync cache for {self.cache_name}: {e}")
        
        # Add TreeCache and CacheWrapper support like sync cache
        if self._is_async:
            # Auto-detect TreeCache mode
            from synapse.util.caches.treecache import TreeCache as PyTreeCache
            self._tree = self._tree or (self._cache_type is PyTreeCache) or self._keylen > 1
            
            # Expose cache attribute like sync cache
            if self._tree:
                self.cache = TreeCache(self._rust_cache)
                # Add get_multi method for TreeCache
                self.get_multi = self._get_multi_impl
            else:
                self.cache = CacheWrapper(self._rust_cache, self._clock, self._prune_unread_entries)
        else:
            # Fallback uses sync cache which already has these patterns
            self.cache = self._lru_cache.cache
            if hasattr(self._lru_cache, 'get_multi'):
                self.get_multi = self._lru_cache.get_multi
        
        # Register with Synapse's cleanup system like sync cache
        server_name = kwargs.get('server_name')
        if self.cache_name and server_name:
            try:
                from synapse.util.caches import register_cache
                self.metrics = register_cache(
                    cache_type="async_rust_lru_cache",
                    cache_name=self.cache_name,
                    cache=self,
                    server_name=server_name,
                    collect_callback=kwargs.get('metrics_collection_callback'),
                )
                log_info(f"✅ AsyncLruCache '{self.cache_name}' registered with cleanup system")
            except Exception as e:
                log_error(f"❌ Failed to register AsyncLruCache '{self.cache_name}' with cleanup system: {e}")
                self.metrics = MockMetrics()
        
        # Add to global cleanup list like sync cache
        if self._prune_unread_entries:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT, _TimedListNode
                from synapse.util import Clock
                from twisted.internet import reactor
                
                class AsyncCacheEntry:
                    def __init__(self, cache_instance):
                        self.cache = cache_instance
                    def drop_from_cache(self):
                        try:
                            self.cache.clear()
                            log_async_info(f"🧹 Global cleanup cleared async cache '{self.cache.cache_name}'")
                        except Exception as e:
                            log_error(f"❌ Global cleanup failed for async cache '{self.cache.cache_name}': {e}")
                
                cache_entry = AsyncCacheEntry(self)
                self._cleanup_node = _TimedListNode.insert_after(cache_entry, GLOBAL_ROOT)
                self._cleanup_node.update_last_access(Clock(reactor))
                log_info(f"✅ AsyncLruCache '{self.cache_name}' added to global cleanup list")
            except Exception as e:
                log_error(f"❌ Failed to add AsyncLruCache '{self.cache_name}' to global cleanup list: {e}")
        
        log_async_info(f"AsyncLruCache({original_name}) -> {self.cache_name} initialized with true async Rust")
    
    def invalidate(self, key: KT) -> None:
        """Sync version of invalidate for compatibility"""
        self.del_multi(key)
    
    async def _async_invalidate(self, key: KT) -> None:
        """Internal async invalidate method"""
        try:
            rust_result = self._rust_cache.invalidate(key)
            future = asyncio.ensure_future(rust_result)
            deferred = defer.Deferred.fromFuture(future)
            await deferred
        except Exception as e:
            log_error(f"AsyncLruCache._async_invalidate error: {e}")
            raise
    
    def get_cache_type(self) -> str:
        """Returns cache type: 'RUST_ASYNC' for true async, 'RUST_SYNC' for fallback."""
        return "RUST_ASYNC" if self._is_async else "RUST_SYNC"
    
    def len(self) -> int:
        if self._is_async:
            return self.max_size  # Approximation for async cache
        else:
            return self._lru_cache.len()
    
    def __len__(self) -> int:
        if self._is_async:
            return self.len()
        else:
            return len(self._lru_cache)
    
    def set_cache_factor(self, factor: float) -> None:
        if not self.apply_cache_factor_from_config:
            return
        new_size = int(self._original_max_size * factor)
        if new_size != self.max_size:
            self.max_size = new_size
            if not self._is_async:
                self._lru_cache.set_cache_factor(factor)
            log_async_info(f"Cache {self.cache_name} factor set to {factor} (size: {new_size})")
    
    async def _await_rust_result(self, rust_result):
        """Helper to convert asyncio.Future to Twisted Deferred"""
        future = asyncio.ensure_future(rust_result)
        deferred = defer.Deferred.fromFuture(future)
        return await deferred
    
    async def get(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        if LRU_ASYNC_DEBUG:
            log_async_debug(f"get({self.cache_name}): {key}")
        if self._is_async:
            try:
                if LRU_ASYNC_DEBUG:
                    log_async_debug(f"Calling rust_cache.get({key}, SENTINEL)")
                rust_result = self._rust_cache.get(key, SENTINEL)
                result = await self._await_rust_result(rust_result)
                if LRU_ASYNC_DEBUG:
                    log_async_debug(f"Rust GET completed, result={result}, is_sentinel={result is SENTINEL}")
                
                # Return default if cache miss (sentinel returned)
                if result is SENTINEL:
                    result = default
                    if LRU_ASYNC_DEBUG:
                        log_async_debug(f"get({self.cache_name}): ❌ MISS")
                else:
                    # Update access time for time-based eviction if it's a hit
                    if not self._tree and hasattr(self.cache, '__getitem__') and self._clock:
                        try:
                            # This will update the last access time
                            _ = self.cache[key]
                        except KeyError:
                            pass
                    if LRU_ASYNC_DEBUG:
                        log_async_debug(f"get({self.cache_name}): ✅ HIT")
                # Rust cache already handles metrics via record_cache_hit/miss
            except Exception as e:
                if LRU_ASYNC_DEBUG:
                    log_async_debug(f"Exception during await: {e}")
                raise
        else:
            result = self._lru_cache.get(key, default, update_metrics=update_metrics)
            if LRU_ASYNC_DEBUG:
                log_async_debug(f"get({self.cache_name}): {'✅ HIT' if result != default else '❌ MISS'}")
        
        return result
    
    async def get_external(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        log_async_debug(f"get_external({self.cache_name}): {key}")
        # External cache miss should return None, not default
        result = await self.get(key, None, update_metrics)
        log_async_debug(f"get_external({self.cache_name}): {'✅ HIT' if result is not None else '❌ MISS'}")
        return result
    
    def get_local(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        log_async_info(f"get_local({self.cache_name}): {key} [is_async={getattr(self, '_is_async', 'unknown')}]")
        log_async_debug(f"get_local({self.cache_name}): {key}")
        if self._is_async:
            # For async cache, get_local should do actual sync lookup like original AsyncLruCache
            # We need to do a sync call to the async cache - this is tricky but necessary
            try:
                # Try to get current result synchronously if possible
                # This is a limitation - true async cache can't provide sync access
                # So we return default to indicate "not available synchronously"
                log_async_info(f"get_local({self.cache_name}): async cache, no sync access available")
                log_async_debug(f"get_local({self.cache_name}): ❌ MISS (async cache)")
                return default
            except Exception as e:
                log_error(f"get_local({self.cache_name}): error={e}")
                log_async_debug(f"get_local({self.cache_name}): ❌ ERROR")
                return default
        else:
            result = self._lru_cache.get(key, default, update_metrics=update_metrics)
            log_async_debug(f"get_local({self.cache_name}): {'✅ HIT' if result != default else '❌ MISS'}")
            return result
    
    async def set(self, key: KT, value: VT) -> None:
        log_async_debug(f"set({self.cache_name}): {key}")
        if self._is_async:
            try:
                # Handle extra index callback like sync cache
                if self._extra_index_cb:
                    index_key = self._extra_index_cb(key, value)
                    mapped_keys = self._extra_index.setdefault(index_key, set())
                    mapped_keys.add(key)
                
                rust_result = self._rust_cache.set(key, value, [])
                log_async_debug(f"set({self.cache_name}): rust_result type={type(rust_result)}")
                
                # Convert asyncio.Future to Twisted Deferred for Synapse compatibility
                future = asyncio.ensure_future(rust_result)
                deferred = defer.Deferred.fromFuture(future)
                await_result = await deferred
                
                # Notify cache wrapper of new entry for time-based eviction
                if not self._tree and hasattr(self.cache, '__setitem__'):
                    self.cache[key] = value
                
                log_async_debug(f"set({self.cache_name}): ✅ SUCCESS")
            except Exception as e:
                log_async_debug(f"set({self.cache_name}): ❌ ERROR: {e}")
                raise
        else:
            self._lru_cache.set(key, value)
            log_async_debug(f"set({self.cache_name}): ✅ SUCCESS (sync)")
        await self.set_external(key, value)
        log_async_debug(f"set({self.cache_name}): complete")
    
    async def set_external(self, key: KT, value: VT) -> None:
        log_async_debug(f"set_external({self.cache_name}): {key} (noop)")
    
    def set_local(self, key: KT, value: VT) -> None:
        log_async_info(f"set_local({self.cache_name}): {key} [is_async={getattr(self, '_is_async', 'unknown')}]")
        log_async_debug(f"set_local({self.cache_name}): {key}")
        if self._is_async:
            # Sync wrapper for async set() - fire and forget from reactor thread
            try:
                # Create the async operation and fire it
                async_op = self.set(key, value)
                d = defer.ensureDeferred(async_op)
                
                # Add error handler but don't wait for completion
                d.addErrback(lambda f: log_error(f"AsyncLruCache.set_local error: {f.value}"))
                log_async_info(f"set_local({self.cache_name}): async set fired")
                log_async_debug(f"set_local({self.cache_name}): ✅ SUCCESS (async fired)")
            except Exception as e:
                log_error(f"set_local({self.cache_name}): error={e}")
                log_async_debug(f"set_local({self.cache_name}): ❌ ERROR")
        else:
            self._lru_cache.set(key, value)
            log_async_debug(f"set_local({self.cache_name}): ✅ SUCCESS (sync)")
    
    async def _async_invalidate(self, key: KT) -> None:
        """Internal async invalidate method"""
        log_async_debug(f"_async_invalidate({self.cache_name}): {key}")
        if self._is_async:
            try:
                # Handle extra index cleanup
                if self._extra_index_cb:
                    try:
                        # Get value for extra index cleanup - use sync check first
                        if hasattr(self._rust_cache, '__contains__') and key in self._rust_cache:
                            rust_result = self._rust_cache.get(key, None)
                            value = await self._await_rust_result(rust_result)
                            if value is not None:
                                index_key = self._extra_index_cb(key, value)
                                mapped_keys = self._extra_index.get(index_key)
                                if mapped_keys:
                                    mapped_keys.discard(key)
                                    if not mapped_keys:
                                        self._extra_index.pop(index_key, None)
                    except Exception:
                        pass  # Ignore errors in cleanup
                
                rust_result = self._rust_cache.invalidate(key)
                
                # Convert asyncio.Future to Twisted Deferred for Synapse compatibility
                future = asyncio.ensure_future(rust_result)
                deferred = defer.Deferred.fromFuture(future)
                await_result = await deferred
                log_async_debug(f"_async_invalidate({self.cache_name}): ✅ SUCCESS")
            except Exception as e:
                log_async_debug(f"_async_invalidate({self.cache_name}): ❌ ERROR: {e}")
                raise
        else:
            self._lru_cache.invalidate(key)
            log_async_debug(f"_async_invalidate({self.cache_name}): ✅ SUCCESS (sync)")
        log_async_debug(f"_async_invalidate({self.cache_name}): complete")
    
    def invalidate_on_extra_index_local(self, index_key: KT) -> None:
        log_async_debug(f"invalidate_on_extra_index_local({self.cache_name}): {index_key}")
        if self._is_async:
            # Handle extra index invalidation like sync cache
            if not self._extra_index_cb:
                return
            keys = self._extra_index.pop(index_key, None)
            if not keys:
                return
            for key in keys:
                try:
                    # Fire async invalidate without waiting
                    async_op = self._async_invalidate(key)
                    d = defer.ensureDeferred(async_op)
                    d.addErrback(lambda f: log_error(f"AsyncLruCache.invalidate_on_extra_index_local error: {f.value}"))
                except Exception as e:
                    log_debug(f"❌ Failed to invalidate key {key}: {e}")
        else:
            self._lru_cache.invalidate_on_extra_index(index_key)
    
    def invalidate_local(self, key: KT) -> None:
        log_async_info(f"invalidate_local({self.cache_name}): {key} [is_async={getattr(self, '_is_async', 'unknown')}]")
        log_async_debug(f"invalidate_local({self.cache_name}): {key}")
        if self._is_async:
            # Sync wrapper for async invalidate() - fire and forget from reactor thread
            try:
                # Create the async operation and fire it
                async_op = self._async_invalidate(key)
                d = defer.ensureDeferred(async_op)
                
                # Add error handler but don't wait for completion
                d.addErrback(lambda f: log_error(f"AsyncLruCache.invalidate_local error: {f.value}"))
                log_async_info(f"invalidate_local({self.cache_name}): async invalidate fired")
                log_async_debug(f"invalidate_local({self.cache_name}): ✅ SUCCESS (async fired)")
            except Exception as e:
                log_error(f"invalidate_local({self.cache_name}): error={e}")
                log_async_debug(f"invalidate_local({self.cache_name}): ❌ ERROR")
        else:
            self._lru_cache.invalidate(key)
            log_async_debug(f"invalidate_local({self.cache_name}): ✅ SUCCESS (sync)")
    
    async def contains(self, key: KT) -> bool:
        """Async version of contains"""
        log_async_debug(f"contains({self.cache_name}): {key}")
        if self._is_async:
            try:
                rust_result = self._rust_cache.get(key, None)
                
                # Convert asyncio.Future to Twisted Deferred for Synapse compatibility
                future = asyncio.ensure_future(rust_result)
                deferred = defer.Deferred.fromFuture(future)
                result = await deferred
                exists = result is not None
                log_async_debug(f"contains({self.cache_name}): {'✅ FOUND' if exists else '❌ NOT_FOUND'}")
            except Exception as e:
                log_async_debug(f"contains({self.cache_name}): ❌ ERROR: {e}")
                raise
        else:
            exists = self._lru_cache.contains(key)
            log_async_debug(f"contains({self.cache_name}): {'✅ FOUND' if exists else '❌ NOT_FOUND'} (sync)")
        return exists
    
    def del_multi(self, key: KT) -> None:
        log_async_debug(f"del_multi({self.cache_name}): {key}")
        if self._is_async:
            try:
                if self._tree and isinstance(key, tuple):
                    # For TreeCache mode, use prefix deletion
                    async_op = self._async_invalidate_prefix(key)
                else:
                    # Regular single key deletion
                    async_op = self._async_invalidate(key)
                d = defer.ensureDeferred(async_op)
                d.addErrback(lambda f: log_error(f"AsyncLruCache.del_multi error: {f.value}"))
                log_async_debug(f"del_multi({self.cache_name}): ✅ SUCCESS (async fired)")
            except Exception as e:
                log_async_debug(f"❌ Async cache del_multi failed for key {key}: {e}")
        else:
            self._lru_cache.del_multi(key)
    
    async def _async_invalidate_prefix(self, key: tuple) -> None:
        """Internal async prefix invalidation for TreeCache"""
        try:
            rust_result = self._rust_cache.invalidate_prefix(key)
            future = asyncio.ensure_future(rust_result)
            deferred = defer.Deferred.fromFuture(future)
            await deferred
        except Exception as e:
            log_error(f"AsyncLruCache._async_invalidate_prefix error: {e}")
            raise
    
    def _get_multi_impl(self, key: tuple, default=None, update_metrics: bool = True):
        """Returns a generator yielding all entries under the given key prefix.
        
        Can only be used if backed by a tree cache.
        """
        if self._is_async:
            try:
                # For async cache, we can't easily provide sync access to async results
                # Return default to indicate "not available synchronously"
                log_async_debug(f"get_multi({self.cache_name}): async cache, no sync access available")
                return default
            except Exception as e:
                log_debug(f"❌ get_multi failed for key {key}: {e}")
                return default
        else:
            return self._lru_cache._get_multi_impl(key, default, update_metrics)
    
    def contains_local(self, key: KT) -> bool:
        """Sync version of contains for compatibility"""
        if self._is_async:
            # For sync access to async cache, return False (not available)
            return False
        else:
            return self._lru_cache.contains(key)
    
    def clear(self) -> None:
        log_info(f"🧹 AsyncLruCache.clear({self.cache_name}) called")
        log_async_debug(f"clear({self.cache_name})")
        if self._is_async:
            # Clear extra index
            self._extra_index.clear()
            
            # Try async clear first, fallback to sync if not in async context
            try:
                loop = asyncio.get_running_loop()
                # We're in async context - fire async clear and don't wait
                async_op = self._async_clear()
                d = defer.ensureDeferred(async_op)
                d.addErrback(lambda f: log_error(f"AsyncLruCache.clear error: {f.value}"))
                log_info(f"🧹 AsyncLruCache.clear({self.cache_name}): async clear fired")
            except RuntimeError:
                # No running loop, use sync clear with retries
                count = self._rust_cache.clear_sync()
                log_info(f"🧹 AsyncLruCache.clear({self.cache_name}): sync clear cleared {count} entries")
            except AttributeError:
                log_info(f"🧹 AsyncLruCache.clear({self.cache_name}): clear_sync method not available")
        else:
            self._lru_cache.clear()
            log_info(f"🧹 AsyncLruCache.clear({self.cache_name}): sync fallback cache cleared")
    
    async def _async_clear(self) -> None:
        """Internal async clear method"""
        try:
            rust_result = self._rust_cache.clear()
            future = asyncio.ensure_future(rust_result)
            deferred = defer.Deferred.fromFuture(future)
            count = await deferred
            log_info(f"🧹 AsyncLruCache._async_clear({self.cache_name}): cleared {count} entries")
        except Exception as e:
            log_error(f"🧹 AsyncLruCache._async_clear({self.cache_name}): error={e}")
            raise


class DebugAsyncLruCache(AsyncLruCache[KT, VT]):
    """Debug async wrapper with race condition logging"""
    
    def __init__(self, *args: Any, **kwargs: Any):
        # Create independent cache with debug suffix (before base class modifies it)
        original_cache_name = kwargs.get('cache_name')
        if original_cache_name:
            kwargs['cache_name'] = f"{original_cache_name}_debug"
        super().__init__(*args, **kwargs)
        
        self._operation_count = 0
        self._lock = threading.Lock()
        log_async_info(f"DebugAsyncLruCache({self.cache_name}) with race detection ready")
        # Force a test operation to verify logging works
        if LRU_ASYNC_DEBUG:
            log_async_info(f"🧪 Testing DebugAsyncLruCache logging for {self.cache_name}")
    
    def _log_operation(self, op: str, key=None):
        if not LRU_ASYNC_DEBUG:
            return
            
        with self._lock:
            self._operation_count += 1
            count = self._operation_count
        
        thread_info = f"thread={threading.current_thread().ident}"
        try:
            task_info = f"task={id(asyncio.current_task()) if asyncio.current_task() else 'None'}"
        except RuntimeError:
            task_info = "task=NoLoop"
        key_info = f"key={key}" if key is not None else ""
        
        log_async_info(f"🔧 [{count:04d}] {op} {key_info} {thread_info} {task_info}")
    
    async def get(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        self._log_operation("GET_START", key)
        try:
            result = await super().get(key, default, update_metrics)
            hit_miss = "✅ HIT" if result != default else "❌ MISS"
            self._log_operation(f"GET_END {hit_miss}", key)
            return result
        except Exception as e:
            self._log_operation(f"GET_ERROR: {e}", key)
            raise
    
    async def get_external(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        self._log_operation("GET_EXT_START", key)
        result = await super().get_external(key, default, update_metrics)
        hit_miss = "✅ HIT" if result is not None else "❌ MISS"
        self._log_operation(f"GET_EXT_END {hit_miss}", key)
        return result
    
    def get_local(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        self._log_operation("GET_LOCAL_START", key)
        result = super().get_local(key, default, update_metrics)
        hit_miss = "✅ HIT" if result != default else "❌ MISS"
        self._log_operation(f"GET_LOCAL_END {hit_miss}", key)
        return result
    
    async def set(self, key: KT, value: VT) -> None:
        self._log_operation("SET_START", key)
        try:
            await super().set(key, value)
            self._log_operation("SET_END ✅ SUCCESS", key)
        except Exception as e:
            self._log_operation(f"SET_ERROR ❌: {e}", key)
            raise
    
    def set_local(self, key: KT, value: VT) -> None:
        self._log_operation("SET_LOCAL_START", key)
        try:
            super().set_local(key, value)
            self._log_operation("SET_LOCAL_END ✅ SUCCESS", key)
        except Exception as e:
            self._log_operation(f"SET_LOCAL_ERROR ❌: {e}", key)
            raise
    
    async def invalidate(self, key: KT) -> None:
        self._log_operation("INVALIDATE_START", key)
        try:
            await super().invalidate(key)
            self._log_operation("INVALIDATE_END ✅ SUCCESS", key)
        except Exception as e:
            self._log_operation(f"INVALIDATE_ERROR ❌: {e}", key)
            raise
    
    def invalidate_local(self, key: KT) -> None:
        self._log_operation("INVALIDATE_LOCAL_START", key)
        try:
            super().invalidate_local(key)
            self._log_operation("INVALIDATE_LOCAL_END ✅ SUCCESS", key)
        except Exception as e:
            self._log_operation(f"INVALIDATE_LOCAL_ERROR ❌: {e}", key)
            raise
    
    def invalidate_on_extra_index_local(self, index_key: KT) -> None:
        self._log_operation("INVALIDATE_IDX", index_key)
        return super().invalidate_on_extra_index_local(index_key)
    
    async def contains(self, key: KT) -> bool:
        self._log_operation("CONTAINS_START", key)
        try:
            result = await super().contains(key)
            found_status = "✅ FOUND" if result else "❌ NOT_FOUND"
            self._log_operation(f"CONTAINS_END {found_status}", key)
            return result
        except Exception as e:
            self._log_operation(f"CONTAINS_ERROR ❌: {e}", key)
            raise
    
    def get_cache_type(self) -> str:
        """Returns cache type with debug suffix."""
        base_type = super().get_cache_type()
        return f"{base_type}_DEBUG"
    
    def clear(self) -> None:
        self._log_operation("CLEAR")
        super().clear()