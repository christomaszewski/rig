"""Shared helpers: YAML loading and stderr printing."""
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
        raise RigError(f"invalid YAML in {path}: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RigError(f"expected a YAML mapping at the top of {path}")
    return data


def eprint(*args, **kwargs) -> None:
    """Print to stderr (stdout stays clean for machine-readable output)."""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)
