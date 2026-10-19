"""Inspect and remove generated deployment artifacts, independently of Docker and run data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

import yaml

from . import RigError
from .common import eprint, load_yaml, print_table, safe_component


def paths(root: Path, tag: str, *, include_unbaked=False) -> dict[str, Path]:
    safe_component(tag, what="artifact tag")
    result = {"archive": root / "var/artifacts" / f"{tag}.tar.gz",
              "staging": root / "var/bake" / tag}
    if include_unbaked:
        result["unbaked"] = root / "var/unbaked" / tag
    return result


def _exists(path):
    return path.exists() or path.is_symlink()


def _confirm(message, *, yes, hint):
    if yes:
        return
    if not sys.stdin.isatty():
        raise RigError(f"{message} Non-interactive input: {hint}")
    eprint(message)
    eprint("Continue? [y/N] ", end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        raise RigError("artifact operation cancelled; existing files unchanged")


def confirm_overwrite(root, tag, *, overwrite=False):
    """Return permission to replace the archive; call before rebuilding its staging tree."""
    archive = paths(root, tag)["archive"]
    if _exists(archive):
        _confirm(f"bake: artifact '{tag}' already exists at {archive}. Replace it?",
                 yes=overwrite, hint="choose a new --tag or pass --overwrite to replace it")
        return True
    return overwrite


def publish(staging, root, tag, *, overwrite=False):
    """Finish the archive before publication; a failed bake leaves the previous archive intact."""
    archive = paths(root, tag)["archive"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{tag}.", suffix=".tmp", dir=archive.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with tarfile.open(temporary, "w:gz") as tf:
            tf.add(staging, arcname=tag)
        if overwrite:
            temporary.replace(archive)
        else:
            try:
                os.link(temporary, archive)  # exclusive publication, including concurrent bakes
            except FileExistsError as exc:
                raise RigError(f"bake: {archive} appeared during the bake; choose a new --tag "
                               "or retry with --overwrite") from exc
    except (OSError, tarfile.TarError) as exc:
        raise RigError(f"bake: cannot publish {archive}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def _size(path):
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in files + [d for d in dirs if (Path(directory) / d).is_symlink()]:
            total += (Path(directory) / name).lstat().st_size
    return total


def _human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def _tags(root):
    tags = {p.name[:-7] for p in (root / "var/artifacts").glob("*.tar.gz")}
    for directory in (root / "var/bake", root / "var/unbaked"):
        if directory.is_dir():
            tags.update(p.name for p in directory.iterdir() if p.is_dir() or p.is_symlink())
    return sorted(tags)


def _documents(root, tag):
    locations = paths(root, tag, include_unbaked=True)
    archive = locations["archive"]
    if archive.is_file():
        try:
            with tarfile.open(archive) as tf:
                def read(name):
                    try:
                        member = tf.extractfile(f"{tag}/{name}")
                    except KeyError:
                        return {}
                    value = (yaml.safe_load(member.read()) or {}) if member else {}
                    if not isinstance(value, dict):
                        raise RigError(f"artifact {tag}: {name} must be a mapping")
                    return value
                return read("metadata.yaml"), read("rig.lock")
        except (tarfile.TarError, OSError, yaml.YAMLError) as exc:
            raise RigError(f"artifact {tag}: cannot read archive: {exc}") from exc
    for directory in (locations["staging"], locations["unbaked"] / tag, locations["unbaked"]):
        if (directory / "metadata.yaml").is_file():
            return load_yaml(directory / "metadata.yaml"), (
                load_yaml(directory / "rig.lock") if (directory / "rig.lock").is_file() else {})
    return {}, {}


def show(root, tag, *, format="yaml"):
    files = {kind: {"path": str(path), "size_bytes": _size(path)}
             for kind, path in paths(root, tag, include_unbaked=True).items() if _exists(path)}
    if not files:
        raise RigError(f"artifact {tag!r} not found in this deployment")
    metadata, lock = _documents(root, tag)
    result = dict(tag=tag, files=files, metadata=metadata,
                  build_catalog=lock.get("build_catalog", {}), packages=lock.get("packages", {}))
    print(json.dumps(result, indent=2, default=str) if format == "json"
          else yaml.safe_dump(result, sort_keys=False), end="\n" if format == "json" else "")
    return 0


def list_artifacts(root):
    tags = _tags(root)
    if not tags:
        print("no artifacts baked (var/artifacts is empty)")
        return 0
    rows = [("TAG", "VEHICLE", "TARGET", "CREATED", "PINNING", "PARENT", "ARCHIVE", "STAGING", "UNBAKED")]
    for tag in tags:
        try:
            meta, _ = _documents(root, tag)
        except RigError:
            meta = {}
        target = meta.get("target") or {}
        sizes = tuple(_human_size(_size(p)) if _exists(p) else "—"
                      for p in paths(root, tag, include_unbaked=True).values())
        rows.append((tag, str(meta.get("vehicle", "?")),
                     "/".join(str(target.get(k) or "?") for k in ("runtime", "arch")),
                     str(meta.get("created", "?")), str(meta.get("pinning", "?")),
                     str((meta.get("parent") or {}).get("tag", "—")), *sizes))
    print_table(rows)
    return 0


def remove(root, tags, *, dry_run=False, yes=False, include_unbaked=False):
    """Plan every deletion before confirming; never follow an artifact-directory symlink."""
    planned = []
    for tag in dict.fromkeys(tags):
        found = [(kind, path) for kind, path in paths(root, tag, include_unbaked=include_unbaked).items()
                 if _exists(path)]
        if not found:
            raise RigError(f"artifact {tag!r} not found (extracted trees require --include-unbaked)")
        for kind, path in found:
            if not path.parent.resolve().is_relative_to(root.resolve()):
                raise RigError(f"artifact cleanup refuses a directory redirected outside the deployment: {path.parent}")
            planned.append((kind, path, _size(path)))
    if not planned:
        eprint("artifact: nothing to remove")
        return 0
    for kind, path, size in planned:
        eprint(f"  {kind}: {path} ({_human_size(size)})")
    eprint(f"artifact: {len(planned)} path(s), {_human_size(sum(size for _, _, size in planned))} of file data")
    if dry_run:
        eprint("dry-run: no files removed")
        return 0
    _confirm("Remove these artifact files?" + (" Extracted trees may contain edits." if include_unbaked else ""),
             yes=yes, hint="pass --yes to confirm removal, or --dry-run to preview")
    for _, path, _ in planned:
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            raise RigError(f"artifact: could not remove {path}: {exc}") from exc
    return 0


def prune(root, *, keep=3, dry_run=False, yes=False, include_unbaked=False):
    if keep < 0:
        raise RigError("artifact prune: --keep must be zero or greater")
    archives = sorted((root / "var/artifacts").glob("*.tar.gz"),
                      key=lambda p: (p.lstat().st_mtime_ns, p.name), reverse=True)
    eprint(f"artifact prune: keeping the {min(keep, len(archives))} newest archive(s) by file modification time")
    return remove(root, [p.name[:-7] for p in archives[keep:]], dry_run=dry_run, yes=yes,
                  include_unbaked=include_unbaked)
