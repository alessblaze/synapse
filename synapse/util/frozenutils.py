#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
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
import collections.abc
from typing import Any

from immutabledict import immutabledict

try:
    # In upstream Synapse, recursive freezing was mainly a defensive tool
    # against accidentally mutating shared Python objects after they had been
    # cached and reused. In this fork, the high-value part of that protection
    # moved into the Rust cache layer, which now owns the shared cache state,
    # synchronization, and object lifetime concerns that originally motivated
    # widespread immutability.
    #
    # Because of that, we intentionally do not preserve "everything is an
    # immutabledict/tuple" semantics across the whole Python surface area by
    # default. Doing so would force a large compatibility burden on the PyO3
    # integration while giving little extra protection for the cache-specific
    # problem we were actually trying to solve.
    #
    # Important nuance: this does NOT mean Rust magically provides Python-level
    # immutability semantics for every returned object. It means the main
    # shared-cache safety reason for global freezing is handled elsewhere now.
    # If a specific Python-facing API still truly needs immutability as part of
    # its contract, that boundary should add a targeted copy/freeze explicitly
    # instead of relying on global recursive freezing here.
    from synapse.util.canonicaljson_compat import MATRICES_EVOLVED_AVAILABLE
except Exception:
    MATRICES_EVOLVED_AVAILABLE = False


def freeze(o: Any) -> Any:
    # See the rationale above: this helper no longer acts as a global Python
    # immutability guarantee in this fork. It only preserves the historical
    # recursive freezing behavior when explicitly enabled by the surrounding
    # integration.
    if not MATRICES_EVOLVED_AVAILABLE:
        return o

    if isinstance(o, dict):
        return immutabledict({k: freeze(v) for k, v in o.items()})

    if isinstance(o, immutabledict):
        return o

    if isinstance(o, (bytes, str)):
        return o

    try:
        return tuple(freeze(i) for i in o)
    except TypeError:
        pass

    return o


def unfreeze(o: Any) -> Any:
    # Symmetric with `freeze`: if we did not globally freeze values on the way
    # in, there is nothing useful to recursively thaw on the way out.
    if not MATRICES_EVOLVED_AVAILABLE:
        return o

    if isinstance(o, collections.abc.Mapping):
        return {k: unfreeze(v) for k, v in o.items()}

    if isinstance(o, (bytes, str)):
        return o

    try:
        return [unfreeze(i) for i in o]
    except TypeError:
        pass

    return o
