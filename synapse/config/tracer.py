#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.d
# Copyright (C) 2023 New Vector, Ltd
# Copyright 2025 Aless Microsystems
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from typing import Any

from synapse.types import JsonDict
from synapse.util.check_dependencies import check_requirements

from ._base import Config, ConfigError


class TracerConfig(Config):
    section = "tracing"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        opentracing_config = config.get("opentelemetry")
        if opentracing_config is None:
            opentracing_config = {}

        self.opentracer_enabled = opentracing_config.get("enabled", False)


        
        # OTLP endpoint for OpenTelemetry traces
        raw_endpoint = opentracing_config.get(
            "otlp_endpoint", "http://localhost:4318/v1/traces"
        )
        self.otlp_endpoint = self._normalize_otlp_endpoint(raw_endpoint)
        

        
        # Sampling configuration
        sampler_config = opentracing_config.get("sampler", {})
        self.sampler_type = sampler_config.get("type", "ratio")
        
        if self.sampler_type == "ratio":
            self.sampling_ratio = sampler_config.get("ratio", opentracing_config.get("sampling_ratio", 1.0))
            if not isinstance(self.sampling_ratio, (int, float)) or not (0.0 <= self.sampling_ratio <= 1.0):
                raise ConfigError("sampler.ratio must be a number between 0.0 and 1.0")
        elif self.sampler_type == "always_on":
            self.sampling_ratio = 1.0
        elif self.sampler_type == "always_off":
            self.sampling_ratio = 0.0
        else:
            raise ConfigError(f"Unknown sampler type: {self.sampler_type}. Must be 'ratio', 'always_on', or 'always_off'")
        
        # OpenTelemetry batch processor configuration
        batch_config = opentracing_config.get("batch_config", {})
        self.batch_max_export_batch_size = batch_config.get("max_export_batch_size", 512)
        self.batch_export_timeout_millis = batch_config.get("export_timeout_millis", 30000)
        self.batch_schedule_delay_millis = batch_config.get("schedule_delay_millis", 5000)
        self.batch_max_queue_size = batch_config.get("max_queue_size", 2048)
        
        # Validate batch config
        if not isinstance(self.batch_max_export_batch_size, int) or self.batch_max_export_batch_size <= 0:
            raise ConfigError("batch_config.max_export_batch_size must be a positive integer")
        if not isinstance(self.batch_export_timeout_millis, int) or self.batch_export_timeout_millis <= 0:
            raise ConfigError("batch_config.export_timeout_millis must be a positive integer")
        if not isinstance(self.batch_schedule_delay_millis, int) or self.batch_schedule_delay_millis <= 0:
            raise ConfigError("batch_config.schedule_delay_millis must be a positive integer")
        if not isinstance(self.batch_max_queue_size, int) or self.batch_max_queue_size <= 0:
            raise ConfigError("batch_config.max_queue_size must be a positive integer")
        
        # OpenTelemetry logging
        self.otel_logging_enabled = opentracing_config.get("logging", False)
        
        # Resource attributes
        self.resource_attributes = opentracing_config.get("resource_attributes", {})
        if not isinstance(self.resource_attributes, dict):
            raise ConfigError("resource_attributes must be a dictionary")

        self.force_tracing_for_users: set[str] = set()

        if not self.opentracer_enabled:
            return

        # Check for OpenTelemetry dependencies instead of jaeger-client
        try:
            from opentelemetry import trace as otel_trace
            import opentelemetry.exporter.otlp.proto.http.trace_exporter
            import opentelemetry.shim.opentracing_shim
            import opentelemetry.sdk.resources
        except ImportError as e:
            raise ConfigError(
                f"The server has been configured to use opentracing but required "
                f"OpenTelemetry packages are not installed: {e}"
            )

        # The tracer is enabled so sanitize the config

        self.opentracer_whitelist: list[str] = opentracing_config.get(
            "homeserver_whitelist", []
        )
        if not isinstance(self.opentracer_whitelist, list):
            raise ConfigError("Tracer homeserver_whitelist config is malformed")

        force_tracing_for_users = opentracing_config.get("force_tracing_for_users", [])
        if not isinstance(force_tracing_for_users, list):
            raise ConfigError(
                "Expected a list", ("opentracing", "force_tracing_for_users")
            )
        for i, u in enumerate(force_tracing_for_users):
            if not isinstance(u, str):
                raise ConfigError(
                    "Expected a string",
                    ("opentracing", "force_tracing_for_users", f"index {i}"),
                )
            self.force_tracing_for_users.add(u)
    
    def _normalize_otlp_endpoint(self, endpoint: str) -> str:
        """Normalize OTLP endpoint URL and warn about common issues."""
        import logging
        from urllib.parse import urlparse, urlunparse
        
        logger = logging.getLogger(__name__)
        
        # Remove trailing slashes
        endpoint = endpoint.rstrip('/')
        
        # Parse URL to check structure
        parsed = urlparse(endpoint)
        
        # Warn if no path provided for HTTP endpoints
        if parsed.scheme in ('http', 'https') and not parsed.path:
            logger.warning(
                "OTLP endpoint '%s' has no path. This will likely result in 404 errors. "
                "Consider using '%s/v1/traces' instead.",
                endpoint, endpoint
            )
            return f"{endpoint}/v1/traces"
        
        # Ensure /v1/traces path for HTTP endpoints if not already present
        if parsed.scheme in ('http', 'https') and not parsed.path.endswith('/v1/traces'):
            if parsed.path and not parsed.path.endswith('/'):
                endpoint += '/v1/traces'
            else:
                endpoint += 'v1/traces'
        
        return endpoint
