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
from synapse.config import cache as cache_config
from synapse.metrics.jemalloc import get_jemalloc_stats
from synapse.util.caches import register_cache

from synapse.util.caches.treecache import TreeCache as PyTreeCache

# Use compatibility clock to fix AlreadyCalled errors
from synapse.util.clock_compat import Clock as CompatClock


# Sentinel objects for cache operations - use identity checks per PEP 661
class _SentinelType:
    def __repr__(self):
        return "<SENTINEL>"

class _MissType:
    def __repr__(self):
        return "<CACHE_MISS>"

_SENTINEL = _SentinelType()
_MISS = _MissType()

logger = logging.getLogger(__name__)
LRU_DEBUG = os.environ.get("SYNAPSE_AMS_LRU_DEBUG") == "1"
LRU_INFO = os.environ.get("SYNAPSE_AMS_LRU_INFO") == "1"
LRU_ASYNC_DEBUG = os.environ.get("SYNAPSE_AMS_LRU_ASYNC_DEBUG") == "1"

# Global eviction scheduler for Rust caches
_RUST_CACHE_REGISTRY = []
_EVICTION_SCHEDULER = None
_RUST_CACHE_TTL = 600  # Default 10 minutes TTL

def register_rust_cache_for_eviction(cache):
    """Register cache for periodic eviction"""
    global _RUST_CACHE_REGISTRY, _EVICTION_SCHEDULER
    _RUST_CACHE_REGISTRY.append(cache)
    cache_name = getattr(cache, 'cache_name', 'unnamed')
    if LRU_DEBUG:
        logger.debug(f"🔧 Cache '{cache_name}' registered for eviction (TTL: {_RUST_CACHE_TTL}s, Total: {len(_RUST_CACHE_REGISTRY)})")
    
    # No need for late-start logic since scheduler starts immediately when setup is called

def cleanup_rust_cache_eviction():
    """Global cleanup function to stop eviction scheduler"""
    global _RUST_CACHE_REGISTRY, _EVICTION_SCHEDULER
    _RUST_CACHE_REGISTRY.clear()
    if _EVICTION_SCHEDULER:
        try:
            if hasattr(_EVICTION_SCHEDULER, 'running') and _EVICTION_SCHEDULER.running:
                _EVICTION_SCHEDULER.stop()
            logger.info("🛑 Global cleanup: Stopped Rust eviction scheduler")
        except Exception as e:
            logger.warning(f"Failed to stop eviction scheduler during cleanup: {e}")
        finally:
            _EVICTION_SCHEDULER = None

def setup_expire_lru_cache_entries(hs):
    """Start a background job that expires all cache entries if they have not
    been accessed for the given number of seconds, or if a given memory usage threshold has been
    breached.
    """

    
    if not hs.config.caches.expiry_time_msec and not hs.config.caches.cache_autotuning:
        return

    if hs.config.caches.expiry_time_msec:
        expiry_time = hs.config.caches.expiry_time_msec / 1000
        logger.info("Expiring Rust LRU caches after %d seconds", expiry_time)
    else:
        expiry_time = 600  # Default 10 minutes for Rust caches when not configured
        logger.info("Using fallback TTL for Rust LRU caches: %d seconds (10 minutes)", expiry_time)

    # Pass HomeServer for cache-specific TTL overrides
    setup_rust_cache_eviction(expiry_time, hs.get_clock(), hs)

def setup_rust_cache_eviction(ttl_seconds, clock=None, hs=None):
    """Configure TTL and start eviction scheduler with cache-specific overrides"""
    global _RUST_CACHE_TTL, _EVICTION_SCHEDULER
    old_ttl = _RUST_CACHE_TTL
    _RUST_CACHE_TTL = ttl_seconds
    old_display = f"{old_ttl}s" if old_ttl is not None else "not set"
    logger.info(f"🕐 Rust cache eviction TTL updated: {old_display} -> {ttl_seconds}s")
    
    # Update TTL for already registered caches
    if _RUST_CACHE_REGISTRY and old_ttl != ttl_seconds:
        logger.info(f"🔄 Updating TTL for {len(_RUST_CACHE_REGISTRY)} already-registered caches")
    
    # Always start scheduler when called, like original lrucache.py
    if not _EVICTION_SCHEDULER:
        logger.info(f"🚀 Starting eviction scheduler with TTL {ttl_seconds}s (currently {len(_RUST_CACHE_REGISTRY)} caches registered)")
        _start_eviction_scheduler(clock, ttl_seconds, hs)

