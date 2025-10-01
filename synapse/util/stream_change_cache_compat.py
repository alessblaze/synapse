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

import logging
logger = logging.getLogger(__name__)

try:
    # Check if matrices_evolved is available first
    import matrices_evolved.rust
    # Only import from stream_change_cache_base if matrices_evolved is available
    from synapse.util.caches.stream_change_cache_base import StreamChangeCache, AllEntitiesChangedResult
    logger.info("✅ matrices_evolved available, using optimized Rust StreamChangeCache")
except ImportError as e:
    logger.info("⚠️ matrices_evolved not available, using fallback StreamChangeCache")
    from synapse.util.caches.stream_change_cache import StreamChangeCache, AllEntitiesChangedResult

__all__ = ["StreamChangeCache", "AllEntitiesChangedResult"]