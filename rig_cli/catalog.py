"""services.yaml — the vehicle-independent catalog mapping a service name to where its repo lives.

Keys are *service* names (the routing key in each sensor config + each repo's deploy.yaml), which may
differ from the repo directory name (e.g. service ``sbg`` lives in repo ``sbg_driver``). ``path`` is
resolved relative to the rig repo root; for deployment these are git submodules under ``services/<name>``,
for local development they point at sibling checkouts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import RigError
from .common import load_yaml


@dataclass(frozen=True)
class ServiceEntry:
    service: str
    path: Path  # absolute path to the service repo


def load_catalog(root: Path) -> dict[str, ServiceEntry]:
    data = load_yaml(root / "services.yaml")
    services = data.get("services") or {}
    if not services:
        raise RigError("services.yaml has no `services:` entries")
    catalog: dict[str, ServiceEntry] = {}
    for name, spec in services.items():
        spec = spec or {}
        raw = spec.get("path")
        if not raw:
            raise RigError(f"services.yaml: service '{name}' is missing `path`")
        path = Path(raw)
        repo = path if path.is_absolute() else (root / path)
        catalog[name] = ServiceEntry(service=name, path=repo.resolve())
    return catalog
