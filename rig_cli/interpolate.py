"""``{{var}}`` interpolation — the vehicle-local vars mechanism's one primitive.

Deliberately NOT ``${VAR}``: compose-style refs flow through configs to launcher/compose env
expansion and must never be eaten by rig. Rules (settled): marker names are lowercase
``[a-z][a-z0-9_]*`` (the shell spelling of the same var is ``RIG_VAR_<name>`` /
``RIG_VEHICLE_ID`` — only rig-namespaced environment feeds vars, never arbitrary env); a scalar
that is ENTIRELY one marker keeps the var's native type (``port: "{{base_port}}"`` renders as an
int); an unknown var is a HARD error listing what's available (a literal ``{{…}}`` reaching a
driver at 2am is the failure mode this exists to prevent); substitution plus ONE mapping form —
still no arithmetic, no conditionals, no nesting.

The mapping form (v0.1.65): ``{{map <list_var> <template_var>}}``, whole-scalar only, renders a
LIST — one ``template`` expansion per element, ``{}`` as the placeholder. BOTH arguments are var
names: the template being a var is what lets one artifact serve field and SIL alike
(``peer_endpoint: tcp/10.160.1.{}:7447`` vs ``RIG_VAR_peer_endpoint=tcp/127.0.0.1:744{}``) via
the normal source tiering, with no nested-marker grammar. The canonical pairing is the derived
built-in ``fleet_peer_ids`` (``fleet_ids`` minus THIS vehicle's id — manifest.py derives it):
``connect: "{{map fleet_peer_ids peer_endpoint}}"``.
"""
from __future__ import annotations

import re

from . import RigError

MARKER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
MAP = re.compile(r"\{\{\s*map\s+([a-z][a-z0-9_]*)\s+([a-z][a-z0-9_]*)\s*\}\}")


def referenced_vars(value) -> set[str]:
    """Every var name referenced anywhere in a str/list/dict tree (for bake's example-file
    generator, provision's satisfaction check, and fleet detection) — map-form args included."""
    out: set[str] = set()
    if isinstance(value, str):
        out.update(MARKER.findall(value))
        for match in MAP.finditer(value):
            out.update(match.groups())
    elif isinstance(value, dict):
        for v in value.values():
            out |= referenced_vars(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= referenced_vars(v)
    return out


def _expand_map(match: re.Match, variables: dict, *, where: str) -> list:
    """``{{map <list_var> <template_var>}}`` -> [template.replace('{}', str(item)), ...].
    The list var may be a list/tuple or a comma-separated string (the shell tier delivers raw
    strings — RIG_VAR_fleet_ids=1,2,7); the template var must be a string carrying a ``{}``
    placeholder (a template without one renders every element identical — that's a mistake,
    say so)."""
    list_name, tmpl_name = match.groups()
    for name in (list_name, tmpl_name):
        if name not in variables:
            raise RigError(_unknown(name, variables, where))
    items = variables[list_name]
    if isinstance(items, str):
        items = [part.strip() for part in items.split(",") if part.strip()]
    if not isinstance(items, (list, tuple)):
        raise RigError(f"{where}: map var '{list_name}' must be a list (or a comma-separated "
                       f"string), not {type(items).__name__}")
    template = variables[tmpl_name]
    if not isinstance(template, str) or "{}" not in template:
        raise RigError(f"{where}: map template var '{tmpl_name}' must be a string containing "
                       f"the '{{}}' placeholder (got {template!r})")
    return [template.replace("{}", str(item)) for item in items]


def substitute_scalar(text: str, variables: dict, *, where: str):
    """One string: whole-marker keeps the var's type (a whole-scalar map form renders a LIST);
    embedded markers stringify. Unknown -> error; an embedded map form -> error (it cannot
    stringify meaningfully, and passing it through verbatim is the 2am literal)."""
    whole_map = MAP.fullmatch(text.strip())
    if whole_map:
        return _expand_map(whole_map, variables, where=where)
    if MAP.search(text):
        raise RigError(f"{where}: the {{{{map …}}}} form is whole-scalar only — it renders a "
                       f"LIST and cannot be embedded inside a longer string")
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
    for key, value in resolved.items():
        if isinstance(value, str) and MAP.search(value):
            raise RigError(f"{where}: '{key}' uses the {{{{map …}}}} form — map renders in CONFIG "
                           f"files (render time), not in vars: (it may reference derived "
                           f"built-ins like fleet_peer_ids that don't exist yet at load)")
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
