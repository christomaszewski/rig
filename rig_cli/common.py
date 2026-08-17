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
