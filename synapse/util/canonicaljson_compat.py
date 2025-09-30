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