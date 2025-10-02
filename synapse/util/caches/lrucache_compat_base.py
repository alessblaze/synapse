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
import logging
import os
import asyncio
import threading
from typing import Any, Callable, Collection, Generic, Optional, TypeVar, Union, overload
from matrices_evolved.rust import (
    create_rust_lru_cache, 
    RustLruCache,
    AsyncRustLruCache
)
from twisted.internet import defer
from synapse.config import cache as cache_config
from synapse.util.caches import register_cache

from synapse.util.caches.treecache import TreeCache as PyTreeCache


# Module-level sentinel for cache miss detection - use identity checks per PEP 661
_SENTINEL = object()
_MISS = object()

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

class CacheNodeWrapper:
    """Lightweight wrapper around RustCacheNode for global eviction integration"""
    def __init__(self, rust_node, clock=None, prune_unread_entries=True):
        self._rust_node = rust_node
        self._global_list_node = None
        
        # Add to global time-based eviction list if enabled
        # Nested import to avoid initialization race conditions
        if prune_unread_entries:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT, _TimedListNode, USE_GLOBAL_LIST
                if USE_GLOBAL_LIST and clock:
                    # Create _TimedListNode that points back to this wrapper
                    self._global_list_node = _TimedListNode.insert_after(self, GLOBAL_ROOT)
                    
                    # Use Python clock time for compatibility with global eviction
                    python_clock_time = clock.time() if clock else 0
                    self._global_list_node.last_access_ts_secs = python_clock_time
            except Exception as e:
                if LRU_DEBUG:
                    logger.warning(f"Failed to add cache node to global time-based eviction list: {e}")
    
    def get_cache_entry(self):
        """Required by Synapse's global eviction system - returns self as the cache entry."""
        return self
    
    @property
    def key(self):
        """Get key from Rust node."""
        return self._rust_node.key
    
    @property
    def value(self):
        """Get value from Rust node."""
        result = self._rust_node.value
        return result if result is not None else None
    
    def get_cache_entry(self):
        """Return self for compatibility."""
        return self
    
    def add_callbacks(self, callbacks):
        """Add callbacks to Rust node."""
        if callbacks:
            self._rust_node.add_callbacks(list(callbacks))
    
    def run_and_clear_callbacks(self):
        """Run callbacks in Rust node."""
        self._rust_node.run_and_clear_callbacks()
    
    def drop_from_cache(self):
        """Remove from cache via Rust node - called by Synapse's global eviction system."""
        try:
            # Log timestamp comparison during eviction
            python_last_access = getattr(self._global_list_node, 'last_access_ts_secs', 'NOT_SET') if self._global_list_node else 'NO_GLOBAL_NODE'
            rust_access_time_ms = self._rust_node.get_last_access_time()
            rust_access_time_secs = rust_access_time_ms / 1000
            logger.info(f"[TIMESTAMP_DEBUG] Eviction for key {self.key}:")
            logger.info(f"  Python last_access_ts_secs: {python_last_access}")
            logger.info(f"  Rust last_access_time: {rust_access_time_ms} ms = {rust_access_time_secs} seconds")
            logger.info(f"  Difference: Python - Rust = {python_last_access - rust_access_time_secs if isinstance(python_last_access, (int, float)) else 'N/A'}")
            
            # This is called by Synapse's _expire_old_entries function
            # We need to remove from both Rust cache and global list
            result = self._rust_node.drop_from_cache()
            
            # Remove from Python global list
            if self._global_list_node:
                self._global_list_node.remove_from_list()
                
            return result
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"Failed to drop from cache: {e}")
            return False
    
    def update_last_access(self, clock):
        """Update access time for both Rust and global eviction."""
        # Update Rust LRU position and time tracking
        self._rust_node.update_last_access(clock)
        
        # Update global list time
        # Nested import to avoid initialization race conditions
        if self._global_list_node:
            try:
                from synapse.util.caches.lrucache import GLOBAL_ROOT
                self._global_list_node.move_after(GLOBAL_ROOT)
                
                # Use Python clock time for compatibility with global eviction
                python_clock_time = clock.time() if clock else 0
                self._global_list_node.last_access_ts_secs = python_clock_time
            except Exception as e:
                if LRU_DEBUG:
                    logger.warning(f"Failed to update cache node last access time: {e}")
    
    def get_last_access_time(self):
        """Get last access time from Rust node."""
        return self._rust_node.get_last_access_time()
    
    def get_creation_time(self):
        """Get creation time from Rust node."""
        return self._rust_node.get_creation_time()
    
    def get_access_count(self):
        """Get access count from Rust node."""
        return self._rust_node.get_access_count()
    
    def is_older_than(self, time_ms):
        """Check if node is older than given time."""
        return self._rust_node.is_older_than(time_ms)
    
    def get_memory_usage(self):
        """Get memory usage from Rust node."""
        return self._rust_node.get_memory_usage()
    
    def is_valid(self):
        """Check if node's key still exists in cache."""
        return self._rust_node.is_valid()
    
    def get_node_id(self):
        """Get unique node ID."""
        return self._rust_node.get_node_id()
    
    def should_evict_based_on_time(self, current_time_ms, max_age_ms):
        """Fast Rust-based time eviction check."""
        return self._rust_node.is_older_than(current_time_ms - max_age_ms)
    
    def get_age_ms(self, current_time_ms):
        """Get age in milliseconds using Rust time tracking."""
        return current_time_ms - self._rust_node.get_last_access_time()
    
    def should_evict_based_on_memory(self, memory_threshold):
        """Check if node should be evicted based on memory usage."""
        try:
            return self._rust_node.get_memory_usage() > memory_threshold
        except:
            return False
    
    def get_eviction_priority(self, current_time_ms):
        """Get eviction priority (higher = evict first) using Rust metrics."""
        age = self.get_age_ms(current_time_ms)
        access_count = self._rust_node.get_access_count()
        memory_usage = self._rust_node.get_memory_usage()
        
        # Simple priority: age * memory / access_count
        # More accessed items have lower priority
        return (age * memory_usage) / max(access_count, 1)