def deregister_rust_cache_for_eviction(cache):
    """Remove cache from eviction registry to prevent memory leaks"""
    global _RUST_CACHE_REGISTRY, _EVICTION_SCHEDULER
    cache_name = getattr(cache, 'cache_name', 'unnamed')
    old_count = len(_RUST_CACHE_REGISTRY)
    _RUST_CACHE_REGISTRY[:] = [c for c in _RUST_CACHE_REGISTRY if c is not cache]
    new_count = len(_RUST_CACHE_REGISTRY)
    
    if old_count != new_count:
        logger.info(f"🗑️ Cache '{cache_name}' deregistered from eviction (remaining: {new_count})")
    
    # Stop scheduler when no caches remain to avoid idle LoopingCall
    if not _RUST_CACHE_REGISTRY and _EVICTION_SCHEDULER:
        try:
            if hasattr(_EVICTION_SCHEDULER, 'running') and _EVICTION_SCHEDULER.running:
                _EVICTION_SCHEDULER.stop()
            logger.info("🛑 Stopped Rust eviction scheduler (no caches remaining)")
        except Exception as e:
            logger.warning(f"Failed to stop eviction scheduler: {e}")
        finally:
            _EVICTION_SCHEDULER = None

def _expire_rust_cache_entries(clock, expiry_seconds, hs=None):
    """Evict expired entries from all registered Rust caches with cache-specific TTL support"""
    if LRU_DEBUG:
        logger.info(f"🕐 Rust cache eviction tick: checking {len(_RUST_CACHE_REGISTRY)} caches (TTL: {expiry_seconds}s)")
    
    if not expiry_seconds or expiry_seconds <= 0:
        logger.warning(f"⚠️ Eviction skipped: invalid TTL {expiry_seconds}")
        return
    
    # Check for memory-based eviction
    evicting_due_to_memory = False
    autotune_config = None
    if hs and hasattr(hs.config, 'caches') and hs.config.caches.cache_autotuning:
        autotune_config = hs.config.caches.cache_autotuning
        
        try:
            jemalloc_interface = get_jemalloc_stats()
            if jemalloc_interface:
                jemalloc_interface.refresh_stats()
                mem_usage = jemalloc_interface.get_stat("allocated")
                max_cache_memory_usage = autotune_config["max_cache_memory_usage"]
                
                if mem_usage > max_cache_memory_usage:
                    if LRU_DEBUG:
                        logger.info(f"🧠 Memory-based eviction triggered: {mem_usage} > {max_cache_memory_usage}")
                    evicting_due_to_memory = True
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"Failed to check memory usage for eviction: {e}")
        
    total_evicted = 0
    alive_caches = []
    
    for cache in _RUST_CACHE_REGISTRY:
        if hasattr(cache, '_rust_cache'):
            try:
                cache_name = getattr(cache, 'cache_name', None) or 'unnamed'
                
                # Determine eviction threshold
                if evicting_due_to_memory and autotune_config:
                    # Use shorter TTL for memory pressure
                    min_cache_ttl = autotune_config.get("min_cache_ttl", 300000) / 1000  # Convert ms to seconds
                    cache_ttl = min(expiry_seconds, min_cache_ttl)
                else:
                    cache_ttl = expiry_seconds
                
                cache_size_before = len(cache._rust_cache) if hasattr(cache._rust_cache, '__len__') else 0
                evicted = cache._rust_cache.evict_older_than(cache_ttl)
                total_evicted += evicted
                alive_caches.append(cache)
                
                if evicted > 0 and LRU_DEBUG:
                    reason = "memory pressure" if evicting_due_to_memory else "time expiry"
                    logger.info(f"🧹 Cache '{cache_name}': evicted {evicted} entries due to {reason} (size: {cache_size_before} -> {cache_size_before - evicted})")
            except Exception as e:
                cache_name = getattr(cache, 'cache_name', None) or 'unnamed'
                if LRU_DEBUG:
                    logger.warning(f"❌ Failed to evict from cache {cache_name}: {e}")
    
    _RUST_CACHE_REGISTRY[:] = alive_caches
    
    if total_evicted > 0:
        reason = "memory pressure" if evicting_due_to_memory else "time expiry"
        logger.info(f"🧹 Rust cache eviction complete: {total_evicted} entries evicted due to {reason} across {len(_RUST_CACHE_REGISTRY)} caches")

