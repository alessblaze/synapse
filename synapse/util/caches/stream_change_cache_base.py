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
from typing import Collection, Dict, FrozenSet, List, Mapping, Optional, Set, Union
import logging
import os

logger = logging.getLogger(__name__)

# Performance: only enable detailed logging if env var is set (checked once at module load)
DETAILED_LOGGING = os.environ.get('SYNAPSE_AMS_STREAM_CACHE_DEBUG', '').lower() in ('1', 'true', 'yes')

# Try to import Rust implementation, fallback to Python
try:
    from matrices_evolved.rust import RustStreamChangeCache, AllEntitiesChangedResult as RustAllEntitiesChangedResult, CacheMetrics
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False
    if DETAILED_LOGGING:
        logger.warning("Rust StreamChangeCache not available, falling back to Python implementation")

# Re-export the original for fallback
from synapse.util.caches.stream_change_cache import StreamChangeCache as PythonStreamChangeCache
from synapse.util.caches.stream_change_cache import AllEntitiesChangedResult as PythonAllEntitiesChangedResult

# Type alias for entity type
EntityType = str

class AllEntitiesChangedResult:
    """Wrapper to maintain API compatibility."""
    
    def __init__(self, rust_result=None, python_result=None):
        self._rust_result = rust_result
        self._python_result = python_result
    
    @property
    def hit(self) -> bool:
        if self._rust_result is not None:
            return self._rust_result.hit
        return self._python_result.hit
    
    @property 
    def entities(self) -> List[EntityType]:
        if self._rust_result is not None:
            return self._rust_result.entities  # Rust already returns List[str]
        return self._python_result.entities
    
    @property
    def truncated(self) -> bool:
        if self._rust_result is not None:
            return self._rust_result.truncated
        return False  # Python implementation doesn't support truncation


