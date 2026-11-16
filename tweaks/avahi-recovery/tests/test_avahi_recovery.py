"""Offline tests: no host changes, service starts, or network requests."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "recovery", Path(__file__).parents[1] / "tweak.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

DEFAULTS = {"attempts": 3, "window_seconds": 600, "delay_seconds": 20}


class ConfigTests(unittest.TestCase):
    def test_versioned_defaults_are_valid(self):
        """The file shipped in Git passes the tweak's own validation."""
        recovery.validate_config(json.loads((Path(recovery.__file__).with_name("config.json")).read_text()))

    def test_config_rejects_unknown_keys(self):
        """Unknown settings must fail instead of being silently ignored."""
        with self.assertRaises(ValueError):
            recovery.validate_config({**DEFAULTS, "interfaces": "veth*"})

    def test_config_bounds(self):
        """Reject booleans, excessive retries and unsafe cooldown values."""
        for key, value in (("attempts", True), ("attempts", 4),
                           ("delay_seconds", 0), ("window_seconds", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                recovery.validate_config({**DEFAULTS, key: value})

    def test_limit_can_trigger_within_window(self):
        """All allowed restarts fit in the smallest window, so the limit stays reachable."""
        attempts = recovery.BOUNDS["attempts"][1]
        delay = recovery.BOUNDS["delay_seconds"][1]
        self.assertLess(attempts * delay, recovery.BOUNDS["window_seconds"][0])

    def test_dropin_maps_settings_to_systemd(self):
        """Settings become a restart policy limited to unclean exits."""
        text = recovery.render_dropin(DEFAULTS).decode()
        self.assertTrue(text.startswith(recovery.MARKER))
        for line in ("StartLimitIntervalSec=600", "StartLimitBurst=3",
                     "Restart=on-failure", "RestartSec=20"):
            self.assertIn(line + "\n", text)

    def test_non_linux_rejected(self):
        """Installation cannot modify a macOS workstation."""
        with patch.object(recovery.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "TrueNAS"):
                recovery.check_host()

    def test_wrong_version_rejected(self):
        """Installation refuses an unreviewed TrueNAS release before systemctl."""
        with patch.object(recovery.sys, "platform", "linux"), \
             patch.object(recovery.os, "geteuid", return_value=0), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", return_value="TrueNAS-27.0.0"), \
             patch.object(recovery, "systemctl") as ctl:
            with self.assertRaisesRegex(ValueError, "Unsupported version"):
                recovery.check_host(install=True)
            ctl.assert_not_called()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        root = Path(self.temp.name)
        self.units = root / "units"
        self.units.mkdir()
        self.dropin = self.units / recovery.DROPIN
        patch.object(recovery, "UNITS", self.units).start()
        patch.object(recovery, "PERSISTENT", root / "etc").start()
        patch.object(recovery, "check_host").start()
        patch.object(recovery.common, "secure_path").start()
        patch.object(recovery.common, "read_config", return_value=DEFAULTS).start()
        self.properties = {"DropInPaths": str(self.dropin), "Restart": "on-failure",
                           "StartLimitBurst": "3", "ActiveState": "active"}
        self.ctl = patch.object(recovery, "systemctl", side_effect=self.systemctl).start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def systemctl(self, *args):
        if args[0] == "show":
            return self.properties[args[2].split("=")[1]]
        return ""

    def commands(self):
        return [call.args for call in self.ctl.call_args_list if call.args[0] != "show"]

    def test_install_uninstall_are_idempotent(self):
        """Repeated runs keep one drop-in, preserve others and never restart a healthy daemon."""
        recovery.install()
        unrelated = self.dropin.with_name("other.conf")
        unrelated.write_text("# another tool\n")
        recovery.install()
        self.assertEqual(self.dropin.read_bytes(), recovery.render_dropin(DEFAULTS))
        recovery.uninstall()
        recovery.uninstall()
        self.assertFalse(self.dropin.exists())
        self.assertEqual(unrelated.read_text(), "# another tool\n")
        self.assertFalse(any(command[0] in ("start", "restart", "reset-failed")
                             for command in self.commands()))

    def test_already_failed_daemon_is_started_within_limit(self):
        """A daemon that failed before boot restore gets a start without clearing the limit."""
        self.properties["ActiveState"] = "failed"
        recovery.install()
        self.assertIn(("start", "--no-block", recovery.TARGET), self.commands())
        self.assertNotIn(("reset-failed", recovery.TARGET), self.commands())

    def test_stopped_daemon_is_not_started(self):
        """An intentionally stopped daemon stays stopped."""
        self.properties["ActiveState"] = "inactive"
        recovery.install()
        self.assertNotIn(("start", "--no-block", recovery.TARGET), self.commands())

    def test_overridden_policy_rolls_back(self):
        """If another drop-in wins, installation restores the previous state."""
        for key, value in (("Restart", "no"), ("StartLimitBurst", "5"), ("DropInPaths", "")):
            with self.subTest(key=key):
                previous = self.properties[key]
                self.properties[key] = value
                with self.assertRaisesRegex(ValueError, "overridden"):
                    recovery.install()
                self.assertFalse(self.dropin.exists())
                self.properties[key] = previous

    def test_failed_start_restores_previous_dropin(self):
        """A failed reinstall returns the earlier settings rather than removing protection."""
        self.dropin.parent.mkdir()
        self.dropin.write_bytes(recovery.MARKER.encode() + b"# previous\n")
        self.properties["ActiveState"] = "failed"
        self.ctl.side_effect = lambda *args: (
            self.systemctl(*args) if args[0] != "start" else (_ for _ in ()).throw(OSError("unavailable")))
        with self.assertRaisesRegex(OSError, "unavailable"):
            recovery.install()
        self.assertEqual(self.dropin.read_bytes(), recovery.MARKER.encode() + b"# previous\n")

    def test_unmanaged_dropin_is_preserved(self):
        """An unrelated file at the reserved path blocks both install and uninstall."""
        self.dropin.parent.mkdir()
        self.dropin.write_text("# user owned\n")
        for operation in (recovery.install, recovery.uninstall):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    operation()
                self.assertEqual(self.dropin.read_text(), "# user owned\n")

    def test_persistent_conflict_is_refused(self):
        """A persistent drop-in with our name would shadow the runtime file."""
        conflict = recovery.PERSISTENT / recovery.DROPIN
        conflict.parent.mkdir(parents=True)
        conflict.write_text("# persistent\n")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            recovery.install()
        self.assertFalse(self.dropin.exists())

    def test_status_reports_effective_policy(self):
        """Status separates missing, ineffective and working installations."""
        with patch.object(recovery.sys, "platform", "linux"), \
             patch.object(recovery, "SYSTEMCTL", __file__):
            self.assertEqual(recovery.status(), "not installed for this boot")
            self.dropin.parent.mkdir()
            self.dropin.write_bytes(recovery.render_dropin(DEFAULTS))
            self.properties.update(Result="start-limit-hit", NRestarts="3", ActiveState="failed")
            self.assertEqual(recovery.status(),
                             "installed; Avahi=failed; result=start-limit-hit; restarts=3")
            self.properties["Restart"] = "no"
            self.assertIn("not effective", recovery.status())

    def test_status_on_workstation_is_read_only(self):
        """Status explains the missing runtime without invoking systemctl on macOS."""
        with patch.object(recovery.sys, "platform", "darwin"):
            self.assertIn("runtime unavailable", recovery.status())
        self.ctl.assert_not_called()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(recovery, "check_host").start()
        self.enabled = patch.object(recovery.common, "read_enabled", return_value=[]).start()
        self.install = patch.object(recovery, "install").start()

    def test_apply_and_boot_require_activation(self):
        """Neither direct apply nor boot may install a disabled tweak."""
        for action in ("apply", "boot"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "use attic"):
                    recovery.main([action])
                self.install.assert_not_called()

    def test_boot_and_apply_restore_enabled_tweak(self):
        """Enabled Avahi is restored through its own install operation."""
        self.enabled.return_value = [recovery.NAME]
        for action in ("apply", "boot"):
            recovery.main([action])
        self.assertEqual(self.install.call_count, 2)

    def test_config_applies_only_when_enabled(self):
        """Config edits a disabled tweak without activating it."""
        for names, expected in (([], None), ([recovery.NAME], recovery.install)):
            with self.subTest(names=names), patch.object(recovery.common, "edit_config") as edit:
                self.enabled.return_value = names
                recovery.main(["config"])
                edit.assert_called_once_with(recovery.ROOT, recovery.NAME,
                                             recovery.validate_config, apply=expected)

    def test_help_does_not_check_host_or_install(self):
        """Per-command help works on a workstation without changing the system."""
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                recovery.main(["install", "--help"])
        self.assertEqual(result.exception.code, 0)
        recovery.check_host.assert_not_called()
        self.install.assert_not_called()

    def test_unknown_command_returns_parser_error(self):
        """The tweak itself rejects unknown commands before any host operations."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                recovery.main(["unknown"])
        self.assertEqual(result.exception.code, 2)
        recovery.check_host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
