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

The goal here is to keep `matrices_evolved` on the normal fast path without
changing Synapse's historical JSON semantics at integration boundaries.

In particular, some older Synapse call sites rely on the stdlib-style
`json.JSONEncoder.encode(...)` contract for values that are not performance
critical, such as optional metadata fields. A concrete example is passing
`None` to mean "no tracing context"; the legacy encoder turns that into `null`,
while the optimized binding may reject it before its internal serializer runs.
That tracing context is observability metadata attached to a send/update path,
not required business data for the operation itself.

Because of that, encoding uses `matrices_evolved` first and falls back to
Synapse's previous encoder only when the optimized implementation refuses a
value. This preserves compatibility for edge cases while keeping the optimized
implementation for ordinary JSON payloads.
"""

from synapse.util.json import (
    json_decoder as _fallback_json_decoder,
    json_encoder as _fallback_json_encoder,
)

# Use matrices_evolved when available, but preserve the previous Synapse
# encoder/decoder behavior when the optimized binding cannot represent a value.
try:
    from matrices_evolved import json_encode as _matrices_json_encode, json_decode as _matrices_json_decode

    class _OptimizedJSONEncoder:
        """Wrapper to provide `.encode()` interface with Synapse compatibility."""

        def encode(self, obj):
            try:
                return _matrices_json_encode(obj)
            except (TypeError, ValueError):
                # Some Synapse call sites intentionally pass edge-case values
                # such as `None` for optional metadata. Those values are not on
                # the hot path, but callers still expect the old `.encode()`
                # semantics instead of a hard failure from the optimized
                # binding, so we delegate back to the legacy encoder here.
                return _fallback_json_encoder.encode(obj)

        def __call__(self, obj):
            return self.encode(obj)

    class _OptimizedJSONDecoder:
        """Wrapper to provide `.decode()` interface for matrices_evolved.json_decode."""

        def decode(self, s):
            return _matrices_json_decode(s)

        def __call__(self, s):
            return self.decode(s)

    json_encoder = _OptimizedJSONEncoder()
    json_decoder = _OptimizedJSONDecoder()

    import logging

    logging.getLogger(__name__).info(
        "Using optimized matrices_evolved encoder and decoder"
    )
except ImportError:
    json_encoder = _fallback_json_encoder
    json_decoder = _fallback_json_decoder

__all__ = ["json_encoder", "json_decoder"]