class StreamChangeCache:
    """
    Drop-in replacement for StreamChangeCache using Rust backend when available.
    
    Maintains 100% API compatibility with the original Python implementation.
    """
    
    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        current_stream_pos: int,
        max_size: int = 10000,
        prefilled_cache: Optional[Mapping[EntityType, int]] = None,
    ) -> None:
        self.name = name
        
        if RUST_AVAILABLE:
            # Use Rust implementation
            prefill_dict = dict(prefilled_cache) if prefilled_cache else None
            self._rust_cache = RustStreamChangeCache(
                name=name,
                server_name=server_name,
                current_stream_pos=current_stream_pos,
                max_size=max_size,
                prefilled_cache=prefill_dict,
            )
            self._python_cache = None
            
            # Register with Synapse's cache system for cleanup and management
            from synapse.util import caches
            
            # Create a wrapper that exposes __len__ for the cache system
            class RustCacheWrapper:
                def __init__(self, rust_cache):
                    self._rust_cache = rust_cache
                
                def __len__(self):
                    return len(self._rust_cache)
            
            self._cache_wrapper = RustCacheWrapper(self._rust_cache)
            self._metrics = caches.register_cache(
                cache_type="cache",
                cache_name=name,
                server_name=server_name,
                cache=self._cache_wrapper,  # Pass wrapper that implements __len__
                resize_callback=self.set_cache_factor,
            )
            
            if DETAILED_LOGGING:
                logger.info(f"✅ Initialized Rust StreamChangeCache '{name}' (max_size={max_size}, prefilled={len(prefill_dict) if prefill_dict else 0})")
        else:
            # Fallback to Python implementation
            self._python_cache = PythonStreamChangeCache(
                name=name,
                server_name=server_name,
                current_stream_pos=current_stream_pos,
                max_size=max_size,
                prefilled_cache=prefilled_cache,
            )
            self._rust_cache = None
            self._metrics = self._python_cache.metrics
            if DETAILED_LOGGING:
                logger.info(f"⚠️ Initialized Python StreamChangeCache '{name}' (Rust not available)")
    
    def set_cache_factor(self, factor: float) -> bool:
        if self._rust_cache is not None:
            try:
                new_size = self._rust_cache.set_cache_factor(factor)
                if DETAILED_LOGGING:
                    logger.info(f"🦀 Rust cache '{self.name}' resized with factor {factor} -> {new_size}")
                return True
            except ValueError:
                return False  # Invalid factor
        changed = self._python_cache.set_cache_factor(factor)
        if changed and DETAILED_LOGGING:
            logger.info(f"🐍 Python cache '{self.name}' resized with factor {factor}")
        return changed
    
    def has_entity_changed(self, entity: EntityType, stream_pos: int) -> bool:
        if self._rust_cache is not None:
            # Get Rust result and metrics before the call
            old_stale = self._rust_cache.get_stale()
            result = self._rust_cache.has_entity_changed(entity, stream_pos)
            new_stale = self._rust_cache.get_stale()
            
            # If Rust detected stale data, translate to Python behavior
            if new_stale > old_stale:
                return True  # Python returns True for stale queries
            
            if DETAILED_LOGGING:
                if not hasattr(self, '_op_count'):
                    self._op_count = 0
                self._op_count += 1
                if self._op_count % 1000 == 0:
                    logger.debug(f"🦀 Rust cache '{self.name}' operations: {self._op_count}")
            return result
        return self._python_cache.has_entity_changed(entity, stream_pos)
    
    def get_entities_changed(
        self, 
        entities: Collection[EntityType], 
        stream_pos: int, 
        _perf_factor: int = 1
    ) -> Union[Set[EntityType], FrozenSet[EntityType]]:
        if self._rust_cache is not None:
            # Convert result from Rust (returns PySet) to Python set
            result = self._rust_cache.get_entities_changed(entities, stream_pos, _perf_factor)
            result_set = set(result) if hasattr(result, '__iter__') else set()
            # Only log if debug enabled and significant operation
            if DETAILED_LOGGING and (len(entities) > 10 or len(result_set) > 0):
                logger.debug(f"🦀 Rust cache '{self.name}' get_entities_changed({len(entities)} entities, {stream_pos}) -> {len(result_set)} changed")
            return result_set
        return self._python_cache.get_entities_changed(entities, stream_pos, _perf_factor)
    
    def has_any_entity_changed(self, stream_pos: int) -> bool:
        if self._rust_cache is not None:
            # Get Rust result and check for stale data
            old_stale = self._rust_cache.get_stale()
            result = self._rust_cache.has_any_entity_changed(stream_pos)
            new_stale = self._rust_cache.get_stale()
            
            # If Rust detected stale data, translate to Python behavior
            if new_stale > old_stale:
                return True  # Python returns True for stale queries
            
            return result
        return self._python_cache.has_any_entity_changed(stream_pos)
    
    def get_all_entities_changed(self, stream_pos: int) -> AllEntitiesChangedResult:
        if self._rust_cache is not None:
            rust_result = self._rust_cache.get_all_entities_changed(stream_pos)
            return AllEntitiesChangedResult(rust_result=rust_result)
        else:
            python_result = self._python_cache.get_all_entities_changed(stream_pos)
            return AllEntitiesChangedResult(python_result=python_result)
    
    def entity_has_changed(self, entity: EntityType, stream_pos: int) -> None:
        if self._rust_cache is not None:
            self._rust_cache.entity_has_changed(entity, stream_pos)
            # Only track updates if debug logging is enabled
            if DETAILED_LOGGING:
                if not hasattr(self, '_update_count'):
                    self._update_count = 0
                self._update_count += 1
                if self._update_count % 100 == 0:
                    logger.debug(f"🦀 Rust cache '{self.name}' updates: {self._update_count}, size: {len(self._rust_cache)}")
        else:
            self._python_cache.entity_has_changed(entity, stream_pos)
    
    def all_entities_changed(self, stream_pos: int) -> None:
        if self._rust_cache is not None:
            self._rust_cache.all_entities_changed(stream_pos)
        else:
            self._python_cache.all_entities_changed(stream_pos)
    
    def get_max_pos_of_last_change(self, entity: EntityType) -> Optional[int]:
        if self._rust_cache is not None:
            return self._rust_cache.get_max_pos_of_last_change(entity)
        return self._python_cache.get_max_pos_of_last_change(entity)
    
    def get_earliest_known_position(self) -> int:
        if self._rust_cache is not None:
            return self._rust_cache.get_earliest_known_position()
        return self._python_cache.get_earliest_known_position()
    
    @property
    def metrics(self):
        """Expose metrics for compatibility with Python implementation."""
        # Return the metrics object registered with Synapse's cache system
        return self._metrics
    
    # Internal attributes for test compatibility
    @property
    def _cache(self):
        """Expose internal cache for test compatibility"""
        if self._rust_cache is not None:
            return self._rust_cache._cache
        return self._python_cache._cache
    
    @property
    def _entity_to_key(self):
        """Expose internal entity mapping for test compatibility"""
        if self._rust_cache is not None:
            return self._rust_cache._entity_to_key
        return self._python_cache._entity_to_key
    
    @property
    def _earliest_known_stream_pos(self):
        """Expose earliest known stream position for test compatibility"""
        if self._rust_cache is not None:
            return self._rust_cache.get_earliest_known_position()
        return self._python_cache._earliest_known_stream_pos
    
    def get_cache_type(self) -> str:
        """Returns cache implementation type for debugging."""
        return "RUST_STREAM_CHANGE" if self._rust_cache is not None else "PYTHON_STREAM_CHANGE"
    
    def log_cache_stats(self) -> None:
        """Log current cache statistics for monitoring."""
        if DETAILED_LOGGING:
            if self._rust_cache is not None:
                # For Rust cache, we'll log basic stats without detailed metrics
                size = len(self._rust_cache)
                earliest_pos = self._rust_cache.get_earliest_known_position()
                logger.info(f"🦀 Rust cache '{self.name}' stats: size={size}, earliest_pos={earliest_pos}")
            else:
                size = len(self._python_cache._cache)
                hits = self._python_cache.metrics.hits
                misses = self._python_cache.metrics.misses
                earliest_pos = self._python_cache.get_earliest_known_position()
                logger.info(f"🐍 Python cache '{self.name}' stats: size={size}, hits={hits}, misses={misses}, earliest_pos={earliest_pos}")