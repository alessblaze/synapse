import json
import time
import re
import logging
from typing import Dict, Any, Tuple



from synapse.util.base64_compat import encode_base64, decode_base64
from synapse.util.canonicaljson_compat import encode_canonical_json
# Import crypto functions - only available when matrices_evolved is present
try:
    from matrices_evolved import verify_signature_fast, sign_json_fast
except ImportError:
    verify_signature_fast = None
    sign_json_fast = None
import base64

from synapse.http.site import SynapseRequest
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.server import HttpServer
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


class IdentityVerifyServlet(RestServlet):
    PATTERNS = [re.compile("^/_matrix/client/(r0|v3|unstable)/identity/verify$")]

    def __init__(self, hs):
        super().__init__()
        self.hs = hs
        
        # Get RTC keys from config
        rtc_config = hs.config.rtc_key
        self.private_key_bytes = rtc_config.server_private_key_bytes
        self.client_public_key_bytes = rtc_config.client_public_key_bytes
        
        # Keys are guaranteed to exist since servlet only registers when keys are present
        assert self.private_key_bytes and self.client_public_key_bytes
        
        logger.info(f"Matrix-RTC identity servlet initialized with Ed25519 keys")
        logger.info(f"Server private key length: {len(self.private_key_bytes)}")
        logger.info(f"Client public key length: {len(self.client_public_key_bytes)}")
        logger.info(f"Client public key hex: {self.client_public_key_bytes.hex()}")

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        logger.info("Matrix-RTC identity verification request received")
        content = parse_json_object_from_request(request)
        logger.info(f"Request content: {content}")
        
        # Extract payload and signature
        payload = content.get("payload", {})
        signature = content.get("signature", "")
        logger.info(f"Extracted payload: {payload}")
        logger.info(f"Extracted signature: {signature}")
        
        # Validate required fields
        session_id = payload.get("session_id")
        timestamp = payload.get("timestamp")
        nonce = payload.get("nonce")
        
        if not all([session_id, timestamp, nonce, signature]):
            logger.warning(f"Missing required fields - session_id: {bool(session_id)}, timestamp: {bool(timestamp)}, nonce: {bool(nonce)}, signature: {bool(signature)}")
            raise SynapseError(400, "Missing required fields", Codes.MISSING_PARAM)
        
        # Validate timestamp (±30s)
        current_time = int(time.time() * 1000)
        time_diff = abs(current_time - timestamp)
        logger.info(f"Timestamp validation - current: {current_time}, received: {timestamp}, diff: {time_diff}ms")
        if time_diff > 30000:
            logger.warning(f"Clock skew too large: {time_diff}ms > 30000ms")
            raise SynapseError(400, "Clock skew too large", "M_CLOCK_SKEW")
        
        # Nonce provides entropy for signature uniqueness (no replay tracking needed)
        
        # Verify incoming signature using Rust crypto
        try:
            # Use Rust encode_canonical_json for consistent canonicalization
            payload_bytes = encode_canonical_json(payload)
            logger.info(f"Rust canonicalized payload for verification: {payload_bytes.decode('utf-8')}")
            
            # Convert base64url signature to standard base64 for Rust function
            signature_std = signature.replace('-', '+').replace('_', '/')
            logger.info(f"Converted signature from {signature} to {signature_std}")
            # Use Rust verify_signature_fast for Ed25519 verification
            is_valid = verify_signature_fast(list(payload_bytes), signature_std, list(self.client_public_key_bytes))
            if not is_valid:
                raise ValueError("Signature verification failed")
            logger.info("Client signature verification successful")
        except Exception as e:
            logger.error(f"Signature verification failed: {e}")
            logger.info(f"Failed signature was: {signature}")
            logger.info(f"Standard base64 signature: {signature_std}")
            logger.info(f"Expected to verify against payload: {payload_bytes.decode('utf-8')}")
            logger.info(f"Using client public key hex: {self.client_public_key_bytes.hex()}")
            raise SynapseError(401, "Invalid signature", "M_INVALID_SIGNATURE")
        
        # Signature verification completed successfully above ✅
        
        # Check if access_token is provided for userinfo functionality
        access_token = payload.get("access_token")
        device_id = payload.get("device_id")
        user_info = None
        device_validity = None
        
        if access_token:
            logger.info(f"Access token provided, fetching user info")
            # Direct database lookup (no federation dependency)
            store = self.hs.get_datastores().main
            current_time_ms = int(time.time() * 1000)
            user_id = await store.get_user_id_for_open_id_token(access_token, current_time_ms)
            
            if user_id:
                user_info = {"sub": user_id}
                logger.info(f"User info retrieved for: {user_id}")
                
                # Validate device_id if provided
                if device_id:
                    logger.info(f"Device ID provided, validating: {device_id}")
                    device_handler = self.hs.get_device_handler()
                    try:
                        # Check if device belongs to the user
                        device_info = await device_handler.get_device(user_id, device_id)
                        if device_info:
                            device_validity = "valid"
                            logger.info(f"Device {device_id} is valid for user {user_id}")
                        else:
                            device_validity = "invalid"
                            logger.warning(f"Device {device_id} not found for user {user_id}")
                    except Exception as e:
                        device_validity = "invalid"
                        logger.warning(f"Device validation failed: {e}")
            else:
                logger.warning(f"Invalid or expired access token")
        
        response_payload = {
            "session_id": session_id,
            "timestamp": current_time,
            "nonce": nonce
        }
        
        # Add user info if available
        if user_info:
            response_payload["user_info"] = user_info
        
        # Add device validity if checked
        if device_validity:
            response_payload["device_validity"] = device_validity
        
        # Sign with Ed25519 private key using Rust crypto
        # Use Rust encode_canonical_json for consistent canonicalization
        payload_bytes = encode_canonical_json(response_payload)
        logger.info(f"Rust canonicalized response payload for signing: {payload_bytes.decode('utf-8')}")
        
        # Use Rust sign_json_fast for Ed25519 signing (returns standard base64)
        response_signature_std = sign_json_fast(list(payload_bytes), list(self.private_key_bytes))
        # Convert to base64url format for Matrix-RTC compatibility
        response_signature = response_signature_std.replace('+', '-').replace('/', '_')
        logger.info(f"Rust generated response signature (base64url): {response_signature}")
        
        response = {
            "payload": response_payload,
            "signature": response_signature
        }
        logger.info(f"Identity verification successful for session {session_id}")
        return 200, response


def register_servlets(hs: HttpServer, http_server: HttpServer) -> None:
    # Only register Matrix-RTC identity verification if keys are configured AND matrices_evolved is available
    rtc_config = hs.config.rtc_key
    if (rtc_config.server_private_key_bytes and rtc_config.client_public_key_bytes and 
        rtc_config.matrices_evolved_available):
        IdentityVerifyServlet(hs).register(http_server)