class CacheWrapper(dict):
    def __init__(self, rust_cache, clock=None, prune_unread_entries=True):
        super().__init__()
        self._rust_cache = rust_cache
        self._clock = clock
        self._prune_unread_entries = prune_unread_entries
        self._nodes = {}  # Track CacheNodeWrapper objects
        self._nodes_lock = threading.Lock()  # Thread safety for _nodes access
    
    def get_nodes_for_eviction(self, max_age_ms=None, memory_threshold=None):
        """Get nodes that should be evicted using Rust-based filtering."""
        if not (max_age_ms or memory_threshold):
            return []
        
        current_time = None
        if max_age_ms and self._clock:
            current_time = self._clock.time_msec()
        
        stale = []
        eviction_candidates = []
        
        with self._nodes_lock:
            items = list(self._nodes.items())
        
        for key, node in items:
            if not node.is_valid():
                stale.append(key)
                continue
            
            should_evict = False
            priority = 0
            
            if max_age_ms and current_time:
                if node.should_evict_based_on_time(current_time, max_age_ms):
                    should_evict = True
                    priority = node.get_eviction_priority(current_time)
            
            if memory_threshold and node.should_evict_based_on_memory(memory_threshold):
                should_evict = True
                priority = max(priority, node.get_eviction_priority(current_time or 0))
            
            if should_evict:
                eviction_candidates.append((key, node, priority))
        
        if stale:
            with self._nodes_lock:
                for k in stale:
                    n = self._nodes.pop(k, None)
                    if n and n._global_list_node:
                        try:
                            n._global_list_node.remove_from_list()
                        except Exception:
                            pass
        
        eviction_candidates.sort(key=lambda x: x[2], reverse=True)
        return eviction_candidates
    
    def __getitem__(self, key):
        # Get auto-created node from Rust cache
        rust_node = self._rust_cache.get_node_for_key(key)
        if rust_node is None:
            raise KeyError(key)
        
        # Thread-safe node creation and access
        with self._nodes_lock:
            node = self._nodes.get(key)
            if node is None:
                node = CacheNodeWrapper(rust_node, self._clock, self._prune_unread_entries)
                self._nodes[key] = node
        
        # Update last access time
        if self._clock:
            node.update_last_access(self._clock)
        
        return node
    
    def __setitem__(self, key, value):
        if not self._prune_unread_entries:
            return
        rust_node = self._rust_cache.get_node_for_key(key)
        if not rust_node:
            return
        with self._nodes_lock:
            if key in self._nodes:
                return
            self._nodes[key] = CacheNodeWrapper(rust_node, self._clock, self._prune_unread_entries)
    
    def __contains__(self, key):
        return self._rust_cache.contains(key)
    
    def _cleanup_node(self, key):
        # Thread-safe node cleanup when key is removed from cache
        with self._nodes_lock:
            node = self._nodes.pop(key, None)
            if node and node._global_list_node:
                try:
                    node._global_list_node.remove_from_list()
                except Exception as e:
                    if LRU_DEBUG:
                        logger.warning(f"Failed to remove node from global list: {e}")
    
    def get(self, key, default=None):
        return self._rust_cache.get(key, default)
    
    def pop(self, key, default=None):
        self._cleanup_node(key)
        return self._rust_cache.pop(key, default)
    
    def clear(self):
        with self._nodes_lock:
            for node in self._nodes.values():
                if node._global_list_node:
                    try:
                        node._global_list_node.remove_from_list()
                    except Exception:
                        pass
            self._nodes.clear()
        return self._rust_cache.clear()

