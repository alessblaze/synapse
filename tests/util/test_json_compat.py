#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#

from unittest import mock

from immutabledict import immutabledict

from synapse.util import json_compat
from synapse.util.json_compat import json_decoder, json_encoder

from tests.unittest import TestCase


class JsonCompatTestCase(TestCase):
    def test_encode_none(self) -> None:
        self.assertEqual(json_encoder.encode(None), "null")

    def test_encode_immutabledict(self) -> None:
        self.assertEqual(
            json_encoder.encode(immutabledict({"a": 1})),
            '{"a":1}',
        )

    def test_encode_uses_matrices_evolved_when_supported(self) -> None:
        if not hasattr(json_compat, "_matrices_json_encode"):
            self.skipTest("matrices_evolved is not installed")

        with mock.patch.object(
            json_compat,
            "_matrices_json_encode",
            wraps=json_compat._matrices_json_encode,
        ) as mock_encode:
            self.assertEqual(json_encoder.encode({"a": 1}), '{"a":1}')

        mock_encode.assert_called_once_with({"a": 1})

    def test_encode_falls_back_when_matrices_evolved_rejects_value(self) -> None:
        if not hasattr(json_compat, "_matrices_json_encode"):
            self.skipTest("matrices_evolved is not installed")

        with (
            mock.patch.object(
                json_compat,
                "_matrices_json_encode",
                side_effect=TypeError("unsupported"),
            ) as mock_encode,
            mock.patch.object(
                json_compat._fallback_json_encoder,
                "encode",
                wraps=json_compat._fallback_json_encoder.encode,
            ) as mock_fallback,
        ):
            self.assertEqual(json_encoder.encode(None), "null")

        mock_encode.assert_called_once_with(None)
        mock_fallback.assert_called_once_with(None)

    def test_decode_uses_matrices_evolved_when_available(self) -> None:
        if not hasattr(json_compat, "_matrices_json_decode"):
            self.skipTest("matrices_evolved is not installed")

        with mock.patch.object(
            json_compat,
            "_matrices_json_decode",
            wraps=json_compat._matrices_json_decode,
        ) as mock_decode:
            self.assertEqual(json_decoder.decode('{"a":1}'), {"a": 1})

        mock_decode.assert_called_once_with('{"a":1}')
