"""Offline tests: no host changes, service starts, or network requests."""

import importlib.util
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


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(recovery.DEFAULTS))

    def test_config_rejects_unknown_keys(self):
        """Unknown settings must fail instead of being silently ignored."""
        self.config.write_text('{"interfaces": "veth*"}')
        with self.assertRaises(ValueError):
            recovery.read_config(self.config)

    def test_config_bounds(self):
        """Reject booleans, excessive retries and unsafe cooldown values."""
        for key, value in (("attempts", True), ("attempts", 4),
                           ("delay_seconds", 0), ("window_seconds", 1)):
            with self.subTest(key=key, value=value):
                self.config.write_text(json.dumps({**recovery.DEFAULTS, key: value}))
                with self.assertRaises(ValueError):
                    recovery.read_config(self.config)

    def test_budget_persists_across_invocations_and_expires(self):
        """Separate handler runs share a rolling window, which eventually expires."""
        path = self.runtime / "attempts.json"
        with patch.object(recovery, "secure_path"):
            for now in (1, 2, 3):
                self.assertTrue(recovery.claim_attempt(path, now, recovery.DEFAULTS))
            self.assertFalse(recovery.claim_attempt(path, 4, recovery.DEFAULTS))
            self.assertTrue(recovery.claim_attempt(path, 604, recovery.DEFAULTS))

    def test_corrupt_budget_fails_closed(self):
        """Malformed history cannot reset the restart budget."""
        path = self.runtime / "attempts.json"
        path.write_text('{"invalid": 1}')
        with patch.object(recovery, "secure_path"), self.assertRaises(ValueError):
            recovery.claim_attempt(path, 1, recovery.DEFAULTS)

    def test_symlink_rejected(self):
        """A managed path must not follow an attacker-supplied symlink."""
        link = self.root / "link"
        link.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            recovery.secure_path(link)

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

    def test_stop_or_shutdown_never_recovers(self):
        """An inactive daemon and a stopping manager both suppress recovery."""
        for manager, daemon in (("running", "inactive"), ("stopping", "failed")):
            with self.subTest(manager=manager), \
                 patch.object(recovery, "systemctl", return_value=manager), \
                 patch.object(recovery, "state", return_value=daemon):
                self.assertFalse(recovery.can_recover())

    def run_recover(self, eligibility, states, budget=True):
        """Run the handler with explicit service states and no real sleeping."""
        with patch.object(recovery, "RUNTIME", self.runtime), \
             patch.object(recovery, "check_host"), \
             patch.object(recovery, "secure_path"), \
             patch.object(recovery, "read_config", return_value=recovery.DEFAULTS), \
             patch.object(recovery, "can_recover", side_effect=eligibility), \
             patch.object(recovery, "state", side_effect=states), \
             patch.object(recovery, "claim_attempt", return_value=budget), \
             patch.object(recovery.time, "sleep"), \
             patch.object(recovery, "systemctl") as ctl:
            try:
                recovery.recover()
            except ValueError:
                self.commands = ctl.call_args_list
                raise
            self.commands = ctl.call_args_list

    def test_stop_during_delay(self):
        """An administrator stopping Avahi during cooldown cancels recovery."""
        self.run_recover([True, False], [])
        self.assertEqual(self.commands, [])

    def test_success_uses_nonblocking_start(self):
        """A successful recovery queues a start without waiting on systemd jobs."""
        self.run_recover([True, True], ["activating", "active"])
        self.assertEqual([call.args for call in self.commands], [
            ("reset-failed", recovery.TARGET), ("start", "--no-block", recovery.TARGET)])

    def test_repeated_failure_is_bounded(self):
        """Immediate failures retry inside the handler but stop at three attempts."""
        with self.assertRaisesRegex(ValueError, "attempts exhausted"):
            self.run_recover([True] * 6, ["failed"] * 3)
        self.assertEqual(len(self.commands), 6)

    def test_budget_exhaustion_does_not_start(self):
        """An exhausted shared budget prevents even reset-failed."""
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.run_recover([True, True], [], budget=False)
        self.assertEqual(self.commands, [])

    def test_stuck_activation_is_bounded(self):
        """A daemon that never finishes activation fails without repeated starts."""
        with self.assertRaisesRegex(ValueError, "did not settle"):
            self.run_recover([True, True], ["activating"] * 15)
        self.assertEqual(len(self.commands), 2)

    def install_context(self):
        """Prepare a temporary filesystem and stub only external safety checks."""
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(recovery, "RUNTIME", self.runtime))
        stack.enter_context(patch.object(recovery, "UNITS", self.root / "units"))
        stack.enter_context(patch.object(recovery, "check_host"))
        stack.enter_context(patch.object(recovery, "secure_path"))
        # The source must appear under /mnt without reading or writing that tree.
        stack.enter_context(patch.object(recovery, "__file__", "/mnt/attic/avahi_recovery.py"))
        original = Path.read_bytes
        stack.enter_context(patch.object(Path, "read_bytes", lambda path: b"# test script\n"
                                        if str(path) == recovery.__file__ else original(path)))
        stack.enter_context(patch.object(recovery, "read_config", return_value=recovery.DEFAULTS))
        stack.enter_context(patch.object(recovery, "state", return_value="active"))
        def ctl(*args):
            if "--property=OnFailure" in args:
                return recovery.HANDLER
            if "--property=FragmentPath" in args:
                return str(recovery.UNITS / recovery.HANDLER)
            return ""
        mock = stack.enter_context(patch.object(recovery, "systemctl", side_effect=ctl))
        return stack, mock

    def test_install_uninstall_are_idempotent(self):
        """Repeated installs preserve budget; repeated removals preserve other drop-ins."""
        stack, ctl = self.install_context()
        with stack:
            recovery.install(Path("/mnt/attic/recovery.json"))
            budget = self.runtime / "attempts.json"
            budget.write_text("[123]")
            unrelated = recovery.UNITS / f"{recovery.TARGET}.d/other.conf"
            unrelated.write_text("# another tool\n")
            recovery.install(Path("/mnt/attic/recovery.json"))
            self.assertEqual(budget.read_text(), "[123]")
            self.assertTrue(all(path.exists() for path in recovery.unit_paths()))
            recovery.uninstall()
            recovery.uninstall()
            self.assertFalse(any(path.exists() for path in recovery.unit_paths()))
            self.assertEqual(unrelated.read_text(), "# another tool\n")
            self.assertEqual(budget.read_text(), "[123]")
            self.assertFalse(any(call.args == ("restart", recovery.TARGET)
                                 for call in ctl.call_args_list))

    def test_install_rolls_back_failed_verification(self):
        """If systemd ignores the hook, installation restores previous files."""
        stack, ctl = self.install_context()
        with stack:
            ctl.side_effect = lambda *args: ""
            with self.assertRaisesRegex(ValueError, "overridden"):
                recovery.install(Path("/mnt/attic/recovery.json"))
            self.assertFalse(any(path.exists() for path in recovery.unit_paths()))
            self.assertFalse((self.runtime / "config.json").exists())

    def test_failed_recovery_queue_rolls_back_install(self):
        """Failure to enqueue recovery restores the previous runtime installation."""
        stack, ctl = self.install_context()
        with stack:
            original = ctl.side_effect
            def fail_start(*args):
                if args[0] == "start":
                    raise OSError("systemctl unavailable")
                return original(*args)
            ctl.side_effect = fail_start
            with patch.object(recovery, "state", return_value="failed"):
                with self.assertRaisesRegex(OSError, "unavailable"):
                    recovery.install(Path("/mnt/attic/recovery.json"))
            self.assertFalse(any(path.exists() for path in recovery.unit_paths()))
            self.assertFalse((self.runtime / "config.json").exists())

    def test_unmanaged_unit_is_preserved(self):
        """An unrelated file at a reserved unit path blocks installation."""
        stack, _ = self.install_context()
        with stack:
            recovery.UNITS.mkdir()
            unit = recovery.UNITS / recovery.HANDLER
            unit.write_text("# user owned\n")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                recovery.install(Path("/mnt/attic/recovery.json"))
            self.assertEqual(unit.read_text(), "# user owned\n")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "avahi-recovery.json"
        self.original = json.dumps(recovery.DEFAULTS)
        self.config.write_text(self.original)

    def configure(self, edited, enabled=True, apply_error=None):
        """Replace the editor and host operations while retaining real JSON validation."""
        def edit(command, **kwargs):
            Path(command[-1]).write_text(edited)
        with patch.object(recovery, "check_host"), \
             patch.object(recovery, "secure_path"), \
             patch.object(recovery.subprocess, "run", side_effect=edit), \
             patch.object(recovery, "apply", side_effect=apply_error) as apply:
            try:
                recovery.configure(self.config, enabled=enabled)
            finally:
                self.apply_calls = apply.call_args_list

    def test_invalid_edit_keeps_saved_and_live_config(self):
        """Invalid JSON settings never replace the saved file or invoke apply."""
        with self.assertRaises(ValueError):
            self.configure('{"attempts": 100}')
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(self.apply_calls, [])
        self.assertEqual(list(self.config.parent.glob(".avahi-edit-*")), [])

    def test_enabled_configuration_applies_valid_edit(self):
        """A valid edit to an enabled tweak is saved and applied exactly once."""
        edited = json.dumps({**recovery.DEFAULTS, "delay_seconds": 30})
        self.configure(edited)
        self.assertEqual(self.config.read_text(), edited)
        self.assertEqual(len(self.apply_calls), 1)
        self.assertEqual(self.apply_calls[0].args, (self.config,))

    def test_disabled_configuration_only_saves(self):
        """Editing a disabled tweak does not install or enable it."""
        edited = json.dumps({**recovery.DEFAULTS, "delay_seconds": 30})
        self.configure(edited, enabled=False)
        self.assertEqual(self.config.read_text(), edited)
        self.assertEqual(self.apply_calls, [])

    def test_failed_apply_restores_saved_config(self):
        """An apply failure restores the previous persistent configuration."""
        edited = json.dumps({**recovery.DEFAULTS, "delay_seconds": 30})
        with self.assertRaisesRegex(ValueError, "apply failed"):
            self.configure(edited, apply_error=ValueError("apply failed"))
        self.assertEqual(self.config.read_text(), self.original)

    def test_unchanged_configuration_does_not_apply(self):
        """Closing the editor without changes performs no runtime writes."""
        self.configure(self.original)
        self.assertEqual(self.apply_calls, [])

    def test_status_on_workstation_is_read_only(self):
        """Status explains the missing runtime without invoking systemctl on macOS."""
        with patch.object(recovery.sys, "platform", "darwin"), \
             patch.object(recovery, "systemctl") as ctl:
            self.assertIn("runtime unavailable", recovery.status())
            ctl.assert_not_called()

    def test_status_detects_incomplete_runtime(self):
        """A remaining unit without the runtime payload is not reported as installed."""
        root = self.config.parent
        with patch.object(recovery.sys, "platform", "linux"), \
             patch.object(recovery, "SYSTEMCTL", str(self.config)), \
             patch.object(recovery, "RUNTIME", root / "runtime"), \
             patch.object(recovery, "UNITS", root), \
             patch.object(recovery, "systemctl") as ctl:
            (root / recovery.HANDLER).write_text(recovery.SERVICE)
            self.assertEqual(recovery.status(), "incomplete runtime installation")
            ctl.assert_not_called()


