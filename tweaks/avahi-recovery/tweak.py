#!/usr/bin/env python3
"""Add a boot-scoped, bounded systemd restart policy for Avahi on TrueNAS 25.10.7."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(ROOT / "lib"))
import attic_common as common  # noqa: E402

NAME = "avahi-recovery"
SUPPORTED = "25.10.7"
TARGET = "avahi-daemon.service"
DROPIN = f"{TARGET}.d/90-attic-recovery.conf"
UNITS = Path("/run/systemd/system")
PERSISTENT = Path("/etc/systemd/system")
SYSTEMCTL = "/usr/bin/systemctl"
MARKER = "# Managed by attic-config avahi recovery v2\n"
BOUNDS = {"attempts": (1, 3), "window_seconds": (300, 86400), "delay_seconds": (5, 60)}


def systemctl(*args):
    """Run a bounded systemctl request; never invoke a shell."""
    return subprocess.run(
        [SYSTEMCTL, *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    ).stdout.strip()


def prop(name):
    return systemctl("show", TARGET, f"--property={name}", "--value")


def validate_config(config):
    if set(config) != set(BOUNDS):
        raise ValueError(f"Configuration must contain exactly: {', '.join(BOUNDS)}")
    for key, (low, high) in BOUNDS.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")


def render_dropin(config):
    """Restart only unclean exits; systemd stops retrying once the start limit is hit."""
    return (MARKER + "[Unit]\n"
            f"StartLimitIntervalSec={config['window_seconds']}\n"
            f"StartLimitBurst={config['attempts']}\n\n"
            "[Service]\nRestart=on-failure\n"
            f"RestartSec={config['delay_seconds']}\n").encode()


def check_host(install=False):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run as root on Linux/TrueNAS, not on your workstation")
    if not Path("/run/systemd/system").is_dir() or not Path(SYSTEMCTL).is_file():
        raise ValueError("A running systemd system manager is required")
    if install:
        version = common.truenas_version()
        if version != SUPPORTED:
            raise ValueError(f"Unsupported version: {version}; review compatibility first")
        if prop("LoadState") != "loaded":
            raise ValueError("Avahi must exist and must not be masked")


def verify_owned():
    """Never replace or remove an unrelated drop-in using our reserved name."""
    path = UNITS / DROPIN
    for parent in path.parents:
        if parent.exists() or parent.is_symlink():
            common.secure_path(parent)
    if path.exists() or path.is_symlink():
        common.secure_path(path)
        if not path.read_text().startswith(MARKER):
            raise ValueError(f"Refusing to overwrite an unmanaged file: {path}")
    # A persistent drop-in with the same name would shadow our runtime file.
    persistent = PERSISTENT / DROPIN
    if persistent.exists() or persistent.is_symlink():
        raise ValueError(f"Conflicting persistent file: {persistent}")


def policy_effective():
    return (str(UNITS / DROPIN) in prop("DropInPaths").split()
            and prop("Restart") == "on-failure")


def install():
    check_host(install=True)
    config = common.read_config(ROOT, NAME, validate_config)
    verify_owned()
    path = UNITS / DROPIN
    data = render_dropin(config)
    old = path.read_bytes() if path.exists() else None
    try:
        path.parent.mkdir(mode=0o755, exist_ok=True)
        if old != data:
            common.atomic_write(path, data)
        systemctl("daemon-reload")
        if not policy_effective() or prop("StartLimitBurst") != str(config["attempts"]):
            raise ValueError("Restart policy was overridden by another drop-in")
        # A plain start stays subject to the limit, so a repeat cannot bypass it.
        if prop("ActiveState") == "failed":
            systemctl("start", "--no-block", TARGET)
    except Exception:
        # Restore our own file even if reload or verification fails.
        if old is None:
            path.unlink(missing_ok=True)
        else:
            common.atomic_write(path, old)
        systemctl("daemon-reload")
        raise
    print("Avahi recovery installed for this boot.")


def uninstall():
    check_host()
    verify_owned()
    (UNITS / DROPIN).unlink(missing_ok=True)
    systemctl("daemon-reload")
    print("Avahi recovery runtime integration removed.")


def status():
    """Report runtime installation separately from daemon health; never modify it."""
    if sys.platform != "linux" or not Path(SYSTEMCTL).is_file():
        return "runtime unavailable (requires Linux/systemd)"
    if not (UNITS / DROPIN).exists():
        return "not installed for this boot"
    if not policy_effective():
        return "drop-in present but systemd integration is not effective"
    return (f"installed; Avahi={prop('ActiveState')}; result={prop('Result')}; "
            f"restarts={prop('NRestarts')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    for name, help_text in (
        ("install", "Install the restart policy for this boot"),
        ("uninstall", "Remove runtime integration"),
        ("config", "Edit, validate and apply settings for an enabled tweak"),
        ("status", "Show runtime status without changing the system"),
        ("boot", "Restore an enabled tweak after reboot"),
        ("apply", "Apply updated code and settings for an enabled tweak"),
    ):
        commands.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return
    if args.action == "status":
        print(status())
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
            raise ValueError("avahi-recovery is not enabled; use attic avahi-recovery install first")
        install()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"attic-avahi: {error}", file=sys.stderr)
        sys.exit(1)
