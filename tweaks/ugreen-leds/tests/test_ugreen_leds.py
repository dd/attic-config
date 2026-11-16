"""Offline tests: a fake SMBus and a fake sysfs tree; no hardware, systemd or network."""

import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ugreen", Path(__file__).parents[1] / "tweak.py")
ugreen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ugreen)

DEFAULTS = json.loads((Path(ugreen.__file__).with_name("config.json")).read_text())
# Status of a purple power LED blinking 400/600 ms, from the upstream README.
README_STATUS = bytes([0x02, 0xB4, 0xFF, 0x00, 0xFF, 0x03, 0xE8, 0x01, 0x90, 0x04, 0x30])


class FakeBus:
    def __init__(self, status=README_STATUS, result=1):
        self.status = status
        self.result = result
        self.writes = []

    def read_block(self, command, length):
        return self.status

    def write_block(self, command, payload):
        self.writes.append((command, bytes(payload)))

    def read_byte(self, command):
        return self.result


def leds(bus=None):
    return ugreen.Leds(bus or FakeBus(), sleep=lambda _: None)


def switches(bus):
    """(led, on) for every on/off request, in order."""
    return [(payload[0], bool(payload[6])) for _, payload in bus.writes
            if payload[5] == ugreen.CMD_ONOFF]


class ProtocolTests(unittest.TestCase):
    def test_frame_matches_upstream_example(self):
        """Byte-for-byte the i2cset example for the power LED from the upstream README."""
        self.assertEqual(ugreen.frame(0, ugreen.CMD_ONOFF, [1]),
                         bytes([0x00, 0xA0, 0x01, 0x00, 0x00, 0x03, 0x01, 0, 0, 0, 0x00, 0xA5]))

    def test_frame_checksum_excludes_led_id(self):
        """The ID replaces byte 0 after the checksum, as the controller expects."""
        data = ugreen.frame(5, ugreen.CMD_COLOR, [255, 0, 0])
        self.assertEqual(data[0], 5)
        self.assertEqual(data[10] << 8 | data[11], sum([0xA0, 0x01, 0x02, 0xFF]))

    def test_parse_status(self):
        self.assertEqual(ugreen.parse_status(README_STATUS),
                         {"mode": "blink", "brightness": 0xB4, "color": [255, 0, 255]})

    def test_parse_rejects_noise(self):
        """Zeros, a wrong checksum or a short read never count as a controller reply."""
        for raw in (bytes(11), README_STATUS[:-1] + b"\x31", README_STATUS[:10],
                    bytes([7]) + README_STATUS[1:]):
            with self.subTest(raw=raw):
                self.assertIsNone(ugreen.parse_status(raw))

    def test_change_retries_until_confirmed(self):
        """An unconfirmed write is repeated, then reported as failed."""
        bus = FakeBus(result=0)
        self.assertFalse(leds(bus).switch(2, True))
        self.assertEqual(len(bus.writes), ugreen.RETRIES)
        bus = FakeBus()
        self.assertTrue(leds(bus).switch(2, True))
        self.assertEqual(len(bus.writes), 1)

    def test_probe_requires_every_led(self):
        """A device without valid status for each LED is not driven."""
        ugreen.probe(leds(), 4)
        with self.assertRaisesRegex(ValueError, "does not look like"):
            ugreen.probe(leds(FakeBus(status=bytes(11))), 4)

    def test_busy_address_is_reported(self):
        """A kernel driver owning 0x3a blocks userspace access instead of being forced."""
        with patch.object(ugreen.os, "open", return_value=99), \
             patch.object(ugreen.os, "close") as close, \
             patch.object(ugreen.fcntl, "ioctl", side_effect=OSError(errno.EBUSY, "busy")):
            with self.assertRaisesRegex(ValueError, "kernel driver"):
                ugreen.Bus("/dev/i2c-1")
            close.assert_called_once_with(99)


