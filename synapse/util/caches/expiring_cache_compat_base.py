import logging
import os
import time
from typing import Any, Generic, Literal, Optional, TypeVar, Union, overload
from twisted.internet import defer

from synapse.config import cache as cache_config
from synapse.util.caches import EvictionReason, register_cache
from synapse.util.clock import Clock
from synapse.util.clock_compat import Clock as CompatClock
from synapse.util.duration import Duration 
from synapse.util.caches.lrucache_compat_base import LruCache as RustLruCache, _SENTINEL, _MISS

logger = logging.getLogger(__name__)

KT = TypeVar("KT")
VT = TypeVar("VT")
T = TypeVar("T")

SENTINEL: Any = _SENTINEL
MISS: Any = _MISS

# Environment-gated logging functions
EXPIRING_DEBUG = os.environ.get("SYNAPSE_AMS_EXPIRING_CACHE_DEBUG") == "1"
EXPIRING_INFO = os.environ.get("SYNAPSE_AMS_EXPIRING_CACHE_INFO") == "1"

def log_info(msg: str, *args) -> None:
    if EXPIRING_INFO:
        logger.info(msg, *args)

def log_error(msg: str, *args) -> None:
    if EXPIRING_DEBUG:
        logger.error(msg, *args)

def log_debug(msg: str, *args) -> None:
    if EXPIRING_DEBUG:
        logger.debug(msg, *args)

