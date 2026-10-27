"""Test CLI dispatch with temporary scripts; never invoke host services or the network."""

import contextlib
from importlib.machinery import SourceFileLoader
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LOADER = SourceFileLoader("attic", str(Path(__file__).parents[1] / "attic"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
attic = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(attic)


class AtticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "local").mkdir()
        self.addCleanup(patch.stopall)
        patch.object(attic, "ROOT", self.root).start()

    def stub_management(self):
        """Keep real state persistence while replacing privileges and child processes."""
        patch.object(attic, "management_lock", contextlib.nullcontext).start()
        patch.object(attic, "secure_path").start()
        patch.object(attic, "tweak_path", return_value=self.root / "tweak.py").start()
        self.run = patch.object(attic, "run_tweak", return_value=0).start()

    def test_discovers_new_scripts_without_registry(self):
        """A new CLI is found without importing it or requiring Python functions."""
        folder = self.root / "tweaks/example"
        folder.mkdir(parents=True)
        (folder / "tweak.py").write_text('raise RuntimeError("do not import")')
        self.assertEqual(attic.discover(), ["example"])
        self.assertEqual(attic.tweak_path("example"), folder / "tweak.py")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(attic.main(["list"]), 0)
        self.assertEqual(output.getvalue().strip(), "example")

    def test_reserved_names_are_not_discovered(self):
        """A tweak cannot shadow the three global commands."""
        folder = self.root / "tweaks/boot"
        folder.mkdir(parents=True)
        (folder / "tweak.py").touch()
        self.assertEqual(attic.discover(), [])

    def test_path_traversal_rejected(self):
        """User-supplied names cannot escape the tweaks directory."""
        with self.assertRaisesRegex(ValueError, "Unknown tweak"):
            attic.tweak_path("../../example")

    def test_run_uses_argument_list_and_inherits_streams(self):
        """Spaces, options and shell metacharacters remain literal child arguments."""
        path = self.root / "tweak.py"
        args = ["custom", "--label", "two words", "$(touch nope)", "--", "-h"]
        with patch.object(attic, "tweak_path", return_value=path), \
             patch.object(attic.subprocess, "run") as run:
            run.return_value.returncode = 17
            self.assertEqual(attic.run_tweak("example", args), 17)
        run.assert_called_once_with([sys.executable, "-B", str(path), *args])

    def test_real_child_receives_arguments_and_returns_exit_code(self):
        """A real offline fixture CLI receives unchanged argv and its exit code propagates."""
        folder = self.root / "tweaks/example"
        folder.mkdir(parents=True)
        output = self.root / "args.json"
        (folder / "tweak.py").write_text(
            'import json, pathlib, sys\n'
            f'pathlib.Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n'
            'sys.exit(7)\n')
        args = ["custom", "--value", "a b", "--", "--unknown"]
        self.assertEqual(attic.run_tweak("example", args), 7)
        self.assertEqual(json.loads(output.read_text()), args)

    def test_child_signal_maps_to_shell_exit_code(self):
        """A child killed by a signal does not appear successful to callers."""
        with patch.object(attic, "tweak_path", return_value=Path("tweak.py")), \
             patch.object(attic.subprocess, "run") as run:
            run.return_value.returncode = -15
            self.assertEqual(attic.run_tweak("example", ["custom"]), 143)

    def test_custom_command_is_forwarded_without_registry(self):
        """The dispatcher forwards a command it has never heard of."""
        self.stub_management()
        self.run.return_value = 9
        self.assertEqual(attic.main(["example", "brightness", "50", "--fade", "2"]), 9)
        self.run.assert_called_once_with("example", ["brightness", "50", "--fade", "2"])
        self.assertEqual(attic.read_enabled(), [])

    def test_help_does_not_change_activation_or_require_management(self):
        """Install/uninstall help and bare tweak help bypass all activation writes."""
        self.stub_management()
        attic.write_enabled(["example"])
        with patch.object(attic, "management_lock", side_effect=AssertionError("mutation")):
            for args in (["install", "--help"], ["uninstall", "-h"], ["--help"], []):
                self.assertEqual(attic.main(["example", *args]), 0)
                self.run.assert_called_with("example", args)
        self.assertEqual(attic.read_enabled(), ["example"])

    def test_install_is_idempotent_and_persistent(self):
        """Repeated successful installs retain exactly one activation record."""
        self.stub_management()
        attic.main(["example", "install"])
        attic.main(["example", "install"])
        self.assertEqual(attic.read_enabled(), ["example"])
        self.assertEqual(self.run.call_count, 2)

    def test_failed_install_does_not_enable(self):
        """A nonzero child exit propagates without enabling the tweak."""
        self.stub_management()
        self.run.return_value = 4
        self.assertEqual(attic.main(["example", "install"]), 4)
        self.assertEqual(attic.read_enabled(), [])

    def test_activation_write_failure_reports_partial_install(self):
        """State failure after a successful child reports the partial installation."""
        self.stub_management()
        with patch.object(attic, "write_enabled", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "runtime installed"):
                attic.main(["example", "install"])
        self.assertEqual(attic.read_enabled(), [])

    def test_failed_reinstall_preserves_existing_activation(self):
        """An enabled tweak stays selected when its update fails."""
        self.stub_management()
        attic.write_enabled(["example"])
        self.run.return_value = 1
        self.assertEqual(attic.main(["example", "install"]), 1)
        self.assertEqual(attic.read_enabled(), ["example"])

    def test_uninstall_disables_before_cleanup(self):
        """Failed cleanup cannot restore the tweak on the next boot."""
        self.stub_management()
        attic.write_enabled(["example", "other"])
        def child(name, args):
            self.assertEqual(attic.read_enabled(), ["other"])
            return 8
        self.run.side_effect = child
        self.assertEqual(attic.main(["example", "uninstall"]), 8)
        self.assertEqual(attic.read_enabled(), ["other"])

    def test_repeated_uninstall_is_allowed(self):
        """Cleanup can be repeated for an already disabled tweak."""
        self.stub_management()
        attic.main(["example", "uninstall"])
        attic.main(["example", "uninstall"])
        self.assertEqual(attic.read_enabled(), [])
        self.assertEqual(self.run.call_count, 2)

    def test_config_is_delegated_without_parameter_knowledge(self):
        """The module receives its own config arguments without injected settings."""
        self.stub_management()
        attic.main(["example", "config", "--custom-option", "value"])
        self.run.assert_called_once_with("example", ["config", "--custom-option", "value"])

    def test_boot_restores_only_enabled_scripts(self):
        """Global boot calls the module's boot command and preserves activation."""
        self.stub_management()
        attic.write_enabled(["example"])
        self.assertEqual(attic.main(["boot"]), 0)
        self.run.assert_called_once_with("example", ["boot"])
        self.assertEqual(attic.read_enabled(), ["example"])

    def test_boot_without_state_does_nothing(self):
        """An initial checkout never runs any tweak at boot."""
        self.stub_management()
        self.assertEqual(attic.main(["boot"]), 0)
        self.run.assert_not_called()

    def test_boot_continues_after_child_failure(self):
        """A failed child does not skip the next tweak; the global exit is nonzero."""
        self.stub_management()
        attic.write_enabled(["first", "second"])
        self.run.side_effect = [3, 0]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(attic.main(["boot"]), 1)
        self.assertEqual(self.run.call_count, 2)

    def test_boot_continues_after_launch_failure(self):
        """Missing or unlaunchable scripts do not prevent other enabled tweaks booting."""
        self.stub_management()
        attic.write_enabled(["first", "second"])
        self.run.side_effect = [OSError("missing script"), 0]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(attic.main(["boot"]), 1)
        self.assertEqual(self.run.call_count, 2)

    def test_corrupt_state_fails_closed(self):
        """Malformed activation state cannot trigger operations."""
        for value in ({}, ["../bad"], ["a", "a"], [1]):
            with self.subTest(value=value):
                (self.root / "local/enabled.json").write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    attic.read_enabled()

    def test_state_symlink_rejected(self):
        """Activation must not follow a symlink, including a dangling one."""
        (self.root / "local/enabled.json").symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            attic.read_enabled()

    def test_global_status_reports_activation_and_runs_children(self):
        """Status reports selection independently and returns nonzero for child failure."""
        self.stub_management()
        attic.write_enabled(["example"])
        self.run.return_value = 6
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(attic.main(["status"]), 1)
        self.assertIn("example: enabled", output.getvalue())
        self.run.assert_called_once_with("example", ["status"])

    def test_management_rejected_on_workstation(self):
        """A mutation on macOS fails before spawning a child or writing state."""
        with patch.object(attic.sys, "platform", "darwin"), \
             patch.object(attic, "tweak_path"), patch.object(attic, "run_tweak") as run:
            with self.assertRaisesRegex(ValueError, "NAS"):
                attic.main(["example", "install"])
            run.assert_not_called()
        self.assertFalse((self.root / "local/enabled.json").exists())


if __name__ == "__main__":
    unittest.main()
