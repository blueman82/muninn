"""Keep the signed distinction between absent and explicit volume fields."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from collections.abc import Mapping
from unittest import mock

from muninn import review_pause_state as state
from muninn.review_json import canonical_json
from tests.test_review_pause import PauseCase

VOLUME = "f5a5fb75-3af2-4f3f-a7b2-5744e5a0b339"


@unittest.skipIf(sys.platform == "win32", "requires POSIX")
class WitnessSerializationTests(PauseCase):
    """Check canonical bytes, copies and the actual witness publisher."""

    def payload(self, volume: Mapping[str, object]) -> dict[str, object]:
        """Supply independently written historical wire fields."""
        return {
            "version": 1,
            "lock_identity": [1, 2],
            "owner": None,
            "state": "complete",
        } | dict(volume)

    def test_serialization_preserves_absence_null_and_uuid(self) -> None:
        for volume in ({}, {"volume_uuid": None}, {"volume_uuid": VOLUME}):
            expected = self.payload(volume)
            witness = state.Witness.model_validate_json(json.dumps(expected))
            with self.subTest(volume=volume):
                self.assertEqual(witness.model_dump(mode="json"), expected)
                self.assertEqual(
                    json.loads(witness.model_dump_json()), expected
                )
                for copy in (
                    witness.model_copy(),
                    witness.model_copy(deep=True),
                ):
                    self.assertEqual(copy.model_dump(mode="json"), expected)
                restored = state.Witness.model_validate(witness.model_dump())
                self.assertEqual(restored.model_dump(mode="json"), expected)

    def test_explicit_null_copy_changes_the_signed_representation(
        self,
    ) -> None:
        absent = state.Witness.model_validate_json(
            json.dumps(self.payload({}))
        )
        explicit = absent.model_copy(update={"volume_uuid": None})
        self.assertNotEqual(canonical_json(absent), canonical_json(explicit))
        self.assertNotEqual(
            hashlib.sha256(canonical_json(absent).encode()).hexdigest(),
            hashlib.sha256(canonical_json(explicit).encode()).hexdigest(),
        )

    def test_publisher_keeps_each_wire_shape_on_linux(self) -> None:
        fd = state.acquire(self.home)
        self.addCleanup(os.close, fd)
        identity = state.lock_identity(self.home, fd)
        with mock.patch.object(state.sys, "platform", "linux"):
            for volume in ({}, {"volume_uuid": None}, {"volume_uuid": VOLUME}):
                payload = self.payload(volume) | {
                    "lock_identity": list(identity)
                }
                witness = state.Witness.model_validate_json(
                    json.dumps(payload)
                )
                state.publish(self.home, fd, state.WITNESS, witness)
                raw = (self.home / state.WITNESS).read_bytes()
                self.assertEqual(json.loads(raw), payload)
                restored = state.parse(raw, state.Witness)
                assert restored is not None
                self.assertEqual(restored.model_dump(mode="json"), payload)

    def test_unbound_factory_keeps_legacy_field_absence(self) -> None:
        fd = state.acquire(self.home)
        self.addCleanup(os.close, fd)
        witness = state.make_witness(self.home, fd, bind_volume=False)
        self.assertNotIn("volume_uuid", witness.model_dump(mode="json"))
