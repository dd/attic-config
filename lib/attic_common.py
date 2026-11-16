"""Helpers shared by the attic dispatcher, setup and tweak scripts; standard library only."""

import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION = re.compile(r"(?:TrueNAS-(?:SCALE-)?)?(\d+(?:\.\d+)+)")


def secure_path(path: Path) -> None:
    """Check root ownership, POSIX write permissions and symlinks before trusting a path."""
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Symlink is not allowed: {item}")
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Must be root-owned and not group/world-writable: {item}")


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
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
        Path(name).unlink(missing_ok=True)


def truenas_version() -> str:
    """Return the bare release from /etc/version, with or without a product prefix."""
    content = Path("/etc/version").read_text().strip()
    match = VERSION.fullmatch(content)
    if not match:
        raise ValueError(f"This installer requires TrueNAS; /etc/version contains: {content}")
    return match.group(1)


def read_protected(path: Path) -> str | None:
    """Read local state; only a missing file is absent, a denied read stays an error."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Symlink is not allowed: {path}")
    if os.geteuid() == 0:
        secure_path(path)
    return path.read_text()


def read_enabled(root: Path) -> list[str]:
    content = read_protected(root / "local/enabled.json")
    if content is None:
        return []
    names = json.loads(content)
    if (not isinstance(names, list)
            or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in names)
            or len(names) != len(set(names))):
        raise ValueError("local/enabled.json must be a list of unique tweak names")
    return sorted(names)


def parse_object(content: str, path: Path) -> dict:
    config = json.loads(content)
    if not isinstance(config, dict):
        raise ValueError(f"Configuration must be a JSON object: {path}")
    return config


def config_paths(root: Path, name: str) -> tuple[Path, Path]:
    """Defaults are versioned beside the tweak; overrides survive repository updates."""
    return root / "tweaks" / name / "config.json", root / "local/config" / f"{name}.json"


def read_config(root: Path, name: str, validate) -> dict:
    """Merge local overrides over versioned defaults and validate the result."""
    defaults, override = config_paths(root, name)
    config = parse_object(read_protected(defaults) or "{}", defaults)
    content = read_protected(override)
    if content is not None:
        config.update(parse_object(content, override))
    validate(config)
    return config


def edit_config(root: Path, name: str, validate, apply=None) -> None:
    """Edit the effective configuration and store only values that differ from defaults.

    Invalid edits never replace saved overrides. A key removed in the editor
    returns to its default. If apply fails, the previous overrides are restored.
    """
    current = read_config(root, name, validate)
    defaults_path, override = config_paths(root, name)
    defaults = parse_object(read_protected(defaults_path) or "{}", defaults_path)
    override.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        secure_path(override.parent)
    fd, temporary = tempfile.mkstemp(prefix=".edit-", suffix=".json", dir=override.parent)
    candidate = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(current, stream, indent=2)
            stream.write("\n")
        editor = shlex.split(os.environ.get("EDITOR", "vi"))
        if not editor:
            raise ValueError("EDITOR must name an editor")
        subprocess.run([*editor, str(candidate)], check=True)
        content = read_protected(candidate)
        if content is None:
            raise ValueError("The editor removed the temporary file; nothing was saved")
        edited = {**defaults, **parse_object(content, candidate)}
        validate(edited)
        if edited == current:
            return
        previous = override.read_bytes() if override.exists() else None
        write_overrides(override, {key: value for key, value in edited.items()
                                   if key not in defaults or defaults[key] != value})
        try:
            if apply:
                apply()
        except Exception:
            if previous is None:
                override.unlink(missing_ok=True)
            else:
                atomic_write(override, previous, 0o600)
            raise
    finally:
        candidate.unlink(missing_ok=True)


def write_overrides(path: Path, changes: dict) -> None:
    if changes:
        atomic_write(path, (json.dumps(changes, indent=2) + "\n").encode(), 0o600)
    else:
        path.unlink(missing_ok=True)
