"""Offline setup tests with in-memory middleware and temporary local state."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("attic_setup", Path(__file__).parents[1] / "lib/attic_setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "local").mkdir()
        self.dispatcher = setup.load_dispatcher()
        self.addCleanup(patch.stopall)
        patch.object(setup, "ROOT", self.root).start()
        patch.object(self.dispatcher, "ROOT", self.root).start()
        self.run = patch.object(self.dispatcher, "run_tweak", return_value=0).start()
        self.tasks = []
        self.mutations = []
        self.api = patch.object(setup, "call", side_effect=self.middleware).start()

    def middleware(self, method, *args):
        """Simulate only the CRUD calls used by setup; never execute a real service."""
        action = method.split('.')[-1]
        if action == 'query':
            return copy.deepcopy(self.tasks)
        self.mutations.append((action, args))
        if action == 'create':
            task = {**args[0], 'id': 42}
            self.tasks.append(task)
            return copy.deepcopy(task)
        task = next(task for task in self.tasks if task['id'] == args[0])
        if action == 'update':
            task.update(args[1])
            return copy.deepcopy(task)
        if action == 'delete':
            self.tasks.remove(task)
            return True
        raise AssertionError(method)

    def add_tweaks(self, *names):
        for name in names:
            folder = self.root / "tweaks" / name
            folder.mkdir(parents=True)
            (folder / "tweak.py").touch()

    def managed_task(self, **overrides):
        return dict(id=42, type='COMMAND', when='POSTINIT', command=setup.boot_command(),
                    enabled=True, timeout=120, comment=setup.MARKER) | overrides

    def test_install_creates_one_task_and_enables_no_tweaks(self):
        """Repeated setup reuses the same boot task without running or enabling tweaks."""
        # A real NAS checkout must reside under /mnt; use that only in task commands.
        with patch.object(setup, "boot_command", return_value='/usr/bin/python3 /mnt/pool/attic-config/attic boot'):
            setup.install(self.dispatcher, None)
            setup.install(self.dispatcher, None)
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual([action for action, _ in self.mutations], ['create'])
        self.assertEqual(self.dispatcher.read_enabled(), [])
        self.run.assert_not_called()

    def test_install_preserves_selection_and_existing_timeout(self):
        """Setup keeps selected tweaks and a user-adjusted timeout on repeated runs."""
        self.dispatcher.write_enabled(['example'])
        command='/usr/bin/python3 /mnt/pool/attic-config/attic boot'
        self.tasks = [self.managed_task(command=command, timeout=300)]
        with patch.object(setup, "boot_command", return_value=command):
            setup.install(self.dispatcher, None)
        self.assertEqual(self.dispatcher.read_enabled(), ['example'])
        self.assertEqual(self.tasks[0]['timeout'], 300)
        self.assertEqual(self.mutations, [])

    def test_install_adopts_exact_manual_task(self):
        """An existing manual Post Init for this checkout is reused, not duplicated."""
        with patch.object(setup, 'ROOT', Path('/mnt/pool/attic-config')):
            self.tasks = [self.managed_task(comment='my manual boot')]
            setup.install(self.dispatcher, 180)
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.tasks[0]['comment'], setup.MARKER)
        self.assertEqual(self.tasks[0]['timeout'], 180)
        self.assertEqual(self.mutations[0][0], 'update')

    def test_move_updates_existing_managed_command(self):
        """Moving the checkout updates the owned task without creating another."""
        self.tasks = [self.managed_task(command='/usr/bin/python3 /mnt/old/attic boot')]
        with patch.object(setup, 'ROOT', Path('/mnt/new/attic-config')):
            setup.install(self.dispatcher, None)
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(shlex.split(self.tasks[0]['command'])[1], '/mnt/new/attic-config/attic')

    def test_duplicates_refused_without_mutations(self):
        """Two managed tasks require manual resolution instead of arbitrary deletion."""
        self.tasks = [self.managed_task(), self.managed_task(id=43)]
        with self.assertRaisesRegex(ValueError, 'Multiple'):
            setup.install(self.dispatcher, None)
        self.assertEqual(self.mutations, [])

    def test_changed_owned_command_is_not_overwritten(self):
        """A marker alone cannot authorize replacing an unexpected command."""
        self.tasks = [self.managed_task(command='/usr/bin/other-program')]
        with self.assertRaisesRegex(ValueError, 'changed unexpectedly'):
            setup.install(self.dispatcher, None)
        self.assertEqual(self.mutations, [])

    def test_foreign_tasks_are_preserved(self):
        """Unrelated tasks survive registration of the new boot task unchanged."""
        foreign = self.managed_task(id=9, command='/usr/bin/true', comment='other task')
        self.tasks = [foreign.copy()]
        with patch.object(setup, "boot_command", return_value='/usr/bin/python3 /mnt/pool/attic boot'):
            setup.install(self.dispatcher, None)
        self.assertEqual(self.tasks[0], foreign)

    def test_api_failure_is_not_reported_as_success(self):
        """A failed middleware create propagates without changing local activation."""
        self.api.side_effect = [[], subprocess.TimeoutExpired('midclt', 60)]
        with self.assertRaises(subprocess.TimeoutExpired):
            setup.install(self.dispatcher, None)
        self.assertEqual(self.dispatcher.read_enabled(), [])

    def test_post_write_verification_detects_missing_task(self):
        """Setup fails if middleware does not return the created task on reread."""
        self.api.side_effect = [[], self.managed_task(), []]
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            setup.install(self.dispatcher, None)

    def test_uninstall_is_repeatable_and_preserves_configs(self):
        """Setup removes enabled tweaks and its task while retaining source files."""
        self.add_tweaks('example')
        self.dispatcher.write_enabled(['example'])
        config=self.root/'config.json'
        config.write_text('{}')
        with patch.object(setup, 'ROOT', Path('/mnt/pool/attic-config')):
            self.tasks = [self.managed_task()]
            setup.uninstall(self.dispatcher)
            setup.uninstall(self.dispatcher)
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.dispatcher.read_enabled(), [])
        self.run.assert_called_once_with('example', ['uninstall'])
        self.assertEqual(config.read_text(), '{}')

    def test_failed_cleanup_keeps_disabled_task_and_retry_selection(self):
        """Failed cleanup remains retryable while successful tweaks are deselected."""
        self.add_tweaks('first', 'second')
        self.dispatcher.write_enabled(['first', 'second'])
        self.run.side_effect=[8, 0]
        with patch.object(setup, 'ROOT', Path('/mnt/pool/attic-config')):
            self.tasks=[self.managed_task()]
            with self.assertRaisesRegex(ValueError, 'Cleanup failed'):
                setup.uninstall(self.dispatcher)
        self.assertFalse(self.tasks[0]['enabled'])
        self.assertEqual(self.dispatcher.read_enabled(), ['first'])
        self.assertFalse(any(action == 'delete' for action, _ in self.mutations))

    def test_deleted_tweak_does_not_block_uninstall(self):
        """A tweak removed from the checkout is deselected instead of failing forever."""
        self.dispatcher.write_enabled(['gone'])
        with patch.object(setup, 'ROOT', Path('/mnt/pool/attic-config')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.tasks = [self.managed_task()]
            setup.uninstall(self.dispatcher)
        self.assertEqual(self.tasks, [])
        self.assertEqual(self.dispatcher.read_enabled(), [])
        self.run.assert_not_called()

    def test_failed_disable_prevents_cleanup(self):
        """If disabling boot fails, no tweak is removed."""
        self.dispatcher.write_enabled(['example'])
        with patch.object(setup, 'ROOT', Path('/mnt/pool/attic-config')):
            task=self.managed_task()
            self.api.side_effect=[[task], OSError('middleware unavailable')]
            with self.assertRaises(OSError):
                setup.uninstall(self.dispatcher)
        self.run.assert_not_called()

    def test_uninstall_from_old_copy_is_refused(self):
        """An old source copy cannot remove the relocated installation."""
        self.tasks=[self.managed_task(command='/usr/bin/python3 /mnt/new/attic boot')]
        with self.assertRaisesRegex(ValueError, 'another checkout'):
            setup.uninstall(self.dispatcher)
        self.assertEqual(self.mutations, [])

    def test_uninstall_does_not_delete_unadopted_manual_task(self):
        """A matching manual task must be adopted before setup owns its removal."""
        self.tasks=[self.managed_task(comment='manual')]
        with self.assertRaisesRegex(ValueError, 'adopt'):
            setup.uninstall(self.dispatcher)
        self.assertEqual(self.mutations, [])

    def test_boot_path_is_shell_quoted(self):
        """Spaces, apostrophes and shell metacharacters stay within a single path argument."""
        with patch.object(setup, 'ROOT', Path("/mnt/pool/admin's $(nope)")):
            self.assertEqual(shlex.split(setup.boot_command()),
                             ['/usr/bin/python3', str(setup.ROOT/'attic'), 'boot'])

    def test_workstation_is_rejected_before_permissions_or_api(self):
        """Setup cannot mutate permissions or call middleware on macOS."""
        with patch.object(setup.sys, 'platform', 'darwin'), \
             patch.object(setup, 'prepare_permissions') as permissions:
            with self.assertRaisesRegex(ValueError, 'workstation'):
                setup.main(['install'])
        permissions.assert_not_called()
        self.api.assert_not_called()

    def test_invalid_timeout_is_rejected(self):
        """An invalid timeout fails before host checks or API calls."""
        with contextlib.redirect_stderr(io.StringIO()), patch.object(setup, 'check_host') as host:
            with self.assertRaises(SystemExit) as error:
                setup.main(['install','--timeout','0'])
        self.assertEqual(error.exception.code, 2)
        host.assert_not_called()

    def test_status_only_reads_middleware(self):
        """Integration status never changes tasks or launches tweaks."""
        with contextlib.redirect_stdout(io.StringIO()):
            setup.status()
        self.api.assert_called_once_with('initshutdownscript.query')
        self.run.assert_not_called()


class PermissionTests(unittest.TestCase):
    def test_unsafe_parent_is_not_modified(self):
        """A writable parent causes refusal before any recursive ownership changes."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'checkout'
            root.mkdir()
            with patch.object(setup, 'ROOT', root), patch.object(setup.os, 'chown') as chown:
                with self.assertRaisesRegex(ValueError, 'Parent'):
                    setup.prepare_permissions(repair=True)
                chown.assert_not_called()

    def test_permission_repair_stays_inside_checkout(self):
        """Ownership repair touches only the checkout and removes group/world write."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'checkout'
            root.mkdir()
            config = root / 'config.json'
            config.write_text('{}')
            config.chmod(0o666)
            original = Path.lstat
            def lstat(path, **kwargs):
                info = original(path, **kwargs)
                if path in root.parents:
                    values = list(info)
                    values[0] = 0o40755
                    values[4] = 0
                    return os.stat_result(values)
                return info
            with patch.object(setup, 'ROOT', root), \
                 patch.object(Path, 'lstat', lstat), patch.object(setup.os, 'chown') as chown:
                setup.prepare_permissions(repair=True)
            self.assertEqual({call.args[0] for call in chown.call_args_list}, {root, config})
            self.assertEqual(config.stat().st_mode & 0o777, 0o644)

    def test_symlink_and_hardlink_refused_before_repair(self):
        """External links cannot cause setup to chown a file outside the checkout."""
        for link_type in ('symlink', 'hardlink'):
            with self.subTest(link_type=link_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'checkout'
                root.mkdir()
                outside = Path(directory) / 'outside'
                outside.write_text('keep')
                link = root / 'link'
                if link_type == 'symlink':
                    link.symlink_to(outside)
                else:
                    os.link(outside, link)
                original = Path.lstat
                def lstat(path, **kwargs):
                    info = original(path, **kwargs)
                    if path in root.parents:
                        values = list(info)
                        values[0] = 0o40755
                        values[4] = 0
                        return os.stat_result(values)
                    return info
                with patch.object(setup, 'ROOT', root), \
                     patch.object(Path, 'lstat', lstat), patch.object(setup.os, 'chown') as chown:
                    with self.assertRaises(ValueError):
                        setup.prepare_permissions(repair=True)
                    chown.assert_not_called()
                self.assertEqual(outside.read_text(), 'keep')

    def test_midclt_arguments_are_json_without_shell(self):
        """Local middleware receives method arguments as JSON and has a timeout."""
        with patch.object(setup.subprocess, 'run') as run:
            run.return_value.stdout='true'
            self.assertTrue(setup.call('initshutdownscript.delete', 42))
        run.assert_called_once_with(['/usr/bin/midclt','call','initshutdownscript.delete','42'],
                                    check=True,text=True,capture_output=True,timeout=60)


if __name__ == '__main__':
    unittest.main()
