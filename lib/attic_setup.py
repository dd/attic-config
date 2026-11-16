#!/usr/bin/env python3
"""Register an in-place attic checkout with TrueNAS; no pip packages or venv."""

import argparse
from importlib.machinery import SourceFileLoader
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).absolute().parent))
import attic_common as common  # noqa: E402

ROOT = Path(__file__).absolute().parents[1]
MIDCLT = "/usr/bin/midclt"
PYTHON = "/usr/bin/python3"
MARKER = "attic-config: managed boot v1"
SUPPORTED = "25.10.7"


def call(method: str, *arguments):
    """Call local middleware as root, using JSON argv and a bounded wait."""
    result = subprocess.run(
        [MIDCLT, "call", method, *[json.dumps(arg) for arg in arguments]],
        check=True, text=True, capture_output=True, timeout=60,
    )
    return json.loads(result.stdout)


def check_host(action: str) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run setup as root on TrueNAS, not on your workstation")
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or newer is required")
    version = common.truenas_version()
    if action == "install" and version != SUPPORTED:
        raise ValueError(f"Unreviewed TrueNAS version: {version}")
    if not str(ROOT).startswith("/mnt/"):
        raise ValueError("Place the checkout in a persistent dataset under /mnt first")
    if not Path(MIDCLT).is_file() or not Path(PYTHON).is_file():
        raise ValueError("System python3 and midclt are required")
    if not (ROOT / "attic").is_file() or not (ROOT / "tweaks").is_dir():
        raise ValueError("Run attic setup from a complete attic-config checkout")


def prepare_permissions(repair: bool) -> None:
    """Validate the whole checkout before optionally securing only its own files.

    Parent directories are never modified. Symlinks, hard-linked files and
    special files are rejected before any ownership changes. ACLs must be
    configured separately by the dataset administrator.
    """
    for parent in ROOT.parents:
        info = parent.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError(f"Parent must be root-owned and not writable by others: {parent}")
    paths = sorted([ROOT, *ROOT.rglob("*")], key=lambda path: len(path.parts))
    for path in paths:
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError(f"Only ordinary files and directories are allowed: {path}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError(f"Hard-linked file is not allowed: {path}")
        if not repair and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError(f"Unsafe permissions: {path}; run attic setup install first")
    if repair:
        for path in paths:
            mode = stat.S_IMODE(path.stat().st_mode) & ~0o6022
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, mode, follow_symlinks=False)


