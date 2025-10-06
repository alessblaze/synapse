#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#

"""
Centralized wrapper for JSON functionality with matrices_evolved optimization.
"""

# Try matrices_evolved first, fallback to existing json.py
try:
    from matrices_evolved import json_encode as _matrices_json_encode, json_decode as _matrices_json_decode
    
    class _OptimizedJSONEncoder:
        """Wrapper to provide .encode() interface for matrices_evolved.json_encode"""
        def encode(self, obj):
            return _matrices_json_encode(obj)
        
        def __call__(self, obj):
            return _matrices_json_encode(obj)
    
    class _OptimizedJSONDecoder:
        """Wrapper to provide .decode() interface for matrices_evolved.json_decode"""
        def decode(self, s):
            return _matrices_json_decode(s)
        
        def __call__(self, s):
            return _matrices_json_decode(s)
    
    json_encoder = _OptimizedJSONEncoder()
    json_decoder = _OptimizedJSONDecoder()
    
    import logging
    logging.getLogger(__name__).info("✅ Using optimized matrices_evolved encoder and decoder")
except ImportError:
    from synapse.util.json import json_encoder, json_decoder

__all__ = ["json_encoder", "json_decoder"]