class ExpiringCache(Generic[KT, VT]):
    def __init__(
        self,
        *,
        cache_name: str,
        server_name: str,
        hs: "HomeServer",
        clock: Clock,
        max_len: int = 0,
        expiry_ms: int = 0,
        reset_expiry_on_get: bool = False,
        iterable: bool = False,
    ):
        self._cache_name = cache_name
        self._original_max_size = max_len
        # Apply cache factor like original ExpiringCache (even to max_len=0)
        self._max_size = int(max_len * cache_config.properties.default_factor_size)
        
        # Handle max_len=0 case - original uses 0 which means unlimited
        # But Rust cache needs a positive number, so use large value when result is 0
        if self._max_size == 0:
            self._max_size = 1000000  # 1M entries should be effectively unlimited
        log_debug("ExpiringCache '%s' size calculation: original=%d, factor=%s, final=%d", cache_name, max_len, cache_config.properties.default_factor_size, self._max_size)
        # Use compatibility clock if the passed clock is the original Clock
        if hasattr(clock, '__class__') and clock.__class__.__name__ == 'Clock':
            # Replace with compatibility clock to fix AlreadyCalled errors
            self._clock = CompatClock(clock._reactor, clock._server_name)
        else:
            self._clock = clock
        self._expiry_ms = expiry_ms
        self._reset_expiry_on_get = reset_expiry_on_get
        self.iterable = iterable
        
        # Rust cache handles time tracking with set_with_clock

        # Create Rust-based cache
        log_info("[%s] Creating Rust-based ExpiringCache: max_len=%d, expiry_ms=%d", 
                cache_name, self._max_size, expiry_ms)
        # Use our Rust LruCache as the backend - it has timestamps already
        # For iterable caches, pass size_callback to calculate total size
        size_callback = len if iterable else None
        log_debug("Creating RustLruCache with max_size=%d, cache_name=%s_expiring", self._max_size, cache_name)
        self._rust_cache = RustLruCache(
            max_size=self._max_size,
            cache_name=f"{cache_name}_expiring",
            server_name=server_name,
            clock=self._clock,  # Use our compatibility clock
            size_callback=size_callback
        )
        
        # Enable node tracking for get_all_nodes() to work
        try:
            self._rust_cache._rust_cache.enable_node_tracking()
            log_debug("Enabled node tracking for ExpiringCache '%s'", cache_name)
        except Exception as e:
            log_error("Failed to enable node tracking for ExpiringCache '%s': %s", cache_name, e)
        
        log_debug("Created RustLruCache '%s', capacity=%d, max_size=%d", cache_name, self._rust_cache.capacity(), self._max_size)

        self.metrics = register_cache(
            cache_type="expiring",
            cache_name=cache_name,
            cache=self,
            server_name=server_name,
        )

        # Start background pruning if expiry is enabled
        if self._expiry_ms:
            def f() -> "defer.Deferred[None]":
                return hs.run_as_background_process("prune_cache", self._prune_cache)
            interval = Duration(milliseconds=self._expiry_ms / 2)
            self._clock.looping_call(f, interval)

    def __setitem__(self, key: KT, value: VT) -> None:
        log_debug("__setitem__(%s, %s) - storing in rust cache", key, value)
        log_info("[%s] SET key=%s", self._cache_name, key)
        # Use clock-aware set method if available for proper time tracking
        if hasattr(self._rust_cache, 'set_with_clock'):
            self._rust_cache.set_with_clock(key, value, [], self._clock)
        else:
            self._rust_cache.set(key, value, [])
        # Rust cache tracks creation time via set_with_clock
        # Always verify it was stored for debugging
        verify = self._rust_cache.get(key, default=MISS)
        log_debug("__setitem__(%s, %s) - verification: stored=%s, capacity=%d, len=%d", key, value, verify is not MISS, self._rust_cache.capacity(), len(self._rust_cache))
        self.evict()

    def __getitem__(self, key: KT) -> VT:
        # Use peek to avoid updating LRU order (maintains FIFO eviction)
        result = self._rust_cache.peek(key, default=MISS)
        if result is MISS:
            log_info("[%s] GET key=%s MISS", self._cache_name, key)
            self.metrics.inc_misses()
            raise KeyError(key)
        
        # No expiry checking here - background only like original
        log_info("[%s] GET key=%s HIT", self._cache_name, key)
        self.metrics.inc_hits()
        
        # Reset expiry time if configured (like original)
        if self._reset_expiry_on_get:
            # Re-set the value to reset creation time in Rust cache
            self._rust_cache.set_with_clock(key, result, [], self._clock)
        
        return result

    @overload
    def get(self, key: KT, default: Literal[None] = None) -> Optional[VT]: ...

    @overload
    def get(self, key: KT, default: T) -> Union[VT, T]: ...

    def get(self, key: KT, default: Optional[T] = None) -> Union[VT, Optional[T]]:
        # Use peek to avoid updating LRU order (maintains FIFO eviction)
        result = self._rust_cache.peek(key, default=MISS)
        log_debug("get(%s) - rust_cache.peek returned: %s, MISS check: %s", key, result, result is MISS)
        if result is MISS:
            log_debug("get(%s) - MISS, returning default: %s", key, default)
            log_info("[%s] GET key=%s MISS", self._cache_name, key)
            self.metrics.inc_misses()
            return default
        
        # No expiry checking here - background only like original
        log_debug("get(%s) - HIT, returning: %s", key, result)
        log_info("[%s] GET key=%s HIT", self._cache_name, key)
        self.metrics.inc_hits()
        
        # Reset expiry time if configured (like original)
        if self._reset_expiry_on_get:
            # Re-set the value to reset creation time in Rust cache
            self._rust_cache.set_with_clock(key, result, [], self._clock)
        
        return result
    
    def _is_expired_locked(self, key: KT) -> bool:
        """Check if a key has expired - assumes caller holds _nodes_lock"""
        if not self._expiry_ms:
            return False
            
        try:
            # Direct node access without additional locking (caller has lock)
            if hasattr(self._rust_cache, 'cache') and hasattr(self._rust_cache.cache, '_nodes'):
                node = self._rust_cache.cache._nodes.get(key)
                if node and node.key == key:
                    # Use test clock time for consistency
                    current_time_ms = int(self._clock.time_msec())
                    age_ms = node.get_age_since_creation_ms(current_time_ms)
                    return age_ms > self._expiry_ms
            
            # No fallback needed - Rust cache handles all time tracking
            return False
        except (KeyError, AttributeError):
            return False

    def pop(self, key: KT, default: T = SENTINEL) -> Union[VT, T]:
        result = self._rust_cache.pop(key, SENTINEL)
        if result is not SENTINEL:
            # Rust cache handles cleanup automatically
            log_info("[%s] POP key=%s SUCCESS", self._cache_name, key)
            if self.iterable:
                self.metrics.inc_evictions(EvictionReason.invalidation, len(result))
            else:
                self.metrics.inc_evictions(EvictionReason.invalidation)
            return result
        elif default is not SENTINEL:
            log_info("[%s] POP key=%s DEFAULT", self._cache_name, key)
            return default
        else:
            log_info("[%s] POP key=%s KEYERROR", self._cache_name, key)
            raise KeyError(key)

    def __contains__(self, key: KT) -> bool:
        # Simple check - no expiry validation like original
        return self._rust_cache.contains(key)

    def __len__(self) -> int:
        if self.iterable:
            # Sum sizes of all values for iterable caches
            total_size = 0
            # Use Rust cache to iterate over all keys
            try:
                if hasattr(self._rust_cache, 'cache') and hasattr(self._rust_cache.cache, '_nodes'):
                    with self._rust_cache.cache._nodes_lock:
                        for key in list(self._rust_cache.cache._nodes.keys()):
                            try:
                                value = self._rust_cache[key]
                                total_size += len(value)
                            except (KeyError, TypeError):
                                # Key evicted or value not sized
                                continue
                return total_size
            except:
                # Fallback to regular count
                return self._rust_cache.len()
        else:
            return self._rust_cache.len()

    def clear(self) -> None:
        log_info("[%s] CLEAR", self._cache_name)
        self._rust_cache.clear()
        # Rust cache handles cleanup automatically

    def setdefault(self, key: KT, value: VT) -> VT:
        """Insert key with a value of default if key is not in the cache.
        Return the value at key.
        """
        try:
            return self[key]
        except KeyError:
            self[key] = value
            return value

    def evict(self) -> None:
        """Evict items if cache is over max size using FIFO strategy"""
        # Since we use peek() for access, Rust LRU naturally becomes FIFO
        # Use Rust's efficient get_oldest_key method for O(1) FIFO eviction
        while self._max_size and len(self._rust_cache) > self._max_size:
            try:
                # Get oldest key from Rust cache (O(1) operation)
                oldest_key = self._rust_cache.get_oldest_key()
                if oldest_key is None:
                    break  # No items to evict
                
                # Remove oldest item
                value = self._rust_cache.pop(oldest_key, MISS)
                if value is not MISS:
                    if self.iterable:
                        self.metrics.inc_evictions(EvictionReason.size, len(value))
                    else:
                        self.metrics.inc_evictions(EvictionReason.size)
                    log_info("[%s] FIFO evicted key=%s", self._cache_name, oldest_key)
                else:
                    # Key already gone
                    break
            except Exception as e:
                log_error("[%s] Failed to evict oldest key: %s", self._cache_name, e)
                break

    def _is_expired(self, key: KT) -> bool:
        """Check if a key has expired based on creation time"""
        if not self._expiry_ms:
            log_debug("_is_expired(%s) - no expiry: expiry=%s", key, self._expiry_ms)
            return False
            
        try:
            # Use Rust relative time method for expiry checking
            if hasattr(self._rust_cache, 'cache') and hasattr(self._rust_cache.cache, '_nodes'):
                with self._rust_cache.cache._nodes_lock:
                    node = self._rust_cache.cache._nodes.get(key)
                    if node:
                        log_debug("_is_expired(%s) - Found node, key: %s, value: %s", key, node.key, node.value)
                        
                        # Verify we have the right node
                        if node.key != key:
                            log_debug("ERROR: Node key mismatch! Expected %s, got %s", key, node.key)
                            return False
                        
                        current_reactor_time = self._clock.time_msec()
                        
                        if EXPIRING_DEBUG:
                            current_sys_time = time.time() * 1000
                            creation_abs = node.get_creation_time_absolute()
                            creation_rel = node.get_creation_time()
                            age_abs = node.get_age_since_creation_ms(int(current_sys_time))
                            current_time_ms = int(self._clock.time_msec())
                            age_rel = node.get_age_since_creation_ms(current_time_ms)
                            
                            log_debug("Time debug - sys:%d reactor:%d rust_abs:%d rust_rel:%d age_abs:%d age_rel:%d", 
                                     current_sys_time, current_reactor_time, creation_abs, creation_rel, 
                                     age_abs, age_rel)
                        
                        # Use test clock time for consistency
                        current_time_ms = int(self._clock.time_msec())
                        age_ms = node.get_age_since_creation_ms(current_time_ms)
                        is_expired = age_ms > self._expiry_ms
                        
                        log_debug("Rust expiry check: %s (age=%d > expiry=%d)", is_expired, age_ms, self._expiry_ms)
                        
                        return is_expired
                    else:
                        log_debug("_is_expired(%s) - No node found", key)
            
            # No fallback needed - Rust cache handles all time tracking
            return False
        except (KeyError, AttributeError) as e:
            log_debug("_is_expired(%s) - error: %s", key, e)
            log_error("[%s] Error checking expiry for key %s: %s", self._cache_name, key, e)
            return False

    def _prune_expired_sync(self) -> None:
        """Synchronously prune expired entries using Rust time tracking"""
        if not self._expiry_ms:
            return
            
        current_time_ms = self._clock.time_msec()
        expired_keys = []
        
        # Use Rust cache nodes for expiry checking
        try:
            nodes = self._rust_cache._rust_cache.get_all_nodes()
            for node in nodes:
                if node is not None:
                    # Use test clock time for consistency
                    current_time_ms = int(self._clock.time_msec())
                    age_ms = node.get_age_since_creation_ms(current_time_ms)
                    if age_ms > self._expiry_ms:
                        expired_keys.append(node.key)
        except Exception as e:
            log_debug("_prune_expired_sync failed to get nodes: %s", e)
            return
        
        # Remove expired keys
        for key in expired_keys:
            if self.iterable:
                try:
                    value = self._rust_cache.get(key, default=MISS)
                    if value is not MISS:
                        self.metrics.inc_evictions(EvictionReason.time, len(value))
                    else:
                        self.metrics.inc_evictions(EvictionReason.time)
                except:
                    self.metrics.inc_evictions(EvictionReason.time)
            else:
                self.metrics.inc_evictions(EvictionReason.time)
            self._rust_cache.invalidate(key)
        
        if expired_keys:
            log_info("[%s] Pruned %d expired keys", self._cache_name, len(expired_keys))

    async def _prune_cache(self) -> None:
        """Background task to prune expired entries"""
        if not self._expiry_ms:
            return
            
        log_info("[%s] Running background pruning", self._cache_name)
        self._prune_expired_sync()

    def set_cache_factor(self, factor: float) -> bool:
        """Set the cache factor for this individual cache.
        
        This will trigger a resize if it changes, which may require evicting
        items from the cache.
        
        Returns:
            Whether the cache changed size or not.
        """
        new_size = int(self._original_max_size * factor)
        if new_size != self._max_size:
            log_info("[%s] RESIZE from %d to %d (factor=%.2f)", 
                    self._cache_name, self._max_size, new_size, factor)
            self._max_size = new_size
            self._rust_cache.set_cache_factor(factor)
            return True
        return False
    
    def __delitem__(self, key: KT) -> None:
        """Delete item from cache."""
        result = self._rust_cache.pop(key, SENTINEL)
        if result is SENTINEL:
            raise KeyError(key)
        # Rust cache handles cleanup automatically
    
    def keys(self):
        """Return cache keys."""
        # Get keys from underlying Rust cache using get_all_nodes
        try:
            nodes = self._rust_cache._rust_cache.get_all_nodes()
            return [node.key for node in nodes if node is not None]
        except Exception as e:
            log_debug("keys() failed to get nodes: %s", e)
            # Fallback: return empty list
            return []
    
    def values(self):
        """Return cache values."""
        for key in self.keys():
            try:
                yield self._rust_cache.peek(key, MISS)
            except:
                # Key was evicted or error occurred
                continue
    
    def items(self):
        """Return cache items."""
        for key in self.keys():
            try:
                value = self._rust_cache.peek(key, MISS)
                if value is not MISS:
                    yield (key, value)
            except:
                # Key was evicted or error occurred
                continue
    
    def __iter__(self):
        """Iterate over cache keys."""
        return iter(self.keys())