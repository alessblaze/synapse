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

"""
Centralized wrapper for LruCache imports.
This module provides a single point of import for all LruCache functionality
used throughout the Synapse codebase.

Function Mapping:
=================

Functions available in matrices_evolved.rust (preferred when available):
- LruCache, AsyncLruCache (optimized Rust implementations)

Fallback behavior:
- If matrices_evolved.rust is not available, falls back to synapse.util.caches.lrucache
- This provides performance benefits when matrices_evolved is installed
"""

# Implementation: Try matrices_evolved.rust first, fallback to synapse lrucache
try:
    from synapse.util.caches.lrucache_compat_base import LruCache, AsyncLruCache
    from synapse.util.caches.lrucache import setup_expire_lru_cache_entries
except ImportError:
    from synapse.util.caches.lrucache import (
        LruCache,
        AsyncLruCache,
        setup_expire_lru_cache_entries,
    )

__all__ = [
    "LruCache",
    "AsyncLruCache", 
    "setup_expire_lru_cache_entries",
]