class TreeCache:
    def __init__(self, rust_cache):
        self._rust_cache = rust_cache
    
    def get(self, key, default=None):
        try:
            return self._rust_cache.get_with_tuple(key, default)
        except:
            return self._rust_cache.get(key, default)
    
    def set(self, key, value, callbacks=None):
        try:
            return self._rust_cache.set_with_tuple(key, value, callbacks or [])
        except:
            return self._rust_cache.set(key, value, callbacks or [])
    
    def __getitem__(self, key):
        result = self.get(key, None)
        if result is None:
            raise KeyError(key)
        return result
    
    def __setitem__(self, key, value):
        self.set(key, value)
    
    def __contains__(self, key):
        return key in self._rust_cache
    
    def pop(self, key, default=None):
        return self._rust_cache.pop(key, default)

class LruCache(Generic[KT, VT]):
    def __init__(
        self,
        *,
        max_size: int,
        clock: Any,
        server_name: str,
        cache_name: Optional[str] = None,
        cache_type: Any = None,
        size_callback: Optional[Callable[[VT], int]] = None,
        metrics_collection_callback: Optional[Callable[[], None]] = None,
        apply_cache_factor_from_config: bool = True,
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
                factor = cache_config.properties.default_factor_size
                self.max_size = int(max_size * factor)
                if LRU_INFO:
                    logger.info(f"🔧 Cache factor applied: {max_size} * {factor} = {self.max_size}")
            except Exception as e:
                self.max_size = int(max_size)
                if LRU_INFO:
                    logger.info(f"🔧 Cache factor failed: {e}, using original size {self.max_size}")
        else:
            self.max_size = int(max_size)
            if LRU_INFO:
                logger.info(f"🔧 Cache factor disabled, using original size {self.max_size}")
            
        # Store callbacks for compatibility
        self._size_callback = size_callback
        self._extra_index_cb = extra_index_cb
        self._extra_index = {}
        self._clock = clock
        
        # Create metrics if needed
        if cache_name and server_name:
            try:
                self.metrics = register_cache(
                    cache_type="rust_lru_cache",
                    cache_name=cache_name,
                    cache=self,
                    server_name=server_name,
                    collect_callback=metrics_collection_callback,
                )
            except Exception as e:
                if LRU_DEBUG:
                    logger.error(f"❌ Failed to register Rust cache '{cache_name}' with cleanup system: {e}")
                self.metrics = MockMetrics()
        else:
            self.metrics = MockMetrics() if cache_name else None
            
        self._rust_cache = create_rust_lru_cache(self.max_size, cache_name, self.metrics, None, self._size_callback)
        
        # Activate RustCacheNode system for global eviction
        if prune_unread_entries:
            try:
                self._rust_cache.enable_node_tracking()
                if LRU_INFO:
                    logger.info(f"🔗 Enabled node tracking for cache '{cache_name}'")
            except Exception as e:
                if LRU_DEBUG:
                    logger.warning(f"Failed to enable node tracking: {e}")
        
        if LRU_INFO:
            logger.info(f"🏗️ Created Rust LruCache '{cache_name}' with max_size={self.max_size}")
        
        # Auto-detect TreeCache mode
        self._tree = tree or (cache_type is PyTreeCache) or keylen > 1
        
        # Expose cache attribute like original
        if self._tree:
            self.cache = TreeCache(self._rust_cache)
        else:
            self.cache = CacheWrapper(self._rust_cache, clock, prune_unread_entries)
            
        # Add to global cleanup list
        if prune_unread_entries:
            try:
                # Don't add to global cleanup list - let individual cache entries handle it
                # The original LruCache adds individual _Node objects, not the cache itself
                pass
            except Exception as e:
                if LRU_DEBUG:
                    logger.error(f"❌ Failed to add Rust cache '{cache_name}' to global cleanup list: {e}")

    @overload
    def get(self, key: KT, default: None = None, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Optional[VT]: ...

    @overload
    def get(self, key: KT, default: T, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Union[T, VT]: ...

    def get(self, key: KT, default: Optional[T] = None, callbacks: Collection[Callable[[], None]] = (), update_metrics: bool = True, update_last_access: bool = True) -> Union[None, T, VT]:
        if LRU_INFO:
            logger.info(f"🔍 LruCache.get({self.cache_name}): {key}")
        try:
            cb_list = list(callbacks) if callbacks else None
            if not update_metrics or not update_last_access:
                result = self._rust_cache.get_advanced(key, _MISS, cb_list, update_metrics, update_last_access)
            else:
                result = self._rust_cache.get(key, default=_MISS, callbacks=cb_list)
            
            hit = result is not _MISS
            if hit and update_last_access and not self._tree:
                try:
                    _ = self.cache[key]
                except KeyError:
                    pass
            
            if LRU_INFO:
                logger.info(f"LruCache.get({self.cache_name}): {'✅ HIT' if hit else '❌ MISS'}")
            return result if hit else default
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Rust cache get failed for key {key}: {e}")
            raise
    
    def set(self, key: KT, value: VT, callbacks: Collection[Callable[[], None]] = ()) -> None:
        if LRU_INFO:
            logger.info(f"🔍 LruCache.set({self.cache_name}): {key}")
        try:
            # Handle extra index callback
            if self._extra_index_cb:
                index_key = self._extra_index_cb(key, value)
                mapped_keys = self._extra_index.setdefault(index_key, set())
                mapped_keys.add(key)
            
            # Optimize callback handling in hot path
            cb_list = list(callbacks) if callbacks else []
            self._rust_cache.set(key, value, cb_list)
            
            # Notify cache wrapper of new entry for time-based eviction
            if not self._tree:
                self.cache[key] = value  # Create tracking node
            
            if LRU_INFO:
                logger.info(f"✅ LruCache.set({self.cache_name}): stored")
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Rust cache set failed for key {key}: {e}")
            raise
    
    def setdefault(self, key: KT, value: VT) -> VT:
        try:
            return self._rust_cache.setdefault(key, value)
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Rust cache setdefault failed for key {key}: {e}")
            raise
    
    @overload
    def pop(self, key: KT, default: None = None) -> Optional[VT]: ...

    @overload
    def pop(self, key: KT, default: T) -> Union[T, VT]: ...

    def pop(self, key: KT, default: Optional[T] = None) -> Union[None, T, VT]:
        if LRU_INFO:
            logger.info(f"🔍 LruCache.pop({self.cache_name}): {key}")
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
                except Exception as e:
                    logger.warning(f"Failed to cleanup extra index for key {key}: {e}")
            
            result = self._rust_cache.pop(key, default)
            if LRU_INFO:
                logger.info(f"LruCache.pop({self.cache_name}): {'✅ FOUND' if result != default else '❌ NOT_FOUND'}")
            return result
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Rust cache pop failed for key {key}: {e}")
            raise
    
    def del_multi(self, key: KT) -> None:
        if LRU_INFO:
            logger.info(f"🔍 LruCache.del_multi({self.cache_name}): {key}")
        try:
            if self._tree and isinstance(key, tuple):
                # Remove wrappers for prefix
                if hasattr(self.cache, '_nodes_lock'):
                    with self.cache._nodes_lock:
                        for k in [k for k in self.cache._nodes.keys() if isinstance(k, tuple) and k[:len(key)] == key]:
                            self.cache._cleanup_node(k)
                
                # Purge extra-index keys sharing prefix
                for idx in list(self._extra_index.keys()):
                    keys = self._extra_index[idx]
                    for k in list(keys):
                        if isinstance(k, tuple) and k[:len(key)] == key:
                            keys.discard(k)
                    if not keys:
                        self._extra_index.pop(idx, None)
                
                self._rust_cache.invalidate_prefix(key)
            else:
                self._rust_cache.invalidate(key)
            if LRU_INFO:
                logger.info(f"✅ LruCache.del_multi({self.cache_name}): invalidated")
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Rust cache del_multi failed for key {key}: {e}")
            raise
    
    def invalidate(self, key: KT) -> None:
        self.del_multi(key)
    
    def get_multi(self, key: tuple, default=None, update_metrics: bool = True):
        """Returns a generator yielding all entries under the given key prefix.
        
        Can only be used if backed by a tree cache.
        """
        if not self._tree:
            raise ValueError("get_multi can only be used with TreeCache")
        try:
            # Use Rust's efficient prefix lookup
            results = self._rust_cache.get_multi(key)
            if results:
                # Return generator for compatibility with original LruCache
                for item in results:
                    yield item
            else:
                return default
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ get_multi failed for key {key}: {e}")
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
                logger.warning(f"Failed to invalidate key {key}: {e}")
    
    def clear(self) -> None:
        try:
            count = self._rust_cache.clear()
            if LRU_INFO:
                logger.info(f"🧹 LruCache.clear({self.cache_name}): cleared {count} entries")
        except Exception as e:
            if LRU_DEBUG:
                logger.error(f"❌ Rust cache clear failed: {e}")
            raise
    
    def len(self) -> int:
        return len(self._rust_cache)
    
    def capacity(self) -> int:
        """Returns the maximum capacity of the cache."""
        return self.max_size
    
    def __len__(self) -> int:
        return len(self._rust_cache)
    
    def __contains__(self, key: KT) -> bool:
        return key in self._rust_cache
    
    def __getitem__(self, key: KT) -> VT:
        return self._rust_cache[key]
    
    def __setitem__(self, key: KT, value: VT) -> None:
        self.set(key, value)
    
    def __delitem__(self, key: KT) -> None:
        result = self.pop(key, _SENTINEL)
        if result is _SENTINEL:
            raise KeyError(key)
    
    def set_cache_factor(self, factor: float) -> None:
        if not getattr(self, 'apply_cache_factor_from_config', True):
            return
        new_size = int(getattr(self, '_original_max_size', self.max_size) * factor)
        if new_size != self.max_size:
            self.max_size = new_size
            try:
                self._rust_cache.resize(new_size)
                if LRU_INFO:
                    logger.info(f"🔄 Cache {self.cache_name} resized to {new_size}")
            except Exception as e:
                if LRU_DEBUG:
                    logger.error(f"❌ Failed to resize cache {self.cache_name}: {e}")
    
    def _update_memory_metrics(self, size_delta: int):
        """Update memory metrics if TRACK_MEMORY_USAGE is enabled"""
        try:
            from synapse.util.caches import TRACK_MEMORY_USAGE
            if TRACK_MEMORY_USAGE and self.metrics:
                if size_delta > 0:
                    self.metrics.inc_memory_usage(size_delta)
                else:
                    self.metrics.dec_memory_usage(-size_delta)
        except ImportError:
            pass
    
    def get_cache_type(self) -> str:
        """Returns 'RUST' to indicate this is a Rust-backed cache."""
        return "RUST"
    
    def get_memory_usage(self) -> int:
        """Get estimated memory usage in bytes."""
        try:
            return self._rust_cache.get_memory_usage()
        except Exception as e:
            if LRU_DEBUG:
                logger.debug(f"❌ Failed to get memory usage: {e}")
            return 0
    
    def get_cache_stats(self):
        """Get cache statistics (hits, misses, evictions, etc.)"""
        return self._rust_cache.get_cache_stats()
    
    def reset_cache_stats(self):
        """Reset all cache statistics"""
        return self._rust_cache.reset_cache_stats()
    
    def iterate_tree_cache_items(self, prefix_key):
        """Generator yielding (key, value) pairs for TreeCache compatibility"""
        if not self._tree:
            raise ValueError("iterate_tree_cache_items can only be used with TreeCache")
        results = self._rust_cache.get_multi(prefix_key)
        for key, value in results:
            yield key, value
    
    def __del__(self) -> None:
        # Avoid nontrivial work in __del__ - use explicit cleanup() method instead
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
        
        # Store original max_size before applying cache factor
        original_max_size = kwargs.get('max_size', 1000)
        
        # Apply cache factor like sync version
        max_size = original_max_size
        if kwargs.get('apply_cache_factor_from_config', True):
            try:
                max_size = int(original_max_size * cache_config.properties.default_factor_size)
            except Exception as e:
                logger.warning(f"Failed to apply cache factor from config: {e}")
        kwargs['max_size'] = max_size
        
        # Store properties for compatibility first
        self.max_size = max_size
        self.cache_name = kwargs.get('cache_name')  # This is the modified name with _async suffix
        self.apply_cache_factor_from_config = kwargs.get('apply_cache_factor_from_config', True)
        self._original_max_size = original_max_size
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
        self._sync_rust_cache = RustLruCache(max_size, f"{self.cache_name}_sync", self.metrics)
        
        # Activate RustCacheNode system for async cache too
        if self._prune_unread_entries:
            try:
                self._sync_rust_cache.enable_node_tracking()
                if LRU_INFO:
                    logger.info(f"🔗 Enabled node tracking for async cache '{self.cache_name}'")
            except Exception as e:
                if LRU_DEBUG:
                    logger.warning(f"Failed to enable node tracking for async cache: {e}")
        
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
            except Exception as e:
                logger.error(f"Failed to create async event loop: {e}")
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
                # Don't add to global cleanup list - let individual cache entries handle it
                # The original LruCache adds individual _Node objects, not the cache itself
                pass
                log_info(f"✅ AsyncLruCache '{self.cache_name}' added to global cleanup list")
            except Exception as e:
                log_error(f"❌ Failed to add AsyncLruCache '{self.cache_name}' to global cleanup list: {e}")
        
        log_async_info(f"AsyncLruCache({original_name}) -> {self.cache_name} initialized with true async Rust")
    
    async def get(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        result = self._sync_rust_cache.get(key, default)
        # Update access time for global cleanup like sync version
        if result != default and not self._tree:
            try:
                _ = self.cache[key]  # This updates last access time
            except KeyError:
                pass
        return result
    
    async def get_external(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        if self._is_async and self._async_rust_cache:
            try:
                rust_result = self._async_rust_cache.get(key, _SENTINEL)
                result = await self._await_rust_result(rust_result)
                return result if result is not _SENTINEL else None
            except Exception as e:
                logger.warning(f"Failed to get external cache value for key {key}: {e}")
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
            except Exception as e:
                logger.warning(f"Failed to set external cache value for key {key}: {e}")
    
    def set_local(self, key: KT, value: VT) -> None:
        self._sync_rust_cache.set(key, value, [])
        # Notify cache wrapper for global integration
        if not self._tree:
            self.cache[key] = None  # Value already stored in Rust cache
    
    def invalidate_local(self, key: KT) -> None:
        """Remove an entry from the local cache

        This variant of `invalidate` is useful if we know that the external
        cache has already been invalidated.
        """
        return self._sync_rust_cache.invalidate(key)
    
    def clear(self) -> None:
        self._sync_rust_cache.clear()
        # Clear async cache if available
        if hasattr(self, '_async_rust_cache') and self._async_rust_cache:
            try:
                self._async_rust_cache.clear()
            except Exception as e:
                logger.warning(f"Failed to clear async rust cache: {e}")
       
    async def invalidate(self, key: KT) -> None:
        # This method should invalidate any external cache and then invalidate the LruCache.
        return self._sync_rust_cache.invalidate(key)
    async def _await_rust_result(self, rust_result):
        """Helper to convert asyncio awaitable to Twisted Deferred"""
        task = None
        try:
            # Wrap awaitable into Task before converting to Deferred
            task = asyncio.ensure_future(rust_result)
            deferred = defer.Deferred.fromFuture(task)
            return await deferred
        except Exception as e:
            # Cancel task on error to prevent resource leak
            if task and not task.done():
                task.cancel()
            raise    
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
    
    def set_cache_factor(self, factor: float) -> None:
        if not getattr(self, 'apply_cache_factor_from_config', True):
            return
        new_size = int(getattr(self, '_original_max_size', self.max_size) * factor)
        if new_size != self.max_size:
            self.max_size = new_size
            try:
                self._sync_rust_cache.resize(new_size)
                if self._async_rust_cache:
                    self._async_rust_cache.resize(new_size)
                log_async_info(f"🔄 AsyncCache {self.cache_name} resized to {new_size}")
            except Exception as e:
                log_error(f"❌ Failed to resize async cache {self.cache_name}: {e}")
    
    def get_cache_type(self) -> str:
        return "ASYNC_RUST"
    
    def __len__(self) -> int:
        return len(self._sync_rust_cache)
    
    def __contains__(self, key: KT) -> bool:
        return key in self._sync_rust_cache
    
    def __del__(self) -> None:
        # Avoid nontrivial work in __del__ - use explicit cleanup() method instead
        pass