def _start_eviction_scheduler(clock, expiry_seconds, hs=None):
    """Start periodic eviction of expired entries using Rust evict_older_than"""
    global _EVICTION_SCHEDULER
    
    # Use Synapse's clock if available, fallback to Twisted
    if clock:
        _EVICTION_SCHEDULER = clock.looping_call(
            _expire_rust_cache_entries,
            30 * 1000,  # 30 seconds in ms
            clock,
            expiry_seconds,
            hs  # Pass HomeServer for cache-specific TTL lookups
        )
        logger.info(f"🕐 Started Rust eviction scheduler via Synapse clock: {len(_RUST_CACHE_REGISTRY)} caches, TTL {expiry_seconds}s")
    else:
        # Fallback to Twisted with closure
        def evict_expired():
            _expire_rust_cache_entries(None, expiry_seconds, hs)
        
        try:
            from twisted.internet import task
            _EVICTION_SCHEDULER = task.LoopingCall(evict_expired)
            _EVICTION_SCHEDULER.start(30.0)
            logger.info(f"🕐 Started Rust eviction scheduler via Twisted fallback: {len(_RUST_CACHE_REGISTRY)} caches, TTL {expiry_seconds}s")
        except ImportError:
            logger.warning("⚠️ Twisted not available, eviction scheduler disabled")
            _EVICTION_SCHEDULER = None



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
        
        # Global list integration removed - using Rust eviction system
    
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
            # Remove from Rust cache
            result = self._rust_node.drop_from_cache()
            
            # Run callbacks like original implementation
            self.run_and_clear_callbacks()
                
            return result
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"Failed to drop from cache: {e}")
            return False
    
    def update_last_access(self, clock):
        """Update access time for both Rust and global eviction."""
        # Update Rust LRU position and time tracking
        self._rust_node.update_last_access(clock)
        
        # Global list integration removed - using Rust time tracking
    
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
    
    def get_creation_time_absolute(self):
        """Get absolute creation time (epoch milliseconds)."""
        return self._rust_node.get_creation_time_absolute()
    
    def get_creation_time_absolute_seconds(self):
        """Get absolute creation time (epoch seconds)."""
        return self._rust_node.get_creation_time_absolute_seconds()
    
    def get_last_access_time_absolute(self):
        """Get absolute last access time (epoch milliseconds)."""
        return self._rust_node.get_last_access_time_absolute()
    
    def get_last_access_time_absolute_seconds(self):
        """Get absolute last access time (epoch seconds)."""
        return self._rust_node.get_last_access_time_absolute_seconds()
    
    def get_age_since_creation_ms(self, current_time_ms):
        """Get age since creation in milliseconds."""
        return self._rust_node.get_age_since_creation_ms(current_time_ms)
    
    def get_age_since_creation_seconds(self, current_time_seconds):
        """Get age since creation in seconds."""
        return self._rust_node.get_age_since_creation_seconds(current_time_seconds)

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
                    self._nodes.pop(k, None)
        
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
                if LRU_DEBUG:
                    logger.debug(f"🆕 Creating CacheNodeWrapper for key {key}")
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
            if LRU_DEBUG:
                logger.debug(f"🆕 Creating CacheNodeWrapper for key {key} via setitem")
            self._nodes[key] = CacheNodeWrapper(rust_node, self._clock, self._prune_unread_entries)
    
    def __contains__(self, key):
        return self._rust_cache.contains(key)
    
    def _cleanup_node(self, key):
        # Thread-safe node cleanup when key is removed from cache
        with self._nodes_lock:
            self._nodes.pop(key, None)
    
    def get(self, key, default=None):
        return self._rust_cache.get(key, default)
    
    def pop(self, key, default=None):
        self._cleanup_node(key)
        return self._rust_cache.pop(key, default)
    
    def clear(self):
        with self._nodes_lock:
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
                if LRU_DEBUG:
                    logger.debug(f"🔧 Cache factor applied: {max_size} * {factor} = {self.max_size}")
            except Exception as e:
                self.max_size = int(max_size)
                if LRU_DEBUG:
                    logger.debug(f"🔧 Cache factor failed: {e}, using original size {self.max_size}")
        else:
            self.max_size = int(max_size)
            if LRU_DEBUG:
                logger.debug(f"🔧 Cache factor disabled, using original size {self.max_size}")
            
        # Store callbacks for compatibility
        self._size_callback = size_callback
        self._extra_index_cb = extra_index_cb
        self._extra_index = {}
        
        # Use compatibility clock if the passed clock is the original Clock
        if hasattr(clock, '__class__') and clock.__class__.__name__ == 'Clock':
            # Replace with compatibility clock to fix AlreadyCalled errors
            self._clock = CompatClock(clock._reactor, clock._server_name)
        else:
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
                if LRU_DEBUG:
                    logger.debug(f"🔗 Enabled node tracking for cache '{cache_name}'")
            except Exception as e:
                if LRU_DEBUG:
                    logger.warning(f"❌ Failed to enable node tracking for cache '{cache_name}': {e}")
        else:
            if LRU_DEBUG:
                logger.debug(f"⚠️ Node tracking disabled for cache '{cache_name}' (prune_unread_entries=False)")
        
        if LRU_DEBUG:
            logger.debug(f"🏗️ Created Rust LruCache '{cache_name}' with max_size={self.max_size}")
        
        # Auto-detect TreeCache mode
        self._tree = tree or (cache_type is PyTreeCache) or keylen > 1
        
        # Expose cache attribute like original
        if self._tree:
            self.cache = TreeCache(self._rust_cache)
        else:
            self.cache = CacheWrapper(self._rust_cache, self._clock, prune_unread_entries)
            
        # Register for Rust-based eviction
        if prune_unread_entries:
            register_rust_cache_for_eviction(self)
            if LRU_DEBUG:
                ttl_display = f"{_RUST_CACHE_TTL}s" if _RUST_CACHE_TTL is not None else "not set"
                logger.info(f"✅ Cache '{cache_name}' registered for Rust eviction (current TTL: {ttl_display})")

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
            
            # Use identity comparison for sentinel check
            hit = result is not _MISS
            
            if LRU_INFO:
                logger.info(f"LruCache.get({self.cache_name}): {'✅ HIT' if hit else '❌ MISS'}")
            return result if hit else default
        except Exception as e:
            print(f"[CACHE_DEBUG] Rust cache get failed for key {key} in cache {self.cache_name}: {e}")
            if LRU_DEBUG:
                logger.warning(f"❌ Rust cache get failed for key {key}: {e}")
            # Re-raise the exception to maintain original behavior - cache failures should propagate
            raise
    
    def peek(self, key: KT, default: Optional[T] = None) -> Union[None, T, VT]:
        """Get a value without updating LRU position or metrics."""
        try:
            result = self._rust_cache.peek(key, _MISS)
            return result if result is not _MISS else default
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"❌ Rust cache peek failed for key {key}: {e}")
            raise
    
    def get_oldest_key(self) -> Optional[KT]:
        """Get the oldest key in the cache (for FIFO eviction)."""
        try:
            return self._rust_cache.get_oldest_key()
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"❌ Rust cache get_oldest_key failed: {e}")
            return None
    
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
            
            # Use clock-aware set method if available
            if hasattr(self._rust_cache, 'set_with_clock') and self._clock:
                self._rust_cache.set_with_clock(key, value, cb_list, self._clock)
            else:
                self._rust_cache.set(key, value, cb_list)
            
            # Notify cache wrapper of new entry for time-based eviction
            if not self._tree:
                self.cache[key] = value  # Create tracking node
            
            if LRU_INFO:
                logger.info(f"✅ LruCache.set({self.cache_name}): stored")
        except Exception as e:
            print(f"[CACHE_DEBUG] Rust cache set failed for key {key} in cache {self.cache_name}: {e}")
            if LRU_DEBUG:
                logger.warning(f"❌ Rust cache set failed for key {key}: {e}")
            # Re-raise the exception to maintain original behavior
            raise
    
    def setdefault(self, key: KT, value: VT) -> VT:
        try:
            result = self._rust_cache.setdefault(key, value)
            if not self._tree and key not in self.cache._nodes:
                self.cache[key] = None  # Create tracking node
            return result
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
            # CRITICAL FIX: Clean up wrapper node FIRST to detach from global list
            if not self._tree and hasattr(self.cache, '_cleanup_node'):
                self.cache._cleanup_node(key)
            
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
            # DEADLOCK FIX: Snapshot keys under lock for cleanup, then cleanup outside
            keys_to_cleanup = []
            if not self._tree and hasattr(self.cache, '_nodes_lock'):
                with self.cache._nodes_lock:
                    if isinstance(key, tuple):
                        # Prefix match for tuple keys
                        keys_to_cleanup = [k for k in self.cache._nodes.keys() if isinstance(k, tuple) and k[:len(key)] == key]
                    else:
                        # Exact match for single keys
                        if key in self.cache._nodes:
                            keys_to_cleanup = [key]
            
            # Cleanup wrapper nodes outside the lock to avoid deadlock
            for k in keys_to_cleanup:
                self.cache._cleanup_node(k)
            
            # Handle extra index cleanup
            if self._extra_index_cb:
                if isinstance(key, tuple):
                    # Cleanup all extra index entries with matching prefix
                    for idx in list(self._extra_index.keys()):
                        keys = self._extra_index[idx]
                        for k in list(keys):
                            if isinstance(k, tuple) and k[:len(key)] == key:
                                keys.discard(k)
                        if not keys:
                            self._extra_index.pop(idx, None)
                else:
                    # Cleanup single key from extra index
                    if key in self._rust_cache:
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
            
            # Use Rust del_multi method directly - handles both single keys and prefixes
            self._rust_cache.del_multi(key)
            
            if LRU_INFO:
                logger.info(f"✅ LruCache.del_multi({self.cache_name}): invalidated")
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"❌ Rust cache del_multi failed for key {key}: {e}")
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
                # CRITICAL: Clean up wrapper node FIRST to detach from global list
                if not self._tree and hasattr(self.cache, '_cleanup_node'):
                    self.cache._cleanup_node(key)
                
                self._rust_cache.invalidate(key)
            except Exception as e:
                logger.warning(f"Failed to invalidate key {key}: {e}")
    
    def clear(self) -> None:
        try:
            # CRITICAL: Clear wrapper nodes first (detaches all from global list)
            if hasattr(self.cache, 'clear'):
                self.cache.clear()
            
            count = self._rust_cache.clear()
            
            # Clear extra index
            self._extra_index.clear()
            
            # Deregister from eviction scheduler
            deregister_rust_cache_for_eviction(self)
            
            if LRU_DEBUG:
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
                if LRU_DEBUG:
                    logger.debug(f"🔄 Cache {self.cache_name} resized to {new_size}")
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
        # Clear cache on deletion like original Synapse LruCache
        try:
            self.clear()
        except Exception:
            # Ignore exceptions in __del__ as they can't be handled properly
            pass

