"""``~/.rig/config.yaml`` — THIS USER's rig settings (override: ``$RIG_HOME``). Today one key:

  data_dir: /Users/me/rig-data     # this user's run registry — `rig setup --data-dir`

Precedence for `data_dir`, most specific wins: a tree-local vehicle.local.yaml > THIS file >
the machine file (/etc/rig/vehicle.local.yaml, `sudo rig provision`) > vehicle.yaml. A laptop
needs no sudo for a registry; a vehicle keeps the machine-level fact. Written by `rig setup`
(interactive on first run, `--data-dir` non-interactively); a typo'd key refuses at load, so a
misspelled setting never silently does nothing.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import RigError
from .registries import rig_home

CONFIG_FILE = "config.yaml"
KEYS = {"data_dir"}


def user_config_file() -> Path:
    return rig_home() / CONFIG_FILE


def load_user_config() -> dict:
    path = user_config_file()
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RigError(f"{path}: invalid YAML ({exc})")
    if not isinstance(doc, dict):
        raise RigError(f"{path}: must be a mapping (data_dir: <absolute path>)")
    unknown = set(doc) - KEYS
    if unknown:
        raise RigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))} — it carries only: "
                       f"{', '.join(sorted(KEYS))}")
    return doc


def user_data_dir() -> str | None:
    """This user's registry home (absolute), None when unset or unreadable."""
    try:
        raw = load_user_config().get("data_dir")
    except RigError:
        return None
    raw = str(raw or "").strip()
    return raw or None


def save_user_config(**updates) -> Path:
    """Merge keys into the file (None deletes a key)."""
    doc = load_user_config()
    for key, value in updates.items():
        if key not in KEYS:
            raise RigError(f"user config: unknown key {key}")
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    path = user_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# rig user settings — written by `rig setup` (data_dir: this user's run registry;\n"
                    "# precedence: tree-local vehicle.local.yaml > THIS file > /etc/rig > vehicle.yaml).\n"
                    + (yaml.safe_dump(doc, sort_keys=False) if doc else ""))
    return path


def absolute_data_dir(value: str, *, what: str) -> str:
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise RigError(f"{what}: data_dir must be an ABSOLUTE path (a relative one would fork the "
                       f"registry per working directory), got '{value}'")
    return str(expanded)
