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