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
Centralized wrapper for canonicaljson imports.
This module provides a single point of import for all canonicaljson functionality
used throughout the Synapse codebase.

Function Mapping:
=================

Functions available in matrices_evolved (preferred when available):
- encode_canonical_json

Functions NOT in matrices_evolved (always from canonicaljson):
- json (module) - always imported from canonicaljson
- register_preserialisation_callback - always imported from canonicaljson

Fallback behavior:
- If matrices_evolved is not available, all functions fall back to canonicaljson
- This provides performance benefits when matrices_evolved is installed
- Maintains full compatibility when only canonicaljson is available
"""

# Implementation: Try matrices_evolved first, fallback to canonicaljson
try:
    from matrices_evolved import encode_canonical_json
    # Import missing items from canonicaljson
    from canonicaljson import json, register_preserialisation_callback
except ImportError:
    from canonicaljson import encode_canonical_json, json, register_preserialisation_callback

__all__ = [
    "encode_canonical_json",
    "json", 
    "register_preserialisation_callback",
]