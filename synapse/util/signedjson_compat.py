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
Centralized wrapper for signedjson imports.
This module provides a single point of import for all signedjson functionality
used throughout the Synapse codebase.

Function Mapping:
=================

Functions available in matrices_evolved (preferred when available):
- SigningKey, VerifyKey, SignatureVerifyException (types)
- decode_signing_key_base64, decode_verify_key_bytes, encode_verify_key_base64
- generate_signing_key, get_verify_key, read_signing_keys, write_signing_keys
- is_signing_algorithm_supported, sign_json, verify_signed_json, signature_ids

Functions NOT in matrices_evolved (always from signedjson):
- BaseKey (type) - always imported from signedjson.types
- NACL_ED25519 (constant) - always imported from signedjson.key
- VerifyKeyWithExpiry (type) - always imported from signedjson.key (optional)

Fallback behavior:
- If matrices_evolved is not available, all functions fall back to signedjson
- This provides performance benefits when matrices_evolved is installed
- Maintains full compatibility when only signedjson is available
"""

# Implementation: Try matrices_evolved first, fallback to signedjson
try:
    from matrices_evolved import (
        SigningKey, VerifyKey, SignatureVerifyException,
        decode_signing_key_base64, decode_verify_key_bytes, encode_verify_key_base64,
        generate_signing_key, get_verify_key, read_signing_keys, write_signing_keys,
        is_signing_algorithm_supported, sign_json, verify_signed_json, signature_ids
    )
    # Import missing items from signedjson
    from signedjson.types import BaseKey
    from signedjson.key import NACL_ED25519
except ImportError:
    from signedjson import key, sign
    from signedjson.types import BaseKey, SigningKey, VerifyKey
    
    decode_signing_key_base64 = key.decode_signing_key_base64
    decode_verify_key_bytes = key.decode_verify_key_bytes
    encode_verify_key_base64 = key.encode_verify_key_base64
    generate_signing_key = key.generate_signing_key
    get_verify_key = key.get_verify_key
    read_signing_keys = key.read_signing_keys
    write_signing_keys = key.write_signing_keys
    is_signing_algorithm_supported = key.is_signing_algorithm_supported
    NACL_ED25519 = key.NACL_ED25519
    
    sign_json = sign.sign_json
    verify_signed_json = sign.verify_signed_json
    signature_ids = sign.signature_ids
    SignatureVerifyException = sign.SignatureVerifyException

# Conditional import for VerifyKeyWithExpiry
try:
    from signedjson.key import VerifyKeyWithExpiry
except ImportError:
    VerifyKeyWithExpiry = None

__all__ = [
    # Key functions
    "NACL_ED25519",
    "decode_signing_key_base64",
    "decode_verify_key_bytes", 
    "encode_verify_key_base64",
    "generate_signing_key",
    "get_verify_key",
    "is_signing_algorithm_supported",
    "read_signing_keys",
    "write_signing_keys",
    # Sign functions
    "SignatureVerifyException",
    "sign_json",
    "signature_ids", 
    "verify_signed_json",
    # Types
    "BaseKey",
    "SigningKey",
    "VerifyKey",
    "VerifyKeyWithExpiry",
]