"""Key-scoped JSON/TOML edits: layout refusal, byte-exact restore, races."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import configedit as ce

CRED = "sk-fake-" + "0123456789abcdef" * 2
MKT = "[marketplaces.provenance-context-local]"
PLUGIN = '[plugins."provenance-context@provenance-context-local"]'
TRUST = (
    '[hooks.state."provenance-context@provenance-context-local:'
    'hooks/hooks.json:session_start:0:0"]'
)
NAMED = (MKT, PLUGIN, TRUST)
MARKERS = ("provenance-context-local", "provenance-context@")
TOML = f"""model = "gpt-test"

[model_providers.fake]
name = "Fake"
api_key = "{CRED}"
notes = \"\"\"
[plugins."looks-like-a-header"]
\"\"\"

{MKT}
source_type = "local"
source = "/fake/source-dir"

[marketplaces.other]
source = "/fake/other"

{TRUST}
trusted_hash = "sha256:{"b" * 64}"

{PLUGIN}
enabled = false

[plugins."other@m"]
enabled = true
"""
SETTINGS = {
    "model": "opus",
    "env": {"TOKEN": CRED},
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},
    "enabledPlugins": {"a@b": True, "provenance-context@p": False},
}


def dump(obj: object) -> bytes:
    """Serialise ``obj`` as indented JSON with a final newline.

    Args:
        obj: A JSON-serialisable value.

    Returns:
        The encoded document.
    """
    return (json.dumps(obj, indent=2) + "\n").encode()


class TomlSectionTest(unittest.TestCase):
    """Section lookup, removal, restore and layout refusal in TOML."""

    def test_header_inside_multiline_string_is_not_a_section(self) -> None:
        fake = '[plugins."looks-like-a-header"]'
        self.assertIsNone(ce.get_section(TOML, fake))
        section = ce.get_section(TOML, MKT)
        assert section is not None
        self.assertIn('source = "/fake/source-dir"', section)

    def test_remove_then_restore_is_byte_identical(self) -> None:
        for header in NAMED:
            with self.subTest(header=header):
                raw = ce.get_section(TOML, header)
                at = ce.index_of(TOML, header)
                gone = ce.put_section(TOML, header, None)
                self.assertNotIn(header, gone)
                ce.toml_check(TOML, gone, NAMED, MARKERS)
                back = ce.put_section(gone, header, raw, at=at)
                self.assertEqual(back, TOML)

    def test_parse_section_reads_only_that_block(self) -> None:
        self.assertEqual(
            ce.parse_section(TOML, MKT, {"source_type", "source"}),
            {"source_type": "local", "source": "/fake/source-dir"},
        )
        self.assertEqual(
            ce.parse_section(TOML, PLUGIN, {"enabled"}), {"enabled": False}
        )

    def test_toml_check_catches_a_change_outside_named_sections(self) -> None:
        bad = TOML.replace('model = "gpt-test"', 'model = "other"')
        with self.assertRaises(ce.RefusedError) as cm:
            ce.toml_check(TOML, bad, NAMED, MARKERS)
        self.assertNotIn("other", str(cm.exception))

    def test_unexpected_layouts_refuse(self) -> None:
        cases = {
            "duplicate": TOML + f"\n{MKT}\nsource = 'x'\n",
            "inline table": TOML.replace(
                'source = "/fake/source-dir"', "source = { a = 1 }"
            ),
            "array value": TOML.replace(
                'source = "/fake/source-dir"', 'source = ["x"]'
            ),
            "comment in section": TOML.replace(
                "enabled = false", "enabled = false\n# note"
            ),
            "unknown key": TOML.replace("enabled = false", "colour = true"),
            "dotted key elsewhere": TOML
            + '\n[plugins]\n"provenance-context@x".enabled = true\n',
            "identifier in a string": TOML.replace(
                '[plugins."looks-like-a-header"]', MKT
            ),
            "sub-table": TOML + f"\n{PLUGIN[:-1]}.mcp_servers.x]\nk = 1\n",
        }
        keys = {
            MKT: {"source_type", "source"},
            PLUGIN: {"enabled"},
            TRUST: {"trusted_hash"},
        }
        ok = ce.scan_named(TOML, keys, MARKERS)  # pass-shaped control
        self.assertEqual(ok[PLUGIN], {"enabled": False})
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(ce.RefusedError) as cm:
                    ce.scan_named(text, keys, MARKERS)
                self.assertNotIn(CRED, str(cm.exception))

    def test_append_needs_trailing_newline(self) -> None:
        with self.assertRaises(ce.RefusedError):
            ce.put_section("a = 1", PLUGIN, f"\n{PLUGIN}\nenabled = true\n")


class JsonTest(unittest.TestCase):
    """Path-scoped JSON edits restore byte for byte."""

    def test_delete_then_restore_at_index_is_byte_identical(self) -> None:
        before = dump(SETTINGS)
        obj = json.loads(before)
        path = ("enabledPlugins", "provenance-context@p")
        present, value, index = ce.jget(obj, path)
        self.assertEqual((present, value, index), (True, False, 1))
        ce.jdel(obj, path)
        ce.jset(obj, ("hooks", "SessionStart"), [{"hooks": []}])
        after = ce.dump_like(before, obj)
        ce.json_check(before, after, [path, ("hooks", "SessionStart")])
        ce.jdel(obj, ("hooks", "SessionStart"))
        ce.jset(obj, path, value, index)
        self.assertEqual(ce.dump_like(before, obj), before)

    def test_json_check_catches_stray_change(self) -> None:
        before = dump(SETTINGS)
        obj = json.loads(before)
        obj["env"]["TOKEN"] = "changed"
        with self.assertRaises(ce.RefusedError) as cm:
            ce.json_check(before, dump(obj), [("hooks", "SessionStart")])
        self.assertNotIn(CRED, str(cm.exception))

    def test_dump_like_keeps_indent_and_missing_newline(self) -> None:
        raw = json.dumps({"a": {"b": 1}}, indent=4).encode()
        self.assertEqual(ce.dump_like(raw, json.loads(raw)), raw)


class EditFileTest(unittest.TestCase):
    """edit_file writes atomically and survives concurrent writers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(TOML)
        self.path.chmod(0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def enable(self, data: bytes) -> bytes:
        """Flip the plugin to enabled, as the installer does."""
        return data.replace(b"enabled = false", b"enabled = true")

    def check(self, before: bytes, after: bytes) -> None:
        """Apply the TOML layout check to a before and after pair."""
        ce.toml_check(before.decode(), after.decode(), NAMED, MARKERS)

    def test_writes_atomically_and_keeps_mode(self) -> None:
        ce.edit_file(self.path, self.enable, self.check)
        self.assertNotIn(b"enabled = false", self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        names = [p.name for p in Path(self.tmp.name).iterdir()]
        self.assertEqual(names, ["config.toml"])

    def test_concurrent_change_is_remerged(self) -> None:
        real = ce._read
        calls: list[int] = []

        def racing_read(path: Path) -> bytes:
            data = real(path)
            calls.append(1)
            if len(calls) == 1:  # someone edits right after our first read
                path.write_bytes(data.replace(b"gpt-test", b"gpt-other"))
            return data

        with mock.patch.object(ce, "_read", racing_read):
            ce.edit_file(self.path, self.enable, self.check)
        final = self.path.read_bytes()
        self.assertIn(b"gpt-other", final)
        self.assertNotIn(b"enabled = false", final)

    def test_file_that_keeps_changing_aborts_without_writing(self) -> None:
        real = ce._read
        count: list[int] = []

        def always_racing(path: Path) -> bytes:
            data = real(path)
            count.append(1)
            path.write_bytes(data + b"# edit %d\n" % len(count))
            return data

        with (
            mock.patch.object(ce, "_read", always_racing),
            self.assertRaises(ce.RacedError),
        ):
            ce.edit_file(self.path, self.enable, self.check)
        self.assertIn(b"enabled = false", self.path.read_bytes())

    def test_refused_transform_writes_nothing(self) -> None:
        before = self.path.read_bytes()

        def stray(data: bytes) -> bytes:
            return data.replace(b"gpt-test", b"gpt-x")

        with self.assertRaises(ce.RefusedError):
            ce.edit_file(self.path, stray, self.check)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