class ConfigTests(unittest.TestCase):
    def test_versioned_defaults_are_valid(self):
        ugreen.validate_config(DEFAULTS)

    def test_rejects_invalid_values(self):
        for key, value in (("model", "DS920"), ("slots", "sda"), ("slots", [1, 2]),
                           ("activity", "blink"), ("interval_ms", 10), ("brightness", True),
                           ("zfs", 1), ("color", [255, 0]), ("power_color", [0, 0, 256]),
                           ("network", "eth0; reboot")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                ugreen.validate_config({**DEFAULTS, key: value})

    def test_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            ugreen.validate_config({**DEFAULTS, "speed": 1})


class FakeSys(unittest.TestCase):
    """A temporary /sys with SATA disks behind chosen ATA ports."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        root = Path(self.temp.name)
        self.sys = root / "sys"
        (self.sys / "block").mkdir(parents=True)
        (self.sys / "class/block").mkdir(parents=True)
        (self.sys / "class/dmi/id").mkdir(parents=True)
        (self.sys / "class/net/eth0/statistics").mkdir(parents=True)
        self.udev = root / "udev"
        self.udev.mkdir()
        self.proc = root / "proc"
        (self.proc / "net").mkdir(parents=True)
        (self.proc / "net/route").write_text(
            "Iface\tDestination\tGateway\nlo\t0000007F\t0\neth0\t00000000\t0101A8C0\n")
        (self.sys / "class/net/eth0/operstate").write_text("up\n")
        self.traffic(0)
        for name, value in (("SYS", self.sys), ("UDEV", self.udev), ("PROC", self.proc)):
            patch.object(ugreen, name, value).start()

    def product(self, name):
        (self.sys / "class/dmi/id/product_name").write_text(name + "\n")

    def disk(self, name, port, serial, minor):
        device = self.sys / f"devices/pci0000:00/0000:00:17.0/{port}/host0/target0/block/{name}"
        device.mkdir(parents=True)
        (device / "stat").write_text("0 0 0\n")
        (device / "dev").write_text(f"8:{minor}\n")
        (self.sys / "block" / name).symlink_to(device)
        (self.sys / "class/block" / name).symlink_to(device)
        part = device / f"{name}1"
        part.mkdir()
        (part / "partition").write_text("1\n")
        (self.sys / "class/block" / f"{name}1").symlink_to(part)
        (self.udev / f"b8:{minor}").write_text(f"S:disk/by-id/x\nE:ID_SERIAL_SHORT={serial}\n")

    def io(self, name, count):
        (self.sys / "block" / name / "stat").write_text(f"{count} 0 0\n")

    def remove(self, name):
        (self.sys / "block" / name).unlink()

    def traffic(self, count):
        for name in ("rx_bytes", "tx_bytes"):
            (self.sys / "class/net/eth0/statistics" / name).write_text(f"{count}\n")


class DiscoveryTests(FakeSys):
    def test_model_from_dmi_prefix(self):
        """Variants such as Plus and Pro share their series mapping."""
        for product, model in (("DXP4800 Plus", "DXP4800"), ("DXP6800 Pro", "DXP6800"),
                               ("DXP2800", "DXP2800")):
            with self.subTest(product=product):
                self.product(product)
                self.assertEqual(ugreen.detect_model(DEFAULTS), (model, product))

    def test_unknown_model_is_refused_unless_configured(self):
        self.product("Default string")
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            ugreen.detect_model(DEFAULTS)
        self.assertEqual(ugreen.detect_model({**DEFAULTS, "model": "DXP8800"})[0], "DXP8800")

    def test_dxp6800_port_order(self):
        """ata1 and ata2 are bays 5 and 6 on the DXP6800."""
        self.disk("sda", "ata1", "S1", 0)
        self.disk("sdb", "ata3", "S2", 16)
        self.assertEqual(ugreen.resolve_slots("DXP6800", DEFAULTS),
                         ["sdb", None, None, None, "sda", None])
        self.assertEqual(ugreen.resolve_slots("DXP4800", DEFAULTS), ["sda", None, "sdb", None])

    def test_serial_mapping(self):
        self.disk("sda", "ata1", "AAA", 0)
        self.disk("sdb", "ata2", "BBB", 16)
        config = {**DEFAULTS, "slots": ["BBB", "", "AAA", "CCC"]}
        self.assertEqual(ugreen.resolve_slots("DXP4800", config), ["sdb", None, "sda", None])
        with self.assertRaisesRegex(ValueError, "4 bays"):
            ugreen.resolve_slots("DXP4800", {**DEFAULTS, "slots": ["AAA"]})

    def test_zfs_faults_map_partitions_to_disks(self):
        self.disk("sda", "ata1", "S1", 0)
        self.disk("sdb", "ata2", "S2", 16)
        output = ("  pool: tank\n config:\n\n\tNAME           STATE     READ WRITE CKSUM\n"
                  "\ttank           DEGRADED     0     0     0\n"
                  "\t  mirror-0     DEGRADED     0     0     0\n"
                  "\t    /dev/sda1  ONLINE       0     0     0\n"
                  "\t    /dev/sdb1  FAULTED      3     0     0  too many errors\n")
        result = subprocess.CompletedProcess([], 0, stdout=output)
        with patch.object(ugreen.subprocess, "run", return_value=result):
            self.assertEqual(ugreen.zfs_faults(), {"sdb"})

    def test_find_bus_picks_i801(self):
        for index, name in ((0, "Synopsys DesignWare I2C adapter"), (1, "SMBus I801 adapter at efa0")):
            device = self.sys / f"class/i2c-dev/i2c-{index}/device"
            device.mkdir(parents=True)
            (device / "name").write_text(name + "\n")
        self.assertEqual(ugreen.find_bus(), ugreen.DEV / "i2c-1")


class MonitorTests(FakeSys):
    def setUp(self):
        super().setUp()
        self.disk("sda", "ata1", "S1", 0)
        self.disk("sdb", "ata2", "S2", 16)
        self.bus = FakeBus()
        self.faults = set()
        self.monitor = ugreen.Monitor(leds(self.bus), "DXP4800", DEFAULTS, faults=lambda: self.faults)
        self.monitor.refresh()
        self.monitor.tick()  # first read only records counters
        self.bus.writes.clear()

    def colors(self):
        return {payload[0]: list(payload[6:9]) for _, payload in self.bus.writes
                if payload[5] == ugreen.CMD_COLOR}

    def test_empty_bays_are_off_and_disks_lit(self):
        states = [light.state for light in self.monitor.disks]
        self.assertEqual(states, ["ok", "ok", "empty", "empty"])
        self.assertEqual(self.monitor.network.state, "up")

    def test_activity_blinks_dark_then_recovers_under_load(self):
        """Constant I/O still blinks: off for one interval, then on for one."""
        disk1 = ugreen.DISK1
        for count in (1, 2, 3):
            self.io("sda", count)
            self.monitor.tick()
        self.assertEqual(switches(self.bus), [(disk1, False), (disk1, True), (disk1, False)])
        self.bus.writes.clear()
        self.monitor.tick()  # idle: back to lit, then nothing more
        self.monitor.tick()
        self.assertEqual(switches(self.bus), [(disk1, True)])

    def test_light_mode_inverts(self):
        monitor = ugreen.Monitor(leds(self.bus), "DXP4800", {**DEFAULTS, "activity": "light"},
                                 faults=lambda: set())
        monitor.refresh()
        monitor.tick()
        self.bus.writes.clear()
        self.io("sdb", 5)
        monitor.tick()
        monitor.tick()
        self.assertEqual(switches(self.bus), [(ugreen.DISK1 + 1, True), (ugreen.DISK1 + 1, False)])

    def test_network_blinks_on_traffic(self):
        self.traffic(100)
        self.monitor.tick()
        self.assertEqual(switches(self.bus), [(ugreen.NETDEV, False)])

    def test_zfs_fault_and_recovery(self):
        self.faults = {"sdb"}
        self.monitor.refresh()
        self.assertEqual(self.colors(), {ugreen.DISK1 + 1: DEFAULTS["fault_color"]})
        self.bus.writes.clear()
        self.faults = set()
        self.monitor.refresh()
        self.assertEqual(self.colors(), {ugreen.DISK1 + 1: DEFAULTS["color"]})

    def test_vanished_disk_turns_red(self):
        """A pulled or dropped disk is a fault, not an empty bay."""
        self.remove("sda")
        self.monitor.refresh()
        self.assertEqual(self.monitor.disks[0].state, "fault")
        self.assertEqual(self.colors(), {ugreen.DISK1: DEFAULTS["fault_color"]})

    def test_zfs_errors_do_not_stop_monitor(self):
        def broken():
            raise subprocess.TimeoutExpired("zpool", 20)
        self.monitor.faults = broken
        with contextlib.redirect_stderr(io.StringIO()):
            self.monitor.refresh()
        self.assertEqual(self.monitor.disks[0].state, "ok")

    def test_failed_write_is_retried_on_next_refresh(self):
        self.bus.result = 0
        self.faults = {"sda"}
        self.monitor.refresh()
        self.assertIsNone(self.monitor.disks[0].state)
        self.bus.result = 1
        self.monitor.refresh()
        self.assertEqual(self.monitor.disks[0].state, "fault")

    def test_stop_switches_off_disks_and_network(self):
        self.monitor.stop()
        self.assertEqual({led for led, on in switches(self.bus) if not on},
                         {ugreen.NETDEV, *range(ugreen.DISK1, ugreen.DISK1 + 4)})

    def test_snapshot_lists_bays(self):
        data = self.monitor.snapshot("DXP4800 Plus", "/dev/i2c-1")
        self.assertEqual(data["slots"][0], {"slot": 1, "disk": "sda", "state": "ok", "serial": "S1"})
        self.assertEqual(data["network"], {"interface": "eth0", "state": "up"})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        root = Path(self.temp.name)
        self.units = root / "units"
        self.units.mkdir()
        self.unit = self.units / ugreen.SERVICE
        patch.object(ugreen, "UNITS", self.units).start()
        patch.object(ugreen, "PERSISTENT", root / "etc").start()
        patch.object(ugreen, "STATE", root / "state.json").start()
        patch.object(ugreen, "check_host").start()
        patch.object(ugreen, "script", return_value=Path("/mnt/tank/attic/tweak.py")).start()
        patch.object(ugreen.common, "secure_path").start()
        patch.object(ugreen.common, "read_config", return_value=DEFAULTS).start()
        patch.object(ugreen, "detect_model", return_value=("DXP4800", "DXP4800 Plus")).start()
        patch.object(ugreen, "find_bus", return_value=Path("/dev/i2c-1")).start()
        patch.object(ugreen, "Bus").start()
        self.probe = patch.object(ugreen, "probe").start()
        patch.object(ugreen, "resolve_slots", return_value=["sda", None, None, None]).start()
        self.run = patch.object(ugreen.subprocess, "run").start()
        self.wait = patch.object(ugreen, "wait_running").start()
        self.ctl = patch.object(ugreen, "systemctl", return_value="").start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def commands(self):
        return [call.args for call in self.ctl.call_args_list]

    def test_unit_runs_this_script(self):
        text = ugreen.render_unit().decode()
        self.assertTrue(text.startswith(ugreen.MARKER))
        self.assertIn("ExecStart=/usr/bin/python3 -B /mnt/tank/attic/tweak.py run\n", text)
        self.assertIn("Restart=on-failure\n", text)
        self.assertNotIn("[Install]", text)

    def test_install_and_uninstall_are_idempotent(self):
        ugreen.install()
        ugreen.install()
        self.assertEqual(self.unit.read_bytes(), ugreen.render_unit())
        self.assertIn(("stop", ugreen.SERVICE), self.commands())  # second run frees the bus
        self.assertEqual(self.run.call_args_list[0].args[0], [ugreen.MODPROBE, "i2c-dev"])
        ugreen.uninstall()
        ugreen.uninstall()
        self.assertFalse(self.unit.exists())

    def test_failed_probe_leaves_nothing(self):
        self.probe.side_effect = ValueError("does not look like")
        with self.assertRaisesRegex(ValueError, "does not look like"):
            ugreen.install()
        self.assertFalse(self.unit.exists())
        self.assertNotIn(("start", ugreen.SERVICE), self.commands())

    def test_failed_start_restores_previous_unit(self):
        self.unit.write_bytes(ugreen.MARKER.encode() + b"# previous\n")
        self.wait.side_effect = ValueError("did not start")
        with self.assertRaisesRegex(ValueError, "did not start"):
            ugreen.install()
        self.assertEqual(self.unit.read_bytes(), ugreen.MARKER.encode() + b"# previous\n")
        self.assertIn([ugreen.SYSTEMCTL, "start", ugreen.SERVICE],
                      [call.args[0] for call in self.run.call_args_list])

    def test_unmanaged_unit_is_preserved(self):
        self.unit.write_text("[Service]\n")
        for operation in (ugreen.install, ugreen.uninstall):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    operation()
                self.assertEqual(self.unit.read_text(), "[Service]\n")

    def test_status_shows_bays(self):
        self.unit.write_bytes(ugreen.render_unit())
        state = {"pid": 1, "model": "DXP4800", "product": "DXP4800 Plus", "bus": "/dev/i2c-1",
                 "slots": [{"slot": 1, "disk": "sda", "serial": "S1", "state": "ok"},
                           {"slot": 2, "disk": None, "serial": "", "state": "empty"}],
                 "network": {"interface": "eth0", "state": "up"}}
        ugreen.STATE.write_text(json.dumps(state))
        self.ctl.side_effect = lambda *args: {"ActiveState": "active", "Result": "success",
                                              "NRestarts": "0"}[args[2].split("=")[1]]
        with patch.object(ugreen.sys, "platform", "linux"), \
             patch.object(ugreen, "SYSTEMCTL", __file__):
            text = ugreen.status()
        self.assertIn("service=active", text)
        self.assertIn("slot 1: sda S1 [ok]", text)
        self.assertIn("slot 2: - [empty]", text)

    def test_status_on_workstation_is_read_only(self):
        with patch.object(ugreen.sys, "platform", "darwin"):
            self.assertIn("runtime unavailable", ugreen.status())
        self.ctl.assert_not_called()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(ugreen, "check_host").start()
        patch.object(ugreen.common, "read_enabled", return_value=[]).start()
        self.install = patch.object(ugreen, "install").start()

    def test_apply_and_boot_require_activation(self):
        for action in ("apply", "boot"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "use attic"):
                ugreen.main([action])
        self.install.assert_not_called()

    def test_help_has_no_side_effects(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ugreen.main([])
        self.assertIn("install", output.getvalue())
        self.install.assert_not_called()

    def test_unsafe_checkout_path_is_refused(self):
        with patch.object(ugreen, "__file__", "/mnt/my pool/tweak.py"):
            with self.assertRaisesRegex(ValueError, "Unsupported characters"):
                ugreen.script()


if __name__ == "__main__":
    unittest.main()
