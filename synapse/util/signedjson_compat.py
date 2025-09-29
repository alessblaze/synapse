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