"""``{{var}}`` interpolation — the vehicle-local vars mechanism's one primitive.

Deliberately NOT ``${VAR}``: compose-style refs flow through configs to launcher/compose env
expansion and must never be eaten by rig. Rules (settled): marker names are lowercase
``[a-z][a-z0-9_]*`` (the shell spelling of the same var is ``RIG_VAR_<name>`` /
``RIG_VEHICLE_ID`` — only rig-namespaced environment feeds vars, never arbitrary env); a scalar
that is ENTIRELY one marker keeps the var's native type (``port: "{{base_port}}"`` renders as an
int); an unknown var is a HARD error listing what's available (a literal ``{{…}}`` reaching a
driver at 2am is the failure mode this exists to prevent); substitution only — no arithmetic, no
conditionals.
"""
from __future__ import annotations

import re

from . import RigError

MARKER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def referenced_vars(value) -> set[str]:
    """Every var name referenced anywhere in a str/list/dict tree (for bake's example-file
    generator, provision's satisfaction check, and fleet detection)."""
    out: set[str] = set()
    if isinstance(value, str):
        out.update(MARKER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= referenced_vars(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= referenced_vars(v)
    return out


def substitute_scalar(text: str, variables: dict, *, where: str):
    """One string: whole-marker keeps the var's type; embedded markers stringify. Unknown -> error."""
    whole = MARKER.fullmatch(text.strip())
    if whole:
        name = whole.group(1)
        if name not in variables:
            raise RigError(_unknown(name, variables, where))
        return variables[name]

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            raise RigError(_unknown(name, variables, where))
        return str(variables[name])

    return MARKER.sub(_sub, text)


def substitute(value, variables: dict, *, where: str):
    """Recursive interpolation over a YAML-shaped tree; non-strings pass through untouched."""
    if isinstance(value, str):
        return substitute_scalar(value, variables, where=where)
    if isinstance(value, dict):
        return {k: substitute(v, variables, where=where) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables, where=where) for v in value]
    return value


def resolve_map(raw: dict, *, where: str, max_depth: int = 5) -> dict:
    """Resolve a var mapping whose VALUES may reference other vars in the same mapping
    (`camera_ip: 10.160.{{vehicle_id}}.25` as a fleet default). Iterates to a fixpoint;
    no fixpoint within `max_depth` passes = a reference cycle -> error."""
    resolved = dict(raw)
    for _ in range(max_depth):
        changed = False
        for key, value in resolved.items():
            if isinstance(value, str) and MARKER.search(value):
                names = set(MARKER.findall(value))
                if key in names:
                    raise RigError(f"{where}: var '{key}' references itself")
                ready = {n: resolved[n] for n in names
                         if n in resolved and not (isinstance(resolved[n], str)
                                                   and MARKER.search(str(resolved[n])))}
                if names <= set(ready):
                    resolved[key] = substitute_scalar(value, ready, where=where)
                    changed = True
        if not any(isinstance(v, str) and MARKER.search(v) for v in resolved.values()):
            return resolved
        if not changed:
            break
    cyclic = sorted(k for k, v in resolved.items() if isinstance(v, str) and MARKER.search(v))
    raise RigError(f"{where}: unresolvable var reference cycle or unknown var among: "
                   f"{', '.join(cyclic)}")


def _unknown(name: str, variables: dict, where: str) -> str:
    avail = ", ".join(sorted(variables)) or "(none)"
    return (f"{where}: unknown var '{{{{{name}}}}}' — available: {avail}. Define it under `vars:` "
            f"(vehicle.yaml or vehicle.local.yaml), or export RIG_VAR_{name}")
