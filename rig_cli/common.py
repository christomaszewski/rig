"""Shared helpers: YAML loading, stderr printing, path hygiene."""
from __future__ import annotations

import sys
from pathlib import Path

from . import RigError

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "rig: PyYAML is required (every launcher needs it too).\n"
        "     pip install pyyaml   |   apt install python3-yaml\n"
    )
    raise


def load_yaml(path: Path) -> dict:
    """Load a YAML file into a dict, with rig-flavored error messages."""
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        raise RigError(f"file not found: {path}")
    except yaml.YAMLError as exc:
        hint = ""
        try:  # an UNQUOTED {{var}} at the start of a value is YAML flow-mapping syntax — the
            #   number-one marker trap ("found unhashable key"). Diagnose it by name.
            import re
            if re.search(r":\s*\{\{", path.read_text()):
                hint = ('\n  (a {{var}} marker that STARTS a value must be quoted — YAML reads '
                        'bare {{ as a mapping: use  vehicle_id: "{{vehicle_id}}"  — mid-string '
                        'markers like  url: rtsp://10.{{vehicle_id}}.80  are fine unquoted)')
        except OSError:
            pass
        raise RigError(f"invalid YAML in {path}: {exc}{hint}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RigError(f"expected a YAML mapping at the top of {path}")
    return data


def eprint(*args, **kwargs) -> None:
    """Print to stderr (stdout stays clean for machine-readable output)."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def print_table(rows: list[tuple]) -> None:
    """Aligned columns to stdout — the one table renderer (pkg list/search, registry list,
    artifact list all draw the same shape)."""
    if not rows:
        return
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip())


def safe_component(value: str, *, what: str) -> str:
    """ONE path segment and nothing else — for a name rig joins onto a directory it then creates,
    replaces or DELETES (an artifact tag names var/bake/<tag>, which bake rmtree's first).
    pathlib makes the hazard silent: ``base / "/etc"`` IS ``/etc``, and ``base / "../../config"``
    walks out of var/bake into the deployment. Rejects the empty string, ``.``/``..``, any
    separator, and surrounding whitespace. Returns the value so a caller can bind it inline."""
    bad = (not value or value in (".", "..") or "/" in value or "\\" in value or "\0" in value
           or value != value.strip())
    if bad:
        raise RigError(f"{what}: {value!r} must be a single path component — no separators, not "
                       f"'.' or '..', not empty (it names a directory rig creates and removes)")
    return value


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so that a reader, a power cut or an interrupt sees the old
    file or the new one, never a truncated one: write a temporary file in the same directory,
    flush it to disk, and rename it over the target. For every file rig owns and reads back —
    a run's manifest.yaml sealed just before a vehicle is powered off, rig.lock, the machine
    identity file, a pushed roster.

    A symlink at ``path`` is written THROUGH (its target is replaced), as an in-place write
    would. A hard link is not: the rename gives ``path`` its own file, so a write to an export's
    hardlinked copy no longer reaches into the run it was linked from. An existing file keeps
    its mode; a new one gets the mode a plain open() would have given it."""
    import os
    import tempfile
    path = Path(os.path.realpath(path))
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """atomic_write_bytes for text (UTF-8)."""
    atomic_write_bytes(path, text.encode("utf-8"))


def checked_git_url(url, *, what: str) -> str:
    """A git remote rig is about to hand to `git fetch` or `git clone`. It arrives from a registry
    package manifest, a rigging.yaml or another repository's .gitmodules: data, never an option.
    git reads any argument that begins with `-` as one (and some options name a program to run),
    so such a value is refused here by name, before it is ever placed on a command line. Callers
    also pass `--` before the URL; this check is what gives the user a sentence instead of a git
    usage error. Returns the URL so a caller can bind it inline."""
    text = "" if url is None else str(url)
    if not text.strip():
        raise RigError(f"{what}: a git URL is required")
    if text.startswith("-") or any(ch in text for ch in "\n\r\0"):
        raise RigError(f"{what}: {text!r} is not a git URL (it may not begin with '-' or hold "
                       f"control characters) — use a URL (https://…, ssh://…, git@host:path) or "
                       f"a path")
    return text


def safe_extract(tf, dest: Path) -> None:
    """Extract an archive rig did not necessarily write (a run copied off a vehicle, an artifact
    from someone else) without letting a member escape ``dest``: tarfile's ``data`` filter where
    Python has it (3.12+, and backports from 3.9.17 / 3.10.12 / 3.11.4), else the same rules by
    hand — no absolute or ``..`` names, no links leaving ``dest``, no device or fifo members."""
    import tarfile
    if hasattr(tarfile, "data_filter"):
        try:
            tf.extractall(dest, filter="data")
        except tarfile.FilterError as exc:
            raise RigError(f"refusing an unsafe archive member: {exc}") from exc
        return
    root = Path(dest).resolve()
    members = []
    for member in tf.getmembers():
        name = Path(member.name)
        target = (root / member.name).resolve()
        if name.is_absolute() or ".." in name.parts or not (target == root or target.is_relative_to(root)):
            raise RigError(f"archive member {member.name!r} would extract outside {dest}")
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname if member.issym() else root / member.linkname)
            if Path(member.linkname).is_absolute() or not link.resolve().is_relative_to(root):
                raise RigError(f"archive link {member.name!r} -> {member.linkname!r} leaves {dest}")
        elif not (member.isfile() or member.isdir()):
            raise RigError(f"archive member {member.name!r} is not a file, directory or link")
        members.append(member)
    tf.extractall(dest, members=members)  # noqa: S202 — every member checked above


def links_leaving(root: Path) -> list[tuple[str, str]]:
    """(tree-relative path, link target) of every symlink under ``root`` whose resolved target is
    not inside ``root`` — dangling ones included. For a tree that came out of an archive and is
    about to be MOVED or written into: extraction only guarantees that a link stays inside the
    directory it was extracted to, so a relative link that was contained there can point
    outside once its tree is moved, and any later write to that path follows it."""
    import os
    base = os.path.realpath(root)
    out: list[tuple[str, str]] = []
    for folder, dirs, files in os.walk(root):  # never follows links
        for name in dirs + files:
            path = os.path.join(folder, name)
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            if target != base and not target.startswith(base + os.sep):
                out.append((os.path.relpath(path, root), os.readlink(path)))
    return sorted(out)


def contained(path: Path, root: Path) -> bool:
    """True iff ``path`` resolves to somewhere STRICTLY inside ``root`` — symlinks resolved on
    both sides, so a link out of the tree fails too. The check behind every "write this under
    the tree" step whose relative path came from data rather than from rig."""
    try:
        target, base = path.resolve(), root.resolve()
    except OSError:
        return False
    return target != base and target.is_relative_to(base)
