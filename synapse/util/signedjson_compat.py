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

# Create wrapper classes that expose matrices_evolved through original interface
class KeyModule:
    """Wrapper for signedjson.key module that uses matrices_evolved when available"""
    def __init__(self):
        try:
            from matrices_evolved import (
                SigningKey, VerifyKey,
                decode_signing_key_base64, decode_verify_key_bytes, encode_verify_key_base64,
                generate_signing_key, get_verify_key, read_signing_keys, write_signing_keys,
                is_signing_algorithm_supported
            )
            self.SigningKey = SigningKey
            self.VerifyKey = VerifyKey
            self.decode_signing_key_base64 = decode_signing_key_base64
            self.decode_verify_key_bytes = decode_verify_key_bytes
            self.encode_verify_key_base64 = encode_verify_key_base64
            self.generate_signing_key = generate_signing_key
            self.get_verify_key = get_verify_key
            self.read_signing_keys = read_signing_keys
            self.write_signing_keys = write_signing_keys
            self.is_signing_algorithm_supported = is_signing_algorithm_supported
        except ImportError:
            import signedjson.key as orig_key
            self.SigningKey = orig_key.SigningKey
            self.VerifyKey = orig_key.VerifyKey
            self.decode_signing_key_base64 = orig_key.decode_signing_key_base64
            self.decode_verify_key_bytes = orig_key.decode_verify_key_bytes
            self.encode_verify_key_base64 = orig_key.encode_verify_key_base64
            self.generate_signing_key = orig_key.generate_signing_key
            self.get_verify_key = orig_key.get_verify_key
            self.read_signing_keys = orig_key.read_signing_keys
            self.write_signing_keys = orig_key.write_signing_keys
            self.is_signing_algorithm_supported = orig_key.is_signing_algorithm_supported
        
        # Always from signedjson.key
        from signedjson.key import NACL_ED25519, VerifyKeyWithExpiry
        self.NACL_ED25519 = NACL_ED25519
        self.VerifyKeyWithExpiry = VerifyKeyWithExpiry

class SignModule:
    """Wrapper for signedjson.sign module that uses matrices_evolved when available"""
    def __init__(self):
        try:
            from matrices_evolved import (
                SigningKey, VerifyKey, SignatureVerifyException,
                sign_json, verify_signed_json, signature_ids
            )
            self.SigningKey = SigningKey
            self.VerifyKey = VerifyKey
            self.SignatureVerifyException = SignatureVerifyException
            self.sign_json = sign_json
            self.verify_signed_json = verify_signed_json
            self.signature_ids = signature_ids
        except ImportError:
            import signedjson.sign as orig_sign
            self.SigningKey = orig_sign.SigningKey
            self.VerifyKey = orig_sign.VerifyKey
            self.SignatureVerifyException = orig_sign.SignatureVerifyException
            self.sign_json = orig_sign.sign_json
            self.verify_signed_json = orig_sign.verify_signed_json
            self.signature_ids = orig_sign.signature_ids

# Create module instances
key = KeyModule()
sign = SignModule()

# Export individual functions and types for direct access
try:
    from matrices_evolved import (
        SigningKey, VerifyKey, SignatureVerifyException,
        decode_signing_key_base64, encode_verify_key_base64,
        generate_signing_key, get_verify_key, read_signing_keys, write_signing_keys,
        is_signing_algorithm_supported, sign_json, verify_signed_json, signature_ids
    )
    # Wrap decode_verify_key_bytes with debug logging
    from matrices_evolved import decode_verify_key_bytes as _orig_decode_verify_key_bytes
    import logging
    _debug_logger = logging.getLogger(__name__ + ".debug")
    def decode_verify_key_bytes(key_id, key_bytes):
        _debug_logger.info(f"🔍 decode_verify_key_bytes: key_id={key_id}, key_bytes={key_bytes!r} (hex: {key_bytes.hex() if isinstance(key_bytes, bytes) else 'not bytes'})")
        result = _orig_decode_verify_key_bytes(key_id, key_bytes)
        _debug_logger.info(f"🔍 decode_verify_key_bytes result: {result}")
        return result
    
    from signedjson.types import BaseKey
    from signedjson.key import NACL_ED25519
    import logging
    logging.getLogger(__name__).info("✅ matrices_evolved available, using optimized signedjson functions")
except ImportError:
    from signedjson import key as orig_key, sign as orig_sign
    from signedjson.types import BaseKey, SigningKey, VerifyKey
    
    decode_signing_key_base64 = orig_key.decode_signing_key_base64
    # Wrap decode_verify_key_bytes with debug logging
    import logging
    _debug_logger = logging.getLogger(__name__ + ".debug")
    def decode_verify_key_bytes(key_id, key_bytes):
        _debug_logger.info(f"🔍 decode_verify_key_bytes: key_id={key_id}, key_bytes={key_bytes!r} (hex: {key_bytes.hex() if isinstance(key_bytes, bytes) else 'not bytes'})")
        result = orig_key.decode_verify_key_bytes(key_id, key_bytes)
        _debug_logger.info(f"🔍 decode_verify_key_bytes result: {result}")
        return result
    
    encode_verify_key_base64 = orig_key.encode_verify_key_base64
    generate_signing_key = orig_key.generate_signing_key
    get_verify_key = orig_key.get_verify_key
    read_signing_keys = orig_key.read_signing_keys
    write_signing_keys = orig_key.write_signing_keys
    is_signing_algorithm_supported = orig_key.is_signing_algorithm_supported
    NACL_ED25519 = orig_key.NACL_ED25519
    
    sign_json = orig_sign.sign_json
    verify_signed_json = orig_sign.verify_signed_json
    signature_ids = orig_sign.signature_ids
    SignatureVerifyException = orig_sign.SignatureVerifyException

# Conditional import for VerifyKeyWithExpiry
try:
    from signedjson.key import VerifyKeyWithExpiry
except ImportError:
    VerifyKeyWithExpiry = None

__all__ = [
    # Modules (wrapper classes that use matrices_evolved internally)
    "key",
    "sign",
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