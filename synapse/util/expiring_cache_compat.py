"""
ExpiringCache compatibility wrapper that tries Rust implementation first,
falls back to standard Python ExpiringCache if Rust is unavailable.
"""

try:
    from synapse.util.caches.expiring_cache_compat_base import ExpiringCache
except ImportError:
    from synapse.util.caches.expiringcache import ExpiringCache

__all__ = ["ExpiringCache"]