def load_dispatcher():
    """Reuse the protected dispatcher's state handling and management lock."""
    loader = SourceFileLoader("attic_setup_dispatcher", str(ROOT / "attic"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def boot_command() -> str:
    return shlex.join([PYTHON, str(ROOT / "attic"), "boot"])


def command_tokens(task) -> list[str]:
    try:
        return shlex.split(task.get("command") or "")
    except ValueError:
        return []


def select_task(tasks, *, adopt: bool):
    """Find one managed task, or adopt exactly this checkout's manual boot task.

    The marker identifies one attic installation per NAS. A moved checkout may
    update that task; duplicate markers or unexpected commands require review.
    """
    marked = [task for task in tasks if task.get("comment") == MARKER]
    exact = [task for task in tasks if task.get("type") == "COMMAND"
             and task.get("when") == "POSTINIT"
             and command_tokens(task) == [PYTHON, str(ROOT / "attic"), "boot"]]
    candidates = {task["id"]: task for task in marked + exact}
    if len(candidates) > 1:
        raise ValueError("Multiple attic boot tasks found; remove duplicates in TrueNAS UI first")
    if marked:
        task = marked[0]
        tokens = command_tokens(task)
        if (task.get("type") != "COMMAND" or task.get("when") != "POSTINIT"
                or len(tokens) != 3 or tokens[0] != PYTHON or tokens[2] != "boot"
                or not tokens[1].startswith("/mnt/") or Path(tokens[1]).name != "attic"):
            raise ValueError("Managed task was changed unexpectedly; inspect it in TrueNAS UI")
        return task
    if exact:
        if not adopt:
            raise ValueError("Manual boot task found; run setup install to adopt it first")
        return exact[0]
    return None


def install(dispatcher, timeout: int | None) -> None:
    task = select_task(call("initshutdownscript.query"), adopt=True)
    desired = {
        "type": "COMMAND", "command": boot_command(), "when": "POSTINIT",
        "enabled": True, "timeout": timeout if timeout is not None else (task["timeout"] if task else 120),
        "comment": MARKER,
    }
    if task is None:
        call("initshutdownscript.create", desired)
    elif any(task.get(key) != value for key, value in desired.items()):
        call("initshutdownscript.update", task["id"], desired)
    actual = select_task(call("initshutdownscript.query"), adopt=False)
    if actual is None or any(actual.get(key) != value for key, value in desired.items()):
        raise ValueError("Boot task verification failed; inspect TrueNAS tasks and rerun setup")
    # Never reset existing selections. An empty installation enables no tweaks.
    names = dispatcher.read_enabled()
    if not (ROOT / "local/enabled.json").exists():
        dispatcher.write_enabled(names)
    print(f"Installed in place: {ROOT}")
    print(f"Post Init task {actual['id']}: {actual['command']}")
    print("No tweaks were enabled or started. Next: ./attic list, then sudo ./attic NAME install")


def uninstall(dispatcher) -> None:
    task = select_task(call("initshutdownscript.query"), adopt=False)
    # A stale old copy must not disable the installation at its new location.
    if task and command_tokens(task) != [PYTHON, str(ROOT / "attic"), "boot"]:
        raise ValueError("Boot task points to another checkout; run uninstall from that location")
    if task and task.get("enabled"):
        call("initshutdownscript.update", task["id"], {"enabled": False})
        current = select_task(call("initshutdownscript.query"), adopt=False)
        if current and current.get("enabled"):
            raise ValueError("Could not disable Post Init; no tweaks were removed")
    names = dispatcher.read_enabled()
    failed = []
    available = dispatcher.discover()
    for name in list(names):
        try:
            # A tweak deleted from the checkout has no code left to run its cleanup.
            if name not in available:
                print(f"{name}: not in this checkout; dropping it from the boot list", file=sys.stderr)
            elif dispatcher.run_tweak(name, ["uninstall"]):
                failed.append(name)
                continue
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f"{name}: {error}", file=sys.stderr)
            failed.append(name)
            continue
        names.remove(name)
        dispatcher.write_enabled(names)
    if failed:
        raise ValueError("Cleanup failed for " + ", ".join(failed)
                         + "; boot task remains disabled. Fix errors and repeat setup uninstall")
    if task:
        call("initshutdownscript.delete", task["id"])
        if any(item["id"] == task["id"] for item in call("initshutdownscript.query")):
            raise ValueError("Task deletion was not confirmed; inspect TrueNAS tasks")
    print("Integration removed. Source files, configs and local state remain in the dataset.")


def status() -> None:
    task = select_task(call("initshutdownscript.query"), adopt=True)
    print(f"Checkout: {ROOT}")
    if task is None:
        print("No Post Init integration found")
    else:
        ownership = "managed" if task.get("comment") == MARKER else "manual (not adopted)"
        print(f"Post Init {task['id']}: {ownership}; enabled={task['enabled']}")
        print(task["command"])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="attic setup", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    install_parser = commands.add_parser("install", help="Secure checkout and register Post Init")
    install_parser.add_argument("--timeout", type=int, help="Post Init timeout in seconds (default: existing value or 120)")
    commands.add_parser("status", help="Inspect checkout path and Post Init integration")
    commands.add_parser("uninstall", help="Remove enabled tweaks, then the managed Post Init task")
    args = parser.parse_args(argv)
    timeout = getattr(args, "timeout", None)
    if timeout is not None and not 10 <= timeout <= 3600:
        parser.error("--timeout must be between 10 and 3600 seconds")
    check_host(args.action)
    if args.action == "status":
        status()
        return
    prepare_permissions(repair=args.action == "install")
    dispatcher = load_dispatcher()
    with dispatcher.management_lock():
        dispatcher.read_enabled()  # Refuse corrupt state before middleware writes.
        if args.action == "install":
            install(dispatcher, timeout)
        else:
            uninstall(dispatcher)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"attic setup: local command failed: {error.stderr or error}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"attic setup: {error}", file=sys.stderr)
        sys.exit(1)
