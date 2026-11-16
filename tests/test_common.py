"""Test helpers shared by tweaks using temporary directories only."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("attic_common_under_test",
                                              Path(__file__).parents[1] / "lib/attic_common.py")
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)


def validate(config):
    if set(config) != {"level", "name"} or type(config["level"]) is not int:
        raise ValueError("invalid configuration")


class HelperTests(unittest.TestCase):
    def test_symlink_rejected(self):
        """A managed path must not follow an attacker-supplied symlink."""
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "link"
            link.symlink_to(Path(directory) / "missing")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                common.secure_path(link)

    def test_version_accepts_prefixed_and_bare_formats(self):
        """Both known /etc/version spellings give the same bare release."""
        for content in ("TrueNAS-SCALE-25.10.7\n", "TrueNAS-25.10.7", "25.10.7\n"):
            with self.subTest(content=content), patch.object(Path, "read_text", return_value=content):
                self.assertEqual(common.truenas_version(), "25.10.7")

    def test_version_rejects_other_systems(self):
        """A file with unrelated content does not pass as TrueNAS."""
        with patch.object(Path, "read_text", return_value="Debian 12"):
            with self.assertRaisesRegex(ValueError, "requires TrueNAS"):
                common.truenas_version()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "tweaks/example"
        folder.mkdir(parents=True)
        (folder / "config.json").write_text('{"level": 1, "name": "x"}')
        self.override = self.root / "local/config/example.json"

    def edit(self, edited, apply=None):
        """Replace only the editor process; JSON handling and files stay real."""
        def editor(command, **kwargs):
            Path(command[-1]).write_text(edited)
        with patch.object(common.subprocess, "run", side_effect=editor):
            common.edit_config(self.root, "example", validate, apply=apply)

    def read(self):
        return common.read_config(self.root, "example", validate)

    def test_defaults_apply_without_local_overrides(self):
        """A fresh checkout works from versioned values."""
        self.assertEqual(self.read(), {"level": 1, "name": "x"})

    def test_new_default_reaches_existing_installation(self):
        """Only changed values are stored, so later default changes still arrive."""
        self.edit('{"level": 5, "name": "x"}')
        self.assertEqual(json.loads(self.override.read_text()), {"level": 5})
        (self.root / "tweaks/example/config.json").write_text('{"level": 1, "name": "new"}')
        self.assertEqual(self.read(), {"level": 5, "name": "new"})

    def test_returning_to_defaults_removes_override(self):
        """Removing a key or restoring its value leaves no stale local file."""
        self.edit('{"level": 5, "name": "x"}')
        self.edit('{"name": "x"}')
        self.assertFalse(self.override.exists())

    def test_invalid_edit_keeps_saved_config(self):
        """Invalid values never replace overrides or invoke apply."""
        self.edit('{"level": 5, "name": "x"}')
        apply = Mock()
        for invalid in ('{"level": "high"}', '{"extra": 1}', '[]', 'not json'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.edit(invalid, apply=apply)
        apply.assert_not_called()
        self.assertEqual(self.read()["level"], 5)
        self.assertEqual(list(self.override.parent.glob(".edit-*")), [])

    def test_unchanged_edit_does_not_apply(self):
        """Closing the editor without changes performs no runtime writes."""
        apply = Mock()
        self.edit('{"level": 1, "name": "x"}', apply=apply)
        apply.assert_not_called()
        self.assertFalse(self.override.exists())

    def test_valid_edit_is_applied_once_with_saved_value(self):
        """Apply sees the new value through the normal configuration read."""
        seen = []
        self.edit('{"level": 7, "name": "x"}', apply=lambda: seen.append(self.read()["level"]))
        self.assertEqual(seen, [7])

    def test_failed_apply_restores_previous_state(self):
        """Apply failure restores earlier overrides, or their absence."""
        apply = Mock(side_effect=ValueError("apply failed"))
        with self.assertRaisesRegex(ValueError, "apply failed"):
            self.edit('{"level": 2, "name": "x"}', apply=apply)
        self.assertFalse(self.override.exists())
        self.edit('{"level": 3, "name": "x"}')
        with self.assertRaisesRegex(ValueError, "apply failed"):
            self.edit('{"level": 4, "name": "x"}', apply=apply)
        self.assertEqual(self.read()["level"], 3)

    def test_override_symlink_rejected(self):
        """A local override must not follow a symlink."""
        self.override.parent.mkdir(parents=True)
        self.override.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.read()


if __name__ == "__main__":
    unittest.main()
