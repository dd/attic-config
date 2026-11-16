#!/usr/bin/env python3
"""Drive UGREEN front-panel LEDs on TrueNAS 25.10.7 from userspace, without a kernel module."""

import argparse
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import attic_common as common  # noqa: E402

NAME = "ugreen-leds"
SUPPORTED = "25.10.7"
SERVICE = "attic-ugreen-leds.service"
UNITS = Path("/run/systemd/system")
PERSISTENT = Path("/etc/systemd/system")
STATE = Path("/run/attic-ugreen-leds.json")
SYS = Path("/sys")
DEV = Path("/dev")
UDEV = Path("/run/udev/data")
PROC = Path("/proc")
SYSTEMCTL = "/usr/bin/systemctl"
MODPROBE = "/usr/sbin/modprobe"
ZPOOL = "/usr/sbin/zpool"
PYTHON = "/usr/bin/python3"
MARKER = "# Managed by attic-config ugreen-leds v1\n"
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")

# Protocol from https://github.com/miskcoo/ugreen_leds_controller (README and cli/).
ADDRESS = 0x3A
ADAPTER = "SMBus I801 adapter"
I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
SMBUS_WRITE, SMBUS_READ = 0, 1
SMBUS_BYTE_DATA, SMBUS_I2C_BLOCK_DATA = 2, 8
POWER, NETDEV, DISK1 = 0, 1, 2
CMD_BRIGHTNESS, CMD_COLOR, CMD_ONOFF = 1, 2, 3
MODES = ("off", "on", "blink", "breath")
RESULT_REGISTER = 0x80
STATUS_REGISTER = 0x81
RETRIES = 3

# Model prefix from DMI product_name -> ATA port behind each slot, slot 1 first.
# Only models confirmed upstream; DXP6800 wires its two first ports to slots 5-6.
MODELS = {
    "DX4600": ("ata1", "ata2", "ata3", "ata4"),
    "DX4700": ("ata1", "ata2", "ata3", "ata4"),
    "DXP2800": ("ata1", "ata2"),
    "DXP4800": ("ata1", "ata2", "ata3", "ata4"),
    "DXP6800": ("ata3", "ata4", "ata5", "ata6", "ata1", "ata2"),
    "DXP8800": ("ata1", "ata2", "ata3", "ata4", "ata5", "ata6", "ata7", "ata8"),
}
KEYS = {"model", "slots", "activity", "interval_ms", "refresh_seconds", "zfs", "brightness",
        "color", "fault_color", "network", "network_color", "power_color"}
INTERFACE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")


# ---------------------------------------------------------------- configuration

def validate_color(key, value):
    if (not isinstance(value, list) or len(value) != 3
            or any(type(part) is not int or not 0 <= part <= 255 for part in value)):
        raise ValueError(f"{key} must be [R, G, B] with integers from 0 to 255")


def validate_int(config, key, low, high):
    if type(config[key]) is not int or not low <= config[key] <= high:
        raise ValueError(f"{key} must be an integer from {low} to {high}")


def validate_config(config):
    if set(config) != KEYS:
        raise ValueError(f"Configuration must contain exactly: {', '.join(sorted(KEYS))}")
    if config["model"] != "auto" and config["model"] not in MODELS:
        raise ValueError(f"model must be auto or one of: {', '.join(MODELS)}")
    slots = config["slots"]
    if slots != "auto" and (not isinstance(slots, list) or not 1 <= len(slots) <= 8
                            or any(not isinstance(item, str) for item in slots)):
        raise ValueError('slots must be "auto" or a list of disk serials, "" for an empty bay')
    if config["activity"] not in ("dark", "light"):
        raise ValueError("activity must be dark (blink off on I/O) or light (blink on on I/O)")
    validate_int(config, "interval_ms", 50, 1000)
    validate_int(config, "refresh_seconds", 5, 3600)
    validate_int(config, "brightness", 1, 255)
    if type(config["zfs"]) is not bool:
        raise ValueError("zfs must be true or false")
    for key in ("color", "fault_color", "network_color", "power_color"):
        validate_color(key, config[key])
    network = config["network"]
    if not isinstance(network, str) or not (network in ("auto", "off") or INTERFACE.fullmatch(network)):
        raise ValueError("network must be auto, off or an interface name")


# ---------------------------------------------------------------- protocol

