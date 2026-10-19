"""Check strict review contracts and their external JSON boundary."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, cast

from muninn.knowledge_models import (
    AddGuide,
    AddNote,
    ReplaceGuide,
    ReplaceNote,
)
from muninn.review_json import canonical_json, parse_result, result_schema

FIXTURES = Path(__file__).parent / "fixtures" / "learning"
EVIDENCE = "E_11111111111111111111111111111111"
KNOWLEDGE = "K_22222222222222222222222222222222"
REQUEST = "R_33333333333333333333333333333333"
ATTEMPT = "A_44444444444444444444444444444444"
DIGEST = "a" * 64


def fixture(name: str) -> dict[str, Any]:
    """Read an independent hand-authored wire example."""
    return json.loads((FIXTURES / f"result-{name}.json").read_text())


def encoded(value: object) -> bytes:
    """Encode mutated external inputs without using production helpers."""
    return json.dumps(value, ensure_ascii=False).encode()


class ReviewModelTests(unittest.TestCase):
    """Exercise permitted wire shapes and rejection at trust boundaries."""

    def assert_invalid(self, value: object) -> None:
        """Require an external result to fail validation."""
        with self.assertRaises(ValueError):
            parse_result(encoded(value), 100_000)

    def test_flat_action_form_branches(self) -> None:
        for name, added, replaced in (
            ("note", AddNote, ReplaceNote),
            ("guide", AddGuide, ReplaceGuide),
        ):
            data = fixture(name)
            parsed = parse_result(encoded(data), 100_000)
            self.assertIsInstance(parsed.changes[0], added)
            self.assertEqual(json.loads(canonical_json(parsed)), data)
            change = data["changes"][0]
            change.update(
                action="replace", knowledge_id=KNOWLEDGE, expected_revision=1
            )
            parsed = parse_result(encoded(data), 100_000)
            self.assertIsInstance(parsed.changes[0], replaced)
            self.assertEqual(json.loads(canonical_json(parsed)), data)
            self.assertNotIn(
                "body", json.loads(canonical_json(parsed))["changes"][0]
            )
        unchanged = fixture("no-change")
        self.assertEqual(
            json.loads(
                canonical_json(parse_result(encoded(unchanged), 100_000))
            ),
            unchanged,
        )

    def test_strict_extra_fields_and_coercions_refused(self) -> None:
        data = fixture("note")
        data["changes"][0].update(
            action="replace", knowledge_id=KNOWLEDGE, expected_revision=1
        )
        for bad in ("1", True, 0, -1, 1.0):
            changed = copy.deepcopy(data)
            changed["changes"][0]["expected_revision"] = bad
            self.assert_invalid(changed)
        for target, field, value in (
            ("root", "extra", "x"),
            ("root", "schema_version", True),
            ("change", "body", {}),
            ("change", "content", "  "),
            ("change", "content", 1),
            ("scope", "extra", "x"),
            ("scope", "task", 2),
        ):
            changed = copy.deepcopy(data)
            container = changed if target == "root" else changed["changes"][0]
            if target == "scope":
                container = container["scope"]
            container[field] = value
            self.assert_invalid(changed)
        for field in ("conditions", "scope", "reason"):
            changed = copy.deepcopy(data)
            del changed["changes"][0][field]
            self.assert_invalid(changed)

    def test_no_change_reason_iff_empty(self) -> None:
        for reason in (None, "", " "):
            changed = fixture("no-change")
            changed["no_change_reason"] = reason
            self.assert_invalid(changed)
        changed = fixture("note")
        changed["no_change_reason"] = "Saved"
        self.assert_invalid(changed)
        changed = fixture("no-change")
        changed["unresolved"] = [
            {
                "claim": "Needs clarification",
                "affected": [],
                "evidence_refs": [],
                "question": "What evidence is missing?",
            }
        ]
        self.assert_invalid(changed)
        changed["no_change_reason"] = None
        self.assertEqual(
            len(parse_result(encoded(changed), 100_000).unresolved), 1
        )

    def test_guide_claims_and_dependencies(self) -> None:
        for field in (
            "summary",
            "applicability",
            "decision_points",
            "pitfalls",
            "steps",
        ):
            data = fixture("guide")
            guide = data["changes"][0]
            claim = guide[field] if field == "summary" else guide[field][0]
            if field == "steps":
                claim = claim["claim"]
            claim["evidence_refs"] = []
            self.assert_invalid(data)
        for steps in (
            [],
            [
                {
                    "key": "prepare",
                    "claim": {"text": "x", "evidence_refs": [EVIDENCE]},
                    "depends_on": ["future"],
                }
            ],
        ):
            data = fixture("guide")
            data["changes"][0]["steps"] = steps
            self.assert_invalid(data)
        for mutation in ("duplicate", "invalid", "forward", "self"):
            data = fixture("guide")
            steps = data["changes"][0]["steps"]
            if mutation == "duplicate":
                steps[1]["key"] = "prepare"
            elif mutation == "invalid":
                steps[0]["key"] = "Bad-Key"
            else:
                steps[0]["depends_on"] = [
                    "run" if mutation == "forward" else "prepare"
                ]
            self.assert_invalid(data)

    def test_ids_timestamps_and_save_claims_refused(self) -> None:
        for field, value in (
            ("id", KNOWLEDGE),
            ("created_at", 0.0),
            ("saved", True),
            ("state", "active"),
            ("expiry", None),
        ):
            data = fixture("note")
            data["changes"][0][field] = value
            self.assert_invalid(data)
        for bad_id in ("K12", 12, REQUEST, "K_" + "A" * 32):
            data = fixture("note")
            data["changes"][0].update(
                action="replace", knowledge_id=bad_id, expected_revision=1
            )
            self.assert_invalid(data)
        data = fixture("note")
        data["changes"][0]["action"] = "retract"
        self.assert_invalid(data)

    def test_duplicate_json_keys_refused(self) -> None:
        for raw in (
            b'{"schema_version":1,"schema_version":1}',
            encoded(fixture("note")).replace(
                b'"task": null', b'"task": null,"task": null'
            ),
            b'{"x":1,"\\u0078":2}',
        ):
            with self.assertRaises(ValueError):
                parse_result(raw, 100_000)

    def test_json_encoding_numbers_and_size(self) -> None:
        raw = encoded(fixture("no-change"))
        self.assertEqual(parse_result(raw, len(raw)).schema_version, 1)
        for bad, limit in (
            (raw, len(raw) - 1),
            (b"\xff", 100),
            (b'{"x":NaN}', 100),
            (b'{"x":Infinity}', 100),
            (b'{"x":-Infinity}', 100),
            (b'{"x":1e999}', 100),
            (raw + b" {}", 100_000),
            (raw, 0),
        ):
            with self.assertRaises(ValueError):
                parse_result(bad, limit)

    def test_schema_discriminates_flat_branches(self) -> None:
        schema = result_schema()
        properties = cast(dict[str, Any], schema["properties"])
        self.assertIsInstance(properties, dict)
        definitions = cast(dict[str, Any], schema["$defs"])
        change = definitions[
            properties["changes"]["items"]["$ref"].split("/")[-1]
        ]
        self.assertEqual(change["discriminator"]["propertyName"], "action")
        for reference in change["oneOf"]:
            branch = definitions[reference["$ref"].split("/")[-1]]
            self.assertEqual(branch["discriminator"]["propertyName"], "form")
        for name in ("AddNote", "AddGuide", "ReplaceNote", "ReplaceGuide"):
            self.assertNotIn("body", definitions[name]["properties"])
            self.assertFalse(definitions[name]["additionalProperties"])

    def test_canonical_encoding_preserves_unicode(self) -> None:
        data = fixture("no-change")
        data["no_change_reason"] = "Déjà vu"
        self.assertEqual(
            canonical_json(parse_result(encoded(data), 100_000)),
            '{"changes":[],"no_change_reason":"Déjà vu",'
            '"schema_version":1,"unresolved":[]}',
        )

    def test_note_support_and_branch_fields(self) -> None:
        for refs in ([], [EVIDENCE, EVIDENCE], [REQUEST]):
            data = fixture("note")
            data["changes"][0]["evidence_refs"] = refs
            self.assert_invalid(data)
        data = fixture("note")
        data["changes"][0]["conditions"] = [" "]
        self.assert_invalid(data)
        for version in ("1", 1.0, False, 2):
            data = fixture("no-change")
            data["schema_version"] = version
            self.assert_invalid(data)
        for form in ("unknown", "guide"):
            data = fixture("note")
            data["changes"][0]["form"] = form
            self.assert_invalid(data)
