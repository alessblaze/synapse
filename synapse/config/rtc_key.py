import os
from typing import Any, Optional
from cryptography.hazmat.primitives import serialization
from synapse.util.base64_compat import b64decode
import yaml

from synapse.types import JsonDict
from ._base import Config, ConfigError


class DuplicateKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConfigError(f"Duplicate key '{key}' found in YAML configuration")
            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value
        return mapping


class RTCKeyConfig(Config):
    section = "rtc_key"

    def read_config(
        self,
        config: JsonDict,
        config_dir_path: str,
        allow_secrets_in_config: bool,
        **kwargs: Any,
    ) -> None:
        # Check for duplicate keys in matrix_rtc_v2 section
        if "matrix_rtc_v2" in config:
            self._validate_no_duplicates(config_dir_path)
        
        rtc_config = config.get("matrix_rtc_v2") or {}
        
        # Server private key for signing responses
        server_key_path = rtc_config.get("server_key_path")
        server_key_base64 = rtc_config.get("server_key_base64")
        
        # Check for conflicting server key configuration
        if (server_key_path and server_key_path.strip() and 
            server_key_base64 and server_key_base64.strip()):
            raise ConfigError("Matrix-RTC server key configured in both 'server_key_path' and 'server_key_base64'. Use only one method.")
        
        if server_key_path and server_key_path.strip():
            self.server_private_key_bytes = self._load_private_key(server_key_path.strip())
        elif server_key_base64 and server_key_base64.strip():
            try:
                self.server_private_key_bytes = b64decode(server_key_base64.strip())
                if len(self.server_private_key_bytes) != 32:
                    raise ValueError(f"Expected 32 bytes for Ed25519 key, got {len(self.server_private_key_bytes)}")
            except Exception as e:
                raise ConfigError(f"Error decoding RTC server key base64 '{server_key_base64.strip()}': {e}")
        else:
            self.server_private_key_bytes = None

        # Client public key for verifying requests
        client_key_path = rtc_config.get("client_key_path")
        client_key_base64 = rtc_config.get("client_key_base64")
        
        # Check for conflicting client key configuration
        if (client_key_path and client_key_path.strip() and 
            client_key_base64 and client_key_base64.strip()):
            raise ConfigError("Matrix-RTC client key configured in both 'client_key_path' and 'client_key_base64'. Use only one method.")
        
        if client_key_path and client_key_path.strip():
            self.client_public_key_bytes = self._load_public_key(client_key_path.strip())
        elif client_key_base64 and client_key_base64.strip():
            try:
                self.client_public_key_bytes = b64decode(client_key_base64.strip())
                if len(self.client_public_key_bytes) != 32:
                    raise ValueError(f"Expected 32 bytes for Ed25519 key, got {len(self.client_public_key_bytes)}")
            except Exception as e:
                raise ConfigError(f"Error decoding RTC client key base64 '{client_key_base64.strip()}': {e}")
        else:
            self.client_public_key_bytes = None

        # Check matrices_evolved availability for Matrix-RTC
        self.matrices_evolved_available = False
        try:
            from matrices_evolved import verify_signature_fast, sign_json_fast
            self.matrices_evolved_available = True
        except ImportError:
            pass
        
        # Validate configuration completeness
        has_config_section = "matrix_rtc_v2" in config
        has_keys = self.server_private_key_bytes is not None and self.client_public_key_bytes is not None
        has_partial_keys = (self.server_private_key_bytes is None) != (self.client_public_key_bytes is None)
        
        # First check: if matrix_rtc_v2 section exists, matrices_evolved must be available
        if has_config_section and not self.matrices_evolved_available:
            raise ConfigError("Matrix-RTC Identity Verification V2 configuration section 'matrix_rtc_v2' is present but 'matrices_evolved' is not installed. It is not neccessary for Matrix-RTC to work. Install it with: pip install matrices-evolved. Remember, it only works with https://github.com/alessblaze/livekit-jwt-service-ams")
        elif has_config_section and not has_keys and not has_partial_keys:
            # matrix_rtc_v2 section exists but no keys provided (all commented out or empty)
            raise ConfigError("Matrix-RTC Identity Verification V2 section 'matrix_rtc_v2' is present but no keys are configured. Either provide both server and client keys, or remove the section entirely to disable Matrix-RTC Identity Verification V2.")
        elif has_partial_keys:
            # Only one key provided
            missing = "server key" if self.server_private_key_bytes is None else "client key"
            provided = "client key" if self.server_private_key_bytes is None else "server key"
            raise ConfigError(f"Matrix-RTC Identity Verification V2 incomplete: {missing} missing but {provided} provided. Both keys are required for Matrix-RTC Identity Verification V2.")
        
        # Log Matrix-RTC status (only if no error was raised)
        import logging
        logger = logging.getLogger(__name__)
        if has_keys and self.matrices_evolved_available:
            logger.info("Matrix-RTC identity verification V2 endpoint will be enabled")
        elif not has_keys:
            logger.info("Matrix-RTC identity verification V2 endpoint disabled (no keys configured)")

    def _load_private_key(self, key_path: str) -> bytes:
        try:
            with open(key_path, "rb") as f:
                private_key_obj = serialization.load_pem_private_key(f.read(), password=None)
                return private_key_obj.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption()
                )
        except Exception as e:
            raise ConfigError(f"Error loading RTC Verification V2  server private key from {key_path}: {e}")

    def _load_public_key(self, key_path: str) -> bytes:
        try:
            with open(key_path, "rb") as f:
                public_key_obj = serialization.load_pem_public_key(f.read())
                return public_key_obj.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
        except Exception as e:
            raise ConfigError(f"Error loading RTC Verification V2 client public key from {key_path}: {e}")

    def generate_config_section(
        self,
        config_dir_path: str,
        server_name: str,
        generate_secrets: bool = False,
        **kwargs: Any,
    ) -> str:
        return """\
        # Matrix-RTC identity verification keys (both keys required)
        # matrix_rtc_v2:
        #   # Option 1: PEM file paths
        #   server_key_path: "rtc_server.pem"
        #   client_key_path: "rtc_client_public.pem"
        #   # Option 2: Base64 encoded raw keys (32 bytes for Ed25519)
        #   server_key_base64: "xNa5/PQV7BAM6c24+ahsklh0GcqWkEVvwsE0P0H34="
        #   client_key_base64: "AbCdEf1234567890..."
        #   # Note: Provide either both keys or neither (partial config will cause startup error)
        """

    def _validate_no_duplicates(self, config_dir_path: str) -> None:
        """Validate YAML has no duplicate keys using custom loader."""
        config_file = os.path.join(config_dir_path, "homeserver.yaml")
        if not os.path.exists(config_file):
            return
        
        try:
            with open(config_file, 'r') as f:
                yaml.load(f, Loader=DuplicateKeyLoader)
        except yaml.YAMLError:
            pass  # Let normal YAML parsing handle syntax errors