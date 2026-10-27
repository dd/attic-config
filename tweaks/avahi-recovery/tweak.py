#!/usr/bin/env python3
"""Install a boot-scoped, bounded Avahi failure handler on TrueNAS 25.10.7."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

TARGET = "avahi-daemon.service"
HANDLER = "attic-avahi-recovery.service"
RUNTIME = Path("/run/attic-avahi")
UNITS = Path("/run/systemd/system")
SYSTEMCTL = "/usr/bin/systemctl"
MARKER = "# Managed by attic-config avahi recovery v1\n"
DEFAULTS = {"attempts": 3, "window_seconds": 600, "delay_seconds": 20}
DROPIN = MARKER + "[Unit]\nOnFailure=attic-avahi-recovery.service\n"
SERVICE = MARKER + """[Unit]
Description=Bounded Avahi recovery for attic
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /run/attic-avahi/avahi_recovery.py recover
TimeoutStartSec=300
UMask=0077
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/run/attic-avahi
PrivateTmp=yes
"""


def systemctl(*args):
    """Run a bounded systemctl request; never invoke a shell."""
    return subprocess.run(
        [SYSTEMCTL, *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    ).stdout.strip()


def secure_path(path):
    """Reject symlinks and paths writable by non-root users (POSIX modes)."""
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Symlink is not allowed: {item}")
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Must be root-owned and not group/world-writable: {item}")


def read_config(path):
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise ValueError(f"Configuration must contain exactly: {', '.join(DEFAULTS)}")
    bounds = {"attempts": (1, 3), "window_seconds": (300, 86400), "delay_seconds": (5, 60)}
    for key, (low, high) in bounds.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    return config


def check_host(install=False):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run as root on Linux/TrueNAS, not on your workstation")
    if not Path("/run/systemd/system").is_dir() or not Path(SYSTEMCTL).is_file():
        raise ValueError("A running systemd system manager is required")
    if install:
        version = Path("/etc/version").read_text().strip()
        if not re.fullmatch(r"TrueNAS-(?:SCALE-)?25\.10\.7", version):
            raise ValueError(f"Unsupported version: {version}; review compatibility first")
        if systemctl("show", TARGET, "--property=LoadState", "--value") != "loaded":
            raise ValueError("Avahi must exist and must not be masked")
        if systemctl("show", TARGET, "--property=RestartMode", "--value") == "direct":
            raise ValueError("RestartMode=direct bypasses OnFailure; review the vendor unit")


def atomic_write(path, data, mode=0o644):
    """Replace a file atomically within its existing parent directory."""
    fd, name = tempfile.mkstemp(prefix=".attic-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(name):
    path = RUNTIME / name
    if path.exists() or path.is_symlink():
        secure_path(path)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def unit_paths():
    return {UNITS / HANDLER: SERVICE.encode(),
            UNITS / f"{TARGET}.d/90-attic-recovery.conf": DROPIN.encode()}


def verify_owned_units():
    """Never replace or remove an unrelated unit using our reserved names."""
    for path in unit_paths():
        for parent in path.parents:
            if parent.exists() or parent.is_symlink():
                secure_path(parent)
        if path.exists() or path.is_symlink():
            secure_path(path)
            if not path.read_text().startswith(MARKER):
                raise ValueError(f"Refusing to overwrite an unmanaged file: {path}")
    # A persistent unit/drop-in takes precedence over our runtime files.
    for relative in (HANDLER, f"{TARGET}.d/90-attic-recovery.conf"):
        path = Path("/etc/systemd/system") / relative
        if path.exists() or path.is_symlink():
            raise ValueError(f"Conflicting persistent file: {path}")


def install(config_path):
    check_host(install=True)
    source = Path(__file__).absolute()
    secure_path(source)
    secure_path(config_path)
    if not str(source).startswith("/mnt/") or not str(config_path).startswith("/mnt/"):
        raise ValueError("Install from a persistent dataset under /mnt")
    config = read_config(config_path)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    secure_path(RUNTIME)
    with locked("install.lock"):
        verify_owned_units()
        files = {RUNTIME / "avahi_recovery.py": source.read_bytes(),
                 RUNTIME / "config.json": (json.dumps(config) + "\n").encode(),
                 **unit_paths()}
        for path in files:
            if path.exists() or path.is_symlink():
                secure_path(path)
        old = {path: path.read_bytes() if path.exists() else None for path in files}
        try:
            for path, data in files.items():
                path.parent.mkdir(mode=0o755, exist_ok=True)
                if old[path] != data:
                    atomic_write(path, data)
            systemctl("daemon-reload")
            hooks = systemctl("show", TARGET, "--property=OnFailure", "--value").split()
            if HANDLER not in hooks:
                raise ValueError("OnFailure was overridden by another drop-in")
            fragment = systemctl("show", HANDLER, "--property=FragmentPath", "--value")
            if fragment != str(UNITS / HANDLER):
                raise ValueError(f"Unexpected recovery unit: {fragment}")
            if state() == "failed":
                systemctl("start", "--no-block", HANDLER)
        except Exception:
            # Restore our own files even if reload or verification fails.
            for path, data in old.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data)
            systemctl("daemon-reload")
            raise
    print("Avahi recovery installed for this boot.")


def uninstall():
    check_host()
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    secure_path(RUNTIME)
    with locked("install.lock"):
        verify_owned_units()
        # Detach the trigger first so another failure cannot requeue the handler.
        dropin = UNITS / f"{TARGET}.d/90-attic-recovery.conf"
        dropin.unlink(missing_ok=True)
        systemctl("daemon-reload")
        if (UNITS / HANDLER).exists():
            systemctl("stop", HANDLER)
            (UNITS / HANDLER).unlink()
        systemctl("daemon-reload")
        for name in ("avahi_recovery.py", "config.json"):
            path = RUNTIME / name
            if path.exists() or path.is_symlink():
                secure_path(path)
                path.unlink()
    print("Avahi recovery runtime integration removed.")


def state():
    return systemctl("show", TARGET, "--property=ActiveState", "--value")


def can_recover():
    # Do not resurrect an intentionally stopped service or run during shutdown.
    manager = systemctl("show", "--property=SystemState", "--value")
    return manager in ("running", "degraded") and state() == "failed"


def claim_attempt(path, now, config):
    """Persist a rolling per-boot budget before attempting a restart."""
    if path.exists() or path.is_symlink():
        secure_path(path)
        history = json.loads(path.read_text())
        if not isinstance(history, list) or any(type(x) not in (int, float) for x in history):
            raise ValueError("Invalid attempt history; refusing recovery")
    else:
        history = []
    recent = [x for x in history if x > now - config["window_seconds"]]
    if len(recent) >= config["attempts"]:
        return False
    atomic_write(path, json.dumps([*recent, now]).encode(), 0o600)
    return True


def recover():
    check_host()
    secure_path(RUNTIME)
    secure_path(RUNTIME / "config.json")
    config = read_config(RUNTIME / "config.json")
    with locked("recovery.lock"):
        for _ in range(config["attempts"]):
            if not can_recover():
                return
            time.sleep(config["delay_seconds"])
            if not can_recover():
                return
            if not claim_attempt(RUNTIME / "attempts.json", time.monotonic(), config):
                raise ValueError("Recovery budget exhausted; inspect Avahi journal and configuration")
            print("Avahi failed; queuing a bounded recovery attempt", flush=True)
            systemctl("reset-failed", TARGET)
            # Never wait on a start job from inside an OnFailure handler.
            systemctl("start", "--no-block", TARGET)
            for _ in range(15):
                time.sleep(1)
                current = state()
                if current == "active":
                    print("Avahi is active again", flush=True)
                    return
                if current == "failed":
                    break
                if current in ("inactive", "deactivating"):
                    return
            else:
                raise ValueError("Avahi did not settle within 15 seconds; inspect systemctl status")
        raise ValueError("Recovery attempts exhausted; manual diagnosis required")


def apply(config_path: Path) -> None:
    """Reinstall runtime files from the current configuration without resetting budget."""
    install(config_path)


def configure(config_path: Path, *, enabled: bool) -> None:
    """Edit a candidate, validate it and apply it only if this tweak is enabled.

    Invalid edits never replace the saved configuration. If apply fails, restore
    the saved configuration; install handles rollback of its runtime files.
    """
    check_host(install=True)
    secure_path(config_path)
    previous = config_path.read_bytes()
    fd, temporary = tempfile.mkstemp(prefix=".avahi-edit-", suffix=".json", dir=config_path.parent)
    candidate = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(previous)
        editor = shlex.split(os.environ.get("EDITOR", "vi"))
        if not editor:
            raise ValueError("EDITOR must name an editor")
        subprocess.run([*editor, str(candidate)], check=True)
        secure_path(candidate)
        read_config(candidate)
        updated = candidate.read_bytes()
        if updated == previous:
            return
        atomic_write(config_path, updated)
        try:
            if enabled:
                apply(config_path)
        except Exception:
            atomic_write(config_path, previous)
            raise
    finally:
        candidate.unlink(missing_ok=True)


def status() -> str:
    """Report runtime installation separately from daemon health; never modify it."""
    if sys.platform != "linux" or not Path(SYSTEMCTL).is_file():
        return "runtime unavailable (requires Linux/systemd)"
    files = [*unit_paths(), RUNTIME / "avahi_recovery.py", RUNTIME / "config.json"]
    present = [path.exists() for path in files]
    if not any(present):
        return "not installed for this boot"
    if not all(present):
        return "incomplete runtime installation"
    hooks = systemctl("show", TARGET, "--property=OnFailure", "--value").split()
    fragment = systemctl("show", HANDLER, "--property=FragmentPath", "--value")
    if HANDLER not in hooks or fragment != str(UNITS / HANDLER):
        return "runtime files present but systemd integration is not effective"
    handler = systemctl("show", HANDLER, "--property=ActiveState", "--value")
    return f"installed; Avahi={state()}; recovery={handler}"


def is_enabled() -> bool:
    """Read activation from this checkout; direct script calls do not change it."""
    path = Path(__file__).absolute().parents[2] / "local/enabled.json"
    if path.is_symlink():
        raise ValueError(f"Symlink is not allowed: {path}")
    if not path.exists():
        return False
    secure_path(path)
    names = json.loads(path.read_text())
    if (not isinstance(names, list)
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                   for name in names)
            or len(names) != len(set(names))):
        raise ValueError("local/enabled.json must be a list of unique tweak names")
    return "avahi-recovery" in names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    for name, help_text in (
        ("install", "Install recovery for this boot"),
        ("uninstall", "Remove runtime integration"),
        ("config", "Edit, validate and apply settings for an enabled tweak"),
        ("status", "Show runtime status without changing the system"),
        ("boot", "Restore an enabled tweak after reboot"),
        ("apply", "Apply updated code and settings for an enabled tweak"),
        ("recover", "Internal systemd failure handler"),
    ):
        commands.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return
    if args.action == "status":
        print(status())
        return
    if args.action == "recover":
        recover()
        return
    check_host()
    config_path = Path(__file__).absolute().with_name("config.json")
    if args.action == "install":
        install(config_path)
    elif args.action == "uninstall":
        uninstall()
    elif args.action == "config":
        configure(config_path, enabled=is_enabled())
    else:
        if not is_enabled():
            raise ValueError("avahi-recovery is not enabled; use attic avahi-recovery install first")
        apply(config_path)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"attic-avahi: {error}", file=sys.stderr)
        sys.exit(1)