## Must be wondering why? it would help for workers management in future. Event workers if they consists in same base it would be easier to manage.
## Also with multithreading sync caches would be blocking, async caches would be non-blocking simultaneous queues via twisted.
## will think later about it.
class AsyncLruCache(Generic[KT, VT]):
    """An asynchronous wrapper around a subset of the LruCache API.

    Uses true async Rust implementation for non-blocking operations.
    """
    
    def __init__(self, *args: Any, **kwargs: Any):
        # Store original cache name for consistent cleanup registration
        original_name = kwargs.get('cache_name', 'unnamed')
        
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
        self.cache_name = original_name  # Keep original name for consistent cleanup
        self.apply_cache_factor_from_config = kwargs.get('apply_cache_factor_from_config', True)
        self._original_max_size = original_max_size
        self.metrics = kwargs.get('metrics')
        
        # Store parameters for timed eviction patterns
        clock = kwargs.get('clock')
        # Use compatibility clock if the passed clock is the original Clock
        if hasattr(clock, '__class__') and clock.__class__.__name__ == 'Clock':
            # Replace with compatibility clock to fix AlreadyCalled errors
            self._clock = CompatClock(clock._reactor, clock._server_name)
        else:
            self._clock = clock
        
        # Store reference to sync cache for eviction
        self._rust_cache = None  # Will be set after sync cache creation
        self._prune_unread_entries = kwargs.get('prune_unread_entries', True)
        self._cache_type = kwargs.get('cache_type')
        self._keylen = kwargs.get('keylen', 1)
        self._tree = kwargs.get('tree', False)
        self._extra_index_cb = kwargs.get('extra_index_cb')
        self._extra_index = {}
        
        # Register with Synapse's cleanup system first to get metrics object
        server_name = kwargs.get('server_name')
        if self.cache_name and server_name:
            try:
                self.metrics = register_cache(
                    cache_type="rust_lru_cache",
                    cache_name=self.cache_name,
                    cache=self,
                    server_name=server_name,
                    collect_callback=kwargs.get('metrics_collection_callback'),
                )
                logger.info(f"✅ AsyncLruCache '{self.cache_name}' registered with cleanup system")
            except Exception as e:
                logger.error(f"❌ Failed to register AsyncLruCache '{self.cache_name}' with cleanup system: {e}")
                self.metrics = MockMetrics()
        
        # Create local sync Rust cache for sync operations with registered metrics
        self._sync_rust_cache = RustLruCache(max_size, f"{self.cache_name}_sync", self.metrics)
        self._rust_cache = self._sync_rust_cache  # Reference for eviction scheduler
        
        # Activate RustCacheNode system for async cache too
        if self._prune_unread_entries:
            try:
                self._sync_rust_cache.enable_node_tracking()
                logger.info(f"🔗 Enabled node tracking for async cache '{self.cache_name}'")
            except Exception as e:
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
        
        # Register for Rust-based eviction
        if self._prune_unread_entries:
            register_rust_cache_for_eviction(self)
            if LRU_DEBUG:
                ttl_display = f"{_RUST_CACHE_TTL}s" if _RUST_CACHE_TTL is not None else "not set"
                logger.info(f"✅ AsyncCache '{self.cache_name}' registered for Rust eviction (current TTL: {ttl_display})")
        
        if LRU_DEBUG:
            logger.info(f"⚡ AsyncLruCache({original_name}) -> {self.cache_name} initialized with true async Rust")
    
    async def get(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        result = self._sync_rust_cache.get(key, default)
        # Update access time for global cleanup like sync version
        if result != default and not self._tree:
            try:
                _ = self.cache[key]  # This updates last access time
            except KeyError:
                pass
        return result
    
    async def peek(self, key: KT, default: Optional[T] = None) -> Optional[VT]:
        """Get a value without updating LRU position or metrics."""
        try:
            result = self._sync_rust_cache.peek(key, _MISS)
            return result if result is not _MISS else default
        except Exception as e:
            if LRU_DEBUG:
                logger.warning(f"❌ Async cache peek failed for key {key}: {e}")
            raise
    
    async def get_external(self, key: KT, default: Optional[T] = None, update_metrics: bool = True) -> Optional[VT]:
        if self._is_async and self._async_rust_cache:
            try:
                rust_result = self._async_rust_cache.get(key, _SENTINEL)
                result = await self._await_rust_result(rust_result)
                # Use identity comparison for sentinel check
                return result if result is not _SENTINEL else default
            except Exception as e:
                logger.debug(f"Failed to get external cache value for key {key}: {e}")
                return default
        return default
    
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
                logger.debug(f"Failed to set external cache value for key {key}: {e}")
    
    def set_local(self, key: KT, value: VT) -> None:
        # Handle extra index callback like sync version
        if self._extra_index_cb:
            index_key = self._extra_index_cb(key, value)
            mapped_keys = self._extra_index.setdefault(index_key, set())
            mapped_keys.add(key)
        
        self._sync_rust_cache.set(key, value, [])
        # Notify cache wrapper for global integration
        if not self._tree:
            self.cache[key] = None  # Value already stored in Rust cache
    
    def invalidate_local(self, key: KT) -> None:
        """Remove an entry from the local cache

        This variant of `invalidate` is useful if we know that the external
        cache has already been invalidated.
        """
        if not self._tree and hasattr(self.cache, '_cleanup_node'):
            self.cache._cleanup_node(key)
        
        return self._sync_rust_cache.invalidate(key)
    
    def clear(self) -> None:
        if hasattr(self.cache, 'clear'):
            self.cache.clear()
        
        self._sync_rust_cache.clear()
        # Clear async cache if available
        if hasattr(self, '_async_rust_cache') and self._async_rust_cache:
            try:
                self._async_rust_cache.clear()
            except Exception as e:
                logger.debug(f"Failed to clear async rust cache: {e}")
        
        # Clear extra index
        self._extra_index.clear()
        
        # Deregister from eviction scheduler
        deregister_rust_cache_for_eviction(self)
        if LRU_DEBUG:
            logger.info(f"🧹 AsyncCache.clear({self.cache_name}): cleared caches")
       
    async def invalidate(self, key: KT) -> None:
        if not self._tree and hasattr(self.cache, '_cleanup_node'):
            self.cache._cleanup_node(key)
        
        # This method should invalidate any external cache and then invalidate the LruCache.
        return self._sync_rust_cache.invalidate(key)
    async def _await_rust_result(self, rust_result):
        """Helper to await asyncio result directly"""
        try:
            # Directly await the asyncio result - no need for Twisted conversion
            return await rust_result
        except Exception as e:
            # Let the exception propagate naturally
            raise    
    def invalidate_on_extra_index_local(self, index_key: KT) -> None:
        if not self._extra_index_cb:
            return
        keys = self._extra_index.pop(index_key, None)
        if not keys:
            return
        for key in keys:
            if not self._tree and hasattr(self.cache, '_cleanup_node'):
                self.cache._cleanup_node(key)
            
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
                logger.info(f"🔄 AsyncCache {self.cache_name} resized to {new_size}")
            except Exception as e:
                logger.error(f"❌ Failed to resize async cache {self.cache_name}: {e}")
    
    def get_cache_type(self) -> str:
        return "ASYNC_RUST"
    
    def __len__(self) -> int:
        return len(self._sync_rust_cache)
    
    def __contains__(self, key: KT) -> bool:
        return key in self._sync_rust_cache
    
    def __del__(self) -> None:
        # Clear cache on deletion like original Synapse LruCache
        try:
            self.clear()
        except Exception:
            # Ignore exceptions in __del__ as they can't be handled properly
            pass