def frame(led, command, params=()):
    """Build a 12-byte change request; the checksum covers the bytes before it, ID as zero."""
    params = list(params) + [0] * (4 - len(params))
    data = [0, 0xA0, 0x01, 0x00, 0x00, command, *params]
    total = sum(data)
    data += [(total >> 8) & 0xFF, total & 0xFF]
    data[0] = led
    return bytes(data)


def parse_status(raw):
    """Decode an 11-byte status block; None for anything without a valid checksum."""
    if len(raw) != 11:
        return None
    total = sum(raw[:9])
    if total == 0 or total != (raw[9] << 8 | raw[10]) or raw[0] >= len(MODES):
        return None
    return {"mode": MODES[raw[0]], "brightness": raw[1], "color": list(raw[2:5])}


class SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16),
                ("block", ctypes.c_uint8 * 34)]


class SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8),
                ("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]


class Bus:
    """One SMBus client at the LED controller address, via /dev/i2c-N."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)
        try:
            # Plain I2C_SLAVE (not FORCE) fails if a kernel driver owns 0x3a.
            fcntl.ioctl(self.fd, I2C_SLAVE, ADDRESS)
        except OSError as error:
            os.close(self.fd)
            if error.errno == errno.EBUSY:
                raise ValueError(f"{path} 0x3a is claimed by a kernel driver (led-ugreen?); "
                                 "remove it before using this tweak") from error
            raise

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def transfer(self, direction, command, size, data):
        request = SmbusIoctl(direction, command, size, ctypes.addressof(data))
        fcntl.ioctl(self.fd, I2C_SMBUS, request)

    def read_block(self, command, length):
        data = SmbusData()
        data.block[0] = length
        self.transfer(SMBUS_READ, command, SMBUS_I2C_BLOCK_DATA, data)
        return bytes(data.block[1:length + 1])

    def write_block(self, command, payload):
        data = SmbusData()
        data.block[0] = len(payload)
        for index, value in enumerate(payload):
            data.block[index + 1] = value
        self.transfer(SMBUS_WRITE, command, SMBUS_I2C_BLOCK_DATA, data)

    def read_byte(self, command):
        data = SmbusData()
        self.transfer(SMBUS_READ, command, SMBUS_BYTE_DATA, data)
        return data.byte


class Leds:
    """Confirmed LED changes with the pacing and retries used by the upstream driver."""

    def __init__(self, bus, sleep=time.sleep):
        self.bus = bus
        self.sleep = sleep

    def status(self, led):
        for attempt in range(RETRIES):
            self.sleep(0.001 if attempt == 0 else 0.03)
            try:
                parsed = parse_status(self.bus.read_block(STATUS_REGISTER + led, 11))
            except OSError:
                parsed = None
            if parsed:
                return parsed
        return None

    def change(self, led, command, params=()):
        for attempt in range(RETRIES):
            self.sleep(0.001 if attempt == 0 else 0.03)
            try:
                self.bus.write_block(led, frame(led, command, params))
                self.sleep(0.002)
                if self.bus.read_byte(RESULT_REGISTER) == 1:
                    return True
            except OSError:
                pass
        return False

    def switch(self, led, on):
        return self.change(led, CMD_ONOFF, [1 if on else 0])

    def show(self, led, color, brightness):
        """Set a steady light; each step stays confirmed so a missed write is retried later."""
        return (self.change(led, CMD_BRIGHTNESS, [brightness])
                and self.change(led, CMD_COLOR, color) and self.switch(led, True))


def probe(leds, count):
    """Accept only a controller that returns checksummed status for every expected LED."""
    for led in (POWER, NETDEV, *range(DISK1, DISK1 + count)):
        if leds.status(led) is None:
            raise ValueError(f"No valid LED status from 0x3a for LED {led}; "
                             "this does not look like a supported UGREEN controller")


def find_bus():
    for entry in sorted((SYS / "class/i2c-dev").glob("i2c-*")):
        try:
            name = (entry / "device/name").read_text().strip()
        except OSError:
            continue
        if name.startswith(ADAPTER):
            return DEV / entry.name
    raise ValueError(f"No '{ADAPTER}' found; is the i2c-dev module loaded?")


# ---------------------------------------------------------------- hardware discovery

def detect_model(config):
    if config["model"] != "auto":
        return config["model"], "set in config"
    path = SYS / "class/dmi/id/product_name"
    product = path.read_text().strip() if path.exists() else ""
    for prefix in sorted(MODELS, key=len, reverse=True):
        if product.upper().startswith(prefix):
            return prefix, product
    raise ValueError(f"Unknown model {product!r}. If this is a UGREEN NAS from the list "
                     f"({', '.join(MODELS)}), set model in attic {NAME} config")


def sata_disks():
    return sorted(entry.name for entry in (SYS / "block").glob("sd*"))


def ata_port(disk):
    match = re.search(r"/(ata\d+)/", str((SYS / "block" / disk).resolve()))
    return match.group(1) if match else None


def disk_serial(disk):
    """The serial udev reports (as lsblk shows), with the SCSI VPD page as a fallback."""
    try:
        major_minor = (SYS / "block" / disk / "dev").read_text().strip()
        for line in (UDEV / f"b{major_minor}").read_text().splitlines():
            if line.startswith("E:ID_SERIAL_SHORT="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    try:
        return (SYS / "block" / disk / "device/vpd_pg80").read_bytes()[4:].decode().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def resolve_slots(model, config):
    """Disk name per slot (None for an empty bay), by ATA port or by configured serials."""
    count = len(MODELS[model])
    if config["slots"] == "auto":
        ports = {ata_port(disk): disk for disk in sata_disks()}
        return [ports.get(port) for port in MODELS[model]]
    if len(config["slots"]) != count:
        raise ValueError(f"{model} has {count} bays; slots lists {len(config['slots'])}")
    serials = {disk_serial(disk): disk for disk in sata_disks()}
    return [serials.get(serial) if serial else None for serial in config["slots"]]


def parent_disk(name):
    path = SYS / "class/block" / name
    return path.resolve().parent.name if (path / "partition").exists() else name


def zfs_faults():
    """Disks backing any ZFS leaf vdev that is not ONLINE."""
    output = subprocess.run([ZPOOL, "status", "-LP"], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout
    return {parent_disk(match.group(1))
            for match in re.finditer(r"^\s+/dev/(\S+)\s+([A-Z]+)\b", output, re.M)
            if match.group(2) != "ONLINE"}


def default_interface():
    for line in (PROC / "net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return fields[0]
    return None


def read_text(path):
    try:
        return path.read_text()
    except OSError:
        return None


# ---------------------------------------------------------------- monitor

class Light:
    """Activity and health of one LED; writes happen only on transitions."""

    def __init__(self, led):
        self.led = led
        self.source = None
        self.state = None
        self.seen = False
        self.counters = None
        self.lit = None
        self.blinking = False


class Monitor:
    def __init__(self, leds, model, config, faults=zfs_faults):
        self.leds = leds
        self.model = model
        self.config = config
        self.faults = faults
        self.disks = [Light(DISK1 + index) for index in range(len(MODELS[model]))]
        self.network = Light(NETDEV)
        self.idle_lit = config["activity"] == "dark"

    def start(self):
        self.leds.show(POWER, self.config["power_color"], self.config["brightness"])

    def set_state(self, light, state):
        """Apply empty/ok/fault/down; a failed write leaves the state unset to retry."""
        if state == light.state:
            return
        if state in ("empty", "down"):
            done = self.leds.switch(light.led, False)
        else:
            key = {"ok": "color", "fault": "fault_color", "up": "network_color"}[state]
            done = self.leds.show(light.led, self.config[key], self.config["brightness"])
        light.state = state if done else None
        light.lit = state not in ("empty", "down") if done else None
        light.blinking = False

    def refresh(self):
        """Re-read the bay mapping and health; covers hot-swapped and vanished disks."""
        disks = resolve_slots(self.model, self.config)
        faults = set()
        if self.config["zfs"]:
            try:
                faults = self.faults()
            except (OSError, subprocess.SubprocessError) as error:
                print(f"zpool status failed: {error}", file=sys.stderr)
        for light, disk in zip(self.disks, disks):
            if disk:
                light.seen = True
            if disk != light.source:
                light.counters = None
            light.source = disk
            if disk is None:
                # A bay that held a disk since start and is now empty means a lost disk.
                self.set_state(light, "fault" if light.seen else "empty")
            else:
                self.set_state(light, "fault" if disk in faults else "ok")
        name = self.config["network"]
        if name == "off":
            self.network.source = None
            self.set_state(self.network, "down")
            return
        interface = default_interface() if name == "auto" else name
        if interface != self.network.source:
            self.network.counters = None
        self.network.source = interface
        operstate = read_text(SYS / "class/net" / str(interface) / "operstate") if interface else None
        self.set_state(self.network, "up" if (operstate or "").strip() == "up" else "down")

    def counters(self, light):
        if light is self.network:
            base = SYS / "class/net" / str(light.source) / "statistics"
            values = (read_text(base / "rx_bytes"), read_text(base / "tx_bytes"))
            return None if None in values else values
        return read_text(SYS / "block" / light.source / "stat")

    def tick(self):
        """Blink once per change: one interval in the active phase, then back to idle."""
        for light in (*self.disks, self.network):
            if light.state not in ("ok", "fault", "up"):
                continue
            counters = self.counters(light)
            busy = light.counters is not None and counters != light.counters
            light.counters = counters
            if light.blinking:
                light.blinking = False
                target = self.idle_lit
            else:
                light.blinking = busy
                target = self.idle_lit != busy
            if target != light.lit and self.leds.switch(light.led, target):
                light.lit = target

    def stop(self):
        for light in (*self.disks, self.network):
            self.leds.switch(light.led, False)

    def snapshot(self, product, bus):
        return {
            "pid": os.getpid(), "model": self.model, "product": product, "bus": str(bus),
            "slots": [{"slot": index + 1, "disk": light.source, "state": light.state,
                       "serial": disk_serial(light.source) if light.source else ""}
                      for index, light in enumerate(self.disks)],
            "network": {"interface": self.network.source, "state": self.network.state},
        }


def run():
    """Long-running monitor started by systemd; resets disk and network LEDs on stop."""
    check_host()
    config = common.read_config(ROOT, NAME, validate_config)
    model, product = detect_model(config)
    bus_path = find_bus()
    stopping = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stopping.append(True))
    with Bus(bus_path) as bus:
        leds = Leds(bus)
        probe(leds, len(MODELS[model]))
        monitor = Monitor(leds, model, config)
        monitor.start()
        refresh_at = 0.0
        try:
            while not stopping:
                if time.monotonic() >= refresh_at:
                    monitor.refresh()
                    data = json.dumps(monitor.snapshot(product, bus_path), indent=2) + "\n"
                    common.atomic_write(STATE, data.encode(), 0o644)
                    refresh_at = time.monotonic() + config["refresh_seconds"]
                monitor.tick()
                time.sleep(config["interval_ms"] / 1000)
        finally:
            monitor.stop()
            STATE.unlink(missing_ok=True)


# ---------------------------------------------------------------- integration

def systemctl(*args):
    """Run a bounded systemctl request; never invoke a shell."""
    return subprocess.run(
        [SYSTEMCTL, *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    ).stdout.strip()


def prop(name):
    return systemctl("show", SERVICE, f"--property={name}", "--value")


def check_host(install=False):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run as root on Linux/TrueNAS, not on your workstation")
    if not UNITS.is_dir() or not Path(SYSTEMCTL).is_file():
        raise ValueError("A running systemd system manager is required")
    if install:
        version = common.truenas_version()
        if version != SUPPORTED:
            raise ValueError(f"Unsupported version: {version}; review compatibility first")


def script():
    path = Path(__file__).absolute()
    if not SAFE_PATH.fullmatch(str(path)):
        raise ValueError(f"Unsupported characters in checkout path for a systemd unit: {path}")
    return path


def render_unit():
    return (MARKER + "[Unit]\n"
            "Description=attic-config UGREEN front-panel LEDs\n"
            "StartLimitIntervalSec=600\nStartLimitBurst=5\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={PYTHON} -B {script()} run\n"
            "Restart=on-failure\nRestartSec=10\n").encode()


def verify_owned():
    """Never replace or remove an unrelated unit using our reserved name."""
    path = UNITS / SERVICE
    for parent in path.parents:
        if parent.exists() or parent.is_symlink():
            common.secure_path(parent)
    if path.exists() or path.is_symlink():
        common.secure_path(path)
        if not path.read_text().startswith(MARKER):
            raise ValueError(f"Refusing to overwrite an unmanaged file: {path}")
    persistent = PERSISTENT / SERVICE
    if persistent.exists() or persistent.is_symlink():
        raise ValueError(f"Conflicting persistent file: {persistent}")


def wait_running(timeout=10.0):
    """The monitor writes its state after probing; that proves it reached the loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = prop("MainPID")
        if prop("ActiveState") == "active" and STATE.exists():
            try:
                if str(json.loads(STATE.read_text())["pid"]) == pid:
                    return
            except (ValueError, KeyError, OSError):
                pass
        time.sleep(0.5)
    raise ValueError(f"{SERVICE} did not start; see journalctl -b -u {SERVICE}")


def install():
    check_host(install=True)
    config = common.read_config(ROOT, NAME, validate_config)
    verify_owned()
    common.secure_path(script())
    common.secure_path(ROOT / "lib/attic_common.py")
    path = UNITS / SERVICE
    old = path.read_bytes() if path.exists() else None
    data = render_unit()
    try:
        if old is not None:
            systemctl("stop", SERVICE)  # release the bus before probing
        subprocess.run([MODPROBE, "i2c-dev"], check=True, timeout=30,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Preflight in this process so mistakes show here, not only in the journal.
        model, product = detect_model(config)
        bus_path = find_bus()
        with Bus(bus_path) as bus:
            probe(Leds(bus), len(MODELS[model]))
        slots = resolve_slots(model, config)
        if old != data:
            common.atomic_write(path, data)
        systemctl("daemon-reload")
        systemctl("reset-failed", SERVICE)
        systemctl("start", SERVICE)
        wait_running()
    except Exception:
        subprocess.run([SYSTEMCTL, "stop", SERVICE], timeout=30, check=False)
        if old is None:
            path.unlink(missing_ok=True)
        else:
            common.atomic_write(path, old)
        systemctl("daemon-reload")
        if old is not None:
            # Best effort: bring back the previous monitor that install stopped.
            subprocess.run([SYSTEMCTL, "start", SERVICE], timeout=30, check=False)
        raise
    mapping = ", ".join(f"{index}={disk or '-'}" for index, disk in enumerate(slots, 1))
    print(f"UGREEN LEDs running for this boot: {model} ({product}), {bus_path}, slots {mapping}.")


def uninstall():
    check_host()
    verify_owned()
    path = UNITS / SERVICE
    if path.exists():
        systemctl("stop", SERVICE)  # the monitor switches its LEDs off on SIGTERM
        path.unlink()
        systemctl("daemon-reload")
    STATE.unlink(missing_ok=True)
    print("UGREEN LED monitor removed.")


def status():
    """Report the unit and the monitor's last view of bays; never touches the bus."""
    if sys.platform != "linux" or not Path(SYSTEMCTL).is_file():
        return "runtime unavailable (requires Linux/systemd)"
    if not (UNITS / SERVICE).exists():
        return "not installed for this boot"
    lines = [f"installed; service={prop('ActiveState')}; result={prop('Result')}; "
             f"restarts={prop('NRestarts')}"]
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return lines[0] + "; monitor state unavailable"
    lines.append(f"model={state['model']} ({state['product']}); bus={state['bus']}")
    for slot in state["slots"]:
        disk = f"{slot['disk']} {slot['serial']}".strip() if slot["disk"] else "-"
        lines.append(f"slot {slot['slot']}: {disk} [{slot['state']}]")
    network = state["network"]
    lines.append(f"network: {network['interface'] or '-'} [{network['state']}]")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    for name, help_text in (
        ("install", "Detect the model, check the controller and start the monitor"),
        ("uninstall", "Stop the monitor, switch its LEDs off and remove the unit"),
        ("config", "Edit, validate and apply settings for an enabled tweak"),
        ("status", "Show the service and bay mapping without changing the system"),
        ("boot", "Restore an enabled tweak after reboot"),
        ("apply", "Apply updated code and settings for an enabled tweak"),
        ("run", "Run the LED monitor in the foreground (used by systemd)"),
    ):
        commands.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return
    if args.action == "status":
        print(status())
        return
    if args.action == "run":
        run()
        return
    check_host()
    if args.action == "install":
        install()
    elif args.action == "uninstall":
        uninstall()
    elif args.action == "config":
        check_host(install=True)
        enabled = NAME in common.read_enabled(ROOT)
        common.edit_config(ROOT, NAME, validate_config, apply=install if enabled else None)
    else:
        if NAME not in common.read_enabled(ROOT):
            raise ValueError(f"{NAME} is not enabled; use attic {NAME} install first")
        install()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"attic-ugreen-leds: {error}", file=sys.stderr)
        sys.exit(1)
