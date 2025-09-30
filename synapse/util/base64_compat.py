# Copyright 2024 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Centralized wrapper for base64 imports.
This module provides a single point of import for all base64 functionality
used throughout the Synapse codebase.

Function Mapping:
=================

Functions available in matrices_evolved (preferred when available):
- decode_base64, encode_base64 (unpadded versions)

Standard library functions (always from base64):
- b64encode, b64decode, urlsafe_b64encode, urlsafe_b64decode (padded versions)

Fallback behavior:
- If matrices_evolved is not available, unpadded functions fall back to unpaddedbase64
- Standard base64 functions always use the standard library
- This provides performance benefits when matrices_evolved is installed
"""

# Implementation: Try matrices_evolved first, fallback to unpaddedbase64
try:
    from matrices_evolved import decode_base64, encode_base64
    # Try padded versions from matrices_evolved
    try:
        from matrices_evolved import encode_base64_padded as b64encode_func, decode_base64_padded as b64decode_func
        b64encode = lambda data: b64encode_func(data).encode() if isinstance(b64encode_func(data), str) else b64encode_func(data)
        b64decode = b64decode_func
    except ImportError:
        from base64 import b64encode, b64decode
except ImportError:
    from unpaddedbase64 import decode_base64, encode_base64
    from base64 import b64encode, b64decode

# Standard library base64 functions (always from standard library)
from base64 import urlsafe_b64encode, urlsafe_b64decode

__all__ = [
    # Unpadded base64 (Matrix-specific)
    "decode_base64",
    "encode_base64",
    # Padded base64 (standard)
    "b64encode",
    "b64decode", 
    "urlsafe_b64encode",
    "urlsafe_b64decode",
]