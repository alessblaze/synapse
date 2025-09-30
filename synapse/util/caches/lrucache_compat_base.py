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
        
        # Create local sync Rust cache for sync operations
        from matrices_evolved.rust import RustLruCache
        self._sync_rust_cache = RustLruCache(max_size, f"{self.cache_name}_sync", self.metrics)
        
        # Create external async Rust cache for async operations
        from matrices_evolved.rust import AsyncRustLruCache
        import asyncio
        try:
            current_loop = asyncio.get_running_loop()
            self._async_rust_cache = AsyncRustLruCache(max_size, f"{self.cache_name}_async", self.metrics)
            self._global_loop = current_loop
            self._is_async = True
        except RuntimeError:
            try:
                current_loop = asyncio.get_event_loop()
                self._async_rust_cache = AsyncRustLruCache(max_size, f"{self.cache_name}_async", self.metrics)
                self._global_loop = current_loop
                self._is_async = True
            except Exception:
                raise RuntimeError("No asyncio event loop available")
        
        # Create cache wrapper for global integration like sync version
        if self._tree:
            self.cache = TreeCache(self._sync_rust_cache)
        else:
            self.cache = CacheWrapper(self._sync_rust_cache, self._clock, self._prune_unread_entries)
        
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
                            count = self.cache.clear()
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
    
    async def get(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        result = self._sync_rust_cache.get(key, default)
        # Update access time for global cleanup like sync version
        if result != default and not self._tree and hasattr(self.cache, '__getitem__'):
            try:
                _ = self.cache[key]  # This updates last access time
            except KeyError:
                pass
        return result
    
    async def get_external(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        if self._is_async and self._async_rust_cache:
            try:
                rust_result = self._async_rust_cache.get(key, SENTINEL)
                result = await self._await_rust_result(rust_result)
                return result if result is not SENTINEL else None
            except Exception:
                return None
        return None
    
    def get_local(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        return self._sync_rust_cache.get(key, default)
    
    async def set(self, key: KT, value: VT) -> None:
        # This will add the entries in the correct order, local first external second
        self.set_local(key, value)
        await self.set_external(key, value)
    
    async def set_external(self, key: KT, value: VT) -> None:
        if self._is_async and self._async_rust_cache:
            try:
                rust_result = self._async_rust_cache.set(key, value, [])
                await self._await_rust_result(rust_result)
            except Exception:
                pass
    
    def set_local(self, key: KT, value: VT) -> None:
        self._sync_rust_cache.set(key, value, [])
        # Notify cache wrapper for global integration
        if not self._tree and hasattr(self.cache, '__setitem__'):
            self.cache[key] = value
    
    def invalidate_local(self, key: KT) -> None:
        """Remove an entry from the local cache

        This variant of `invalidate` is useful if we know that the external
        cache has already been invalidated.
        """
        return self._sync_rust_cache.invalidate(key)
    
    def clear(self) -> None:
        self._sync_rust_cache.clear()
       
    async def invalidate(self, key: KT) -> None:
        # This method should invalidate any external cache and then invalidate the LruCache.
        return self._sync_rust_cache.invalidate(key)
    async def _await_rust_result(self, rust_result):
        """Helper to convert asyncio.Future to Twisted Deferred"""
        future = asyncio.ensure_future(rust_result)
        deferred = defer.Deferred.fromFuture(future)
        return await deferred    
    def invalidate_on_extra_index_local(self, index_key: KT) -> None:
        if not self._extra_index_cb:
            return
        keys = self._extra_index.pop(index_key, None)
        if not keys:
            return
        for key in keys:
            self._sync_rust_cache.invalidate(key)    
    async def contains(self, key: KT) -> bool:
        return key in self._sync_rust_cache
    

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