class CLITests(unittest.TestCase):
    def test_install_uses_config_beside_script(self):
        """The independent CLI resolves its working configuration without dispatcher input."""
        with patch.object(recovery, "check_host"), patch.object(recovery, "install") as install:
            recovery.main(["install"])
        install.assert_called_once_with(Path(recovery.__file__).absolute().with_name("config.json"))

    def test_apply_and_boot_require_activation(self):
        """Neither direct apply nor boot may install a disabled tweak."""
        for action in ("apply", "boot"):
            with self.subTest(action=action), patch.object(recovery, "check_host"), \
                 patch.object(recovery, "is_enabled", return_value=False), \
                 patch.object(recovery, "apply") as apply:
                with self.assertRaisesRegex(ValueError, "use attic"):
                    recovery.main([action])
                apply.assert_not_called()

    def test_boot_applies_enabled_configuration(self):
        """Boot restores enabled Avahi through its own apply operation."""
        with patch.object(recovery, "check_host"), \
             patch.object(recovery, "is_enabled", return_value=True), \
             patch.object(recovery, "apply") as apply:
            recovery.main(["boot"])
        apply.assert_called_once_with(Path(recovery.__file__).absolute().with_name("config.json"))

    def test_config_passes_activation_to_editor(self):
        """Config edits a disabled tweak without activating it."""
        with patch.object(recovery, "check_host"), \
             patch.object(recovery, "is_enabled", return_value=False), \
             patch.object(recovery, "configure") as configure:
            recovery.main(["config"])
        configure.assert_called_once_with(
            Path(recovery.__file__).absolute().with_name("config.json"), enabled=False)

    def test_help_does_not_check_host_or_install(self):
        """Per-command help works on a workstation without changing the system."""
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(recovery, "check_host") as host, \
             patch.object(recovery, "install") as install:
            with self.assertRaises(SystemExit) as result:
                recovery.main(["install", "--help"])
        self.assertEqual(result.exception.code, 0)
        host.assert_not_called()
        install.assert_not_called()

    def test_unknown_command_returns_parser_error(self):
        """The tweak itself rejects unknown commands before any host operations."""
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), \
             patch.object(recovery, "check_host") as host:
            with self.assertRaises(SystemExit) as result:
                recovery.main(["unknown"])
        self.assertEqual(result.exception.code, 2)
        host.assert_not_called()

    def test_activation_reads_current_checkout_and_rejects_corruption(self):
        """Activation comes from local/enabled.json and malformed state fails closed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local").mkdir()
            path = root / "local/enabled.json"
            with patch.object(recovery, "__file__", str(root / "tweaks/avahi-recovery/tweak.py")), \
                 patch.object(recovery, "secure_path"):
                self.assertFalse(recovery.is_enabled())
                path.write_text('["avahi-recovery"]')
                self.assertTrue(recovery.is_enabled())
                for invalid in ('{}', '["../bad"]', '["a", "a"]'):
                    path.write_text(invalid)
                    with self.assertRaises(ValueError):
                        recovery.is_enabled()

    def test_runtime_recover_keeps_its_original_entrypoint(self):
        """The existing systemd command remains valid without reading persistent state."""
        with patch.object(recovery, "recover") as recover, \
             patch.object(recovery, "is_enabled") as enabled:
            recovery.main(["recover"])
        recover.assert_called_once_with()
        enabled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
