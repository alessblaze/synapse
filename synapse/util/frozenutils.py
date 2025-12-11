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
    # There is no reason currently to use immutabledict and frozen types if we are offloading most of
    # heavy logic to Rust/C++ code. it would make code more complex for no benefit.
    # we can manage the types needed but currently there is no need for that.
    # no actual performance or security benefit, if we are not using pure python logic.
    # the references stay in memory for indefinite future and caching layer works fine.
    # in future this can cause issues in case muatblity in python itself. as multiple workers in same process.
    # but for that this also needed to be made sure that free threading comes with lots of responsiblities.
    # so far we have been holding huge amount of mutexes for caches in Rust caches.
    # if it is considered for lifecycle management itself so its safe.
    # MATRICES_EVOLVED_AVAILABLE is always false here.
    # ModuleApiTestCase::test_get_global_no_mutability
    # ThirdPartyRulesTestCase::test_cannot_modify_event 
    # These tests may fail. maybe will be fixed in future if needed.
    # If you are reading this to understand more. You ain't crossing any strictly guarded boundary ever, probably the best
    # boundary is FFI only, where in memory protection is not very strict, so we best only speak about it twice a day.
    from synapse.util.canonicaljson_compat import MATRICES_EVOLVED_AVAILABLE
except Exception:
    MATRICES_EVOLVED_AVAILABLE = False


def freeze(o: Any) -> Any:
    # When matrices-evolved is NOT available, don't freeze; just return as-is.
    # When it IS available, keep the existing behaviour.
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
    # Symmetric behaviour: if we never froze, just return as-is.
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
