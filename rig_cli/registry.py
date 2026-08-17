"""Registry core — the package-registry data model every registry-facing feature shares.

A registry is a git repo (or plain local dir) of manifests — ``services/<name>/manifest.yaml``,
``profiles/``, ``overlays/``, ``suites/`` — plus ``registry.yaml`` metadata and a GENERATED
``index.json``. This module owns parsing, validation, and the deterministic index generator:
the same rules a registry's ``tools/validate`` and CI wrappers run, so there is exactly one
source of truth and no vendored validator copies to drift. Scaffolding lives in
``registry_scaffold``; the client-side cache/sync (registries.yaml) lands with M2.

Validation philosophy: collect EVERY issue (``Issue`` rows, registry-relative paths) rather
than stopping at the first — a registry author fixes a PR in one round trip. Only structural
impossibilities (no registry.yaml, unreadable YAML for registry.yaml itself, or a schema
version newer than this rig understands) raise ``RigError``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import RigError
from .common import eprint, load_yaml

SCHEMA = 1           # the registry schema version this rig writes
MAX_SUPPORTED = 1    # the highest schema this rig can read (client-side degrade-not-fail is M2)

KIND_DIRS = {"service": "services", "profile": "profiles", "overlay": "overlays", "suite": "suites"}

# Package/namespace names are registry identifiers (NOT instance names — those stay ROS-safe
# with underscores); hyphens are idiomatic here (`camera-service`, `siyi-zr30`).
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_REV = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")            # full git SHA-1 or SHA-256 — never short/branch
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_CONSTRAINT = re.compile(                                       # requires.service: name@^1.4 / name@1.4.2
    r"^(?:(?P<ns>[a-z][a-z0-9-]*)/)?(?P<name>[a-z][a-z0-9-]*)@(?P<caret>\^?)(?P<ver>\d+\.\d+(?:\.\d+)?)$")
_QUALIFIED_EXACT = re.compile(                                  # suite members: ns/name@X.Y.Z, nothing less
    r"^(?P<ns>[a-z][a-z0-9-]*)/(?P<name>[a-z][a-z0-9-]*)@(?P<ver>\d+\.\d+\.\d+)$")
_INSTANCE = re.compile(r"^[a-z][a-z0-9_]*$")                    # instance-scoped overlay targets


@dataclass(frozen=True)
class Issue:
    where: str    # registry-relative context, e.g. "profiles/siyi-zr30/manifest.yaml"
    message: str
    level: str = "error"  # "error" fails validation; "warning" surfaces without failing (e.g. an
    #                       overlay authored against an older service version — staleness signal)


@dataclass(frozen=True)
class Package:
    kind: str
    name: str
    version: str
    pkg_dir: Path   # absolute
    manifest: dict


@dataclass
class Registry:
    root: Path
    meta: dict                      # registry.yaml
    packages: dict[str, Package]    # bare name -> package; names are unique ACROSS kinds (index keys)

    @property
    def namespace(self) -> str:
        return str(self.meta.get("namespace") or "")


def constraint_satisfied(constraint_ver: str, caret: bool, version: str) -> bool:
    """Does exact ``version`` satisfy the profile's service constraint? Exact = equality (a two-part
    exact like ``@1.4`` means 1.4.0). Caret = same major AND >= the base — v1 keeps caret simple
    (no special major-0 rule); the resolved pin lands in the lockfile anyway."""
    want = [int(x) for x in constraint_ver.split(".")]
    have = [int(x) for x in version.split(".")]
    if not caret:
        return have == (want + [0, 0, 0])[:3]
    return have[0] == want[0] and have >= (want + [0, 0, 0])[:3]


# --- per-kind validation ----------------------------------------------------------------------

def _rel_payload(pkg: Package, raw: object, field: str, issues: list[Issue], *, must_exist: bool = True):
    """Validate a manifest-relative payload/schema path (inside the package dir, no .. / absolute);
    return the resolved Path or None."""
    where = f"{KIND_DIRS[pkg.kind]}/{pkg.name}/manifest.yaml"
    if not isinstance(raw, str) or not raw:
        issues.append(Issue(where, f"`{field}` must be a relative path string"))
        return None
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        issues.append(Issue(where, f"`{field}`: path must stay inside the package dir: {raw}"))
        return None
    path = pkg.pkg_dir / rel
    if must_exist and not path.is_file():
        issues.append(Issue(where, f"`{field}`: file not found: {raw}"))
        return None
    return path


def _load_payload_mapping(pkg: Package, path: Path, field: str, issues: list[Issue]) -> dict | None:
    where = f"{KIND_DIRS[pkg.kind]}/{pkg.name}/{path.relative_to(pkg.pkg_dir)}"
    try:
        data = load_yaml(path)
    except RigError as exc:
        issues.append(Issue(where, f"unreadable {field}: {exc}"))
        return None
    if not isinstance(data, dict):
        issues.append(Issue(where, f"{field} must be a YAML mapping"))
        return None
    return data


def _check_ref_resolves(value: str, kind: str, reg: Registry, where: str, issues: list[Issue],
                        *, what: str) -> None:
    """A bare in-registry name must exist (as `kind`); ns-qualified refs are checked in-registry when
    the ns is OURS, format-only otherwise (cross-registry — resolvable only at install time)."""
    ns, _, name = value.rpartition("/")
    if ns and ns != reg.namespace:
        return  # cross-registry: format was already checked by the caller's regex
    pkg = reg.packages.get(name)
    if pkg is None:
        issues.append(Issue(where, f"{what} '{value}' does not resolve in this registry"))
    elif pkg.kind != kind:
        issues.append(Issue(where, f"{what} '{value}' is a {pkg.kind}, expected a {kind}"))


def _validate_service(pkg: Package, reg: Registry, issues: list[Issue]) -> None:
    where = f"services/{pkg.name}/manifest.yaml"
    m = pkg.manifest
    source, image = m.get("source"), m.get("image")
    if not source and not image:
        issues.append(Issue(where, "a service needs `source:` (repo+rev) and/or `image:` (digest ref)"))
    if source is not None:
        if not isinstance(source, dict) or not source.get("repo"):
            issues.append(Issue(where, "`source` must be a mapping with `repo`"))
        else:
            rev = str(source.get("rev") or "")
            if not _REV.match(rev):
                issues.append(Issue(where, f"`source.rev` must be a full commit SHA (40/64 hex), got '{rev}'"))
            path = source.get("path")
            if path is not None and (Path(str(path)).is_absolute() or ".." in Path(str(path)).parts):
                issues.append(Issue(where, f"`source.path` must be a repo-relative dir: {path}"))
    if image is not None:
        ref = str((image or {}).get("ref") or "") if isinstance(image, dict) else ""
        if not _DIGEST.search(ref):
            issues.append(Issue(where, f"`image.ref` must be digest-pinned (…@sha256:<64hex>), got '{ref}'"))
        elif ":" in ref.split("@", 1)[0].rsplit("/", 1)[-1]:
            issues.append(Issue(where, f"`image.ref` carries a TAG — digest-only refs, got '{ref}'"))
    schema_rel = (m.get("config") or {}).get("schema") if isinstance(m.get("config"), dict) else None
    if schema_rel is not None:
        _rel_payload(pkg, schema_rel, "config.schema", issues)


def _validate_profile(pkg: Package, reg: Registry, issues: list[Issue]) -> None:
    where = f"profiles/{pkg.name}/manifest.yaml"
    m = pkg.manifest
    req = (m.get("requires") or {}).get("service") if isinstance(m.get("requires"), dict) else None
    if not isinstance(req, str) or not (match := _CONSTRAINT.match(req)):
        issues.append(Issue(where, f"`requires.service` must be `[ns/]name@[^]X.Y[.Z]`, got {req!r}"))
    else:
        ref = (f"{match['ns']}/" if match["ns"] else "") + match["name"]
        _check_ref_resolves(ref, "service", reg, where, issues, what="requires.service")
        dep = reg.packages.get(match["name"]) if (not match["ns"] or match["ns"] == reg.namespace) else None
        if dep is not None and dep.kind == "service" and not constraint_satisfied(
                match["ver"], bool(match["caret"]), dep.version):
            issues.append(Issue(where, f"`requires.service` {req} is not satisfied by "
                                       f"{match['name']}@{dep.version} in this registry"))
    for block in (m.get("provides") or {}).get("sensor", []) if isinstance(m.get("provides"), dict) else []:
        if not isinstance(block, dict) or not isinstance(block.get("match"), list) or not block["match"]:
            issues.append(Issue(where, "`provides.sensor[]` entries need a non-empty `match:` list"))
    cfg = m.get("config") if isinstance(m.get("config"), dict) else {}
    payload_path = _rel_payload(pkg, cfg.get("payload"), "config.payload", issues)
    payload = _load_payload_mapping(pkg, payload_path, "payload", issues) if payload_path else None
    if payload is not None and "name" in payload:
        issues.append(Issue(where, "profile payloads are NAMELESS configs — the instance row supplies "
                                   "`name`; remove it from the payload"))
    if cfg.get("overrides_schema") is not None:
        schema_path = _rel_payload(pkg, cfg["overrides_schema"], "config.overrides_schema", issues)
        if schema_path and payload is not None:
            try:
                schema = json.loads(schema_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(Issue(where, f"`config.overrides_schema` is not valid JSON: {exc}"))
                return
            _schema_check(payload, schema, f"profiles/{pkg.name} payload", "", issues)
    # Fork-lineage staleness (the profile mirror of overlay authored_against): in-registry
    # parents only — cross-registry parents (the common public-base case) are surfaced
    # consumer-side by `pkg info`. Drift = WARNING; `rig pkg rebase` is the fix.
    based_on = m.get("based_on")
    if isinstance(based_on, str):
        match = _QUALIFIED_EXACT.match(based_on)
        if match and match["ns"] == reg.namespace:
            parent = reg.packages.get(match["name"])
            if parent is not None and parent.kind == "profile" and parent.version != match["ver"]:
                issues.append(Issue(where, f"based on {based_on}; this registry now carries "
                                           f"{parent.name}@{parent.version} — "
                                           f"`rig pkg rebase {pkg.name}` follows the parent",
                                    level="warning"))


def _validate_overlay(pkg: Package, reg: Registry, issues: list[Issue]) -> None:
    where = f"overlays/{pkg.name}/manifest.yaml"
    m = pkg.manifest
    targets = m.get("targets")
    if not isinstance(targets, list) or not targets:
        issues.append(Issue(where, "`targets` must be a non-empty list of {service: …} / {instance: …}"))
        targets = []
    for t in targets:
        if not isinstance(t, dict) or len(t) != 1 or next(iter(t)) not in ("service", "instance"):
            issues.append(Issue(where, f"each target is exactly one of `service:` or `instance:`, got {t!r}"))
            continue
        key, value = next(iter(t.items()))
        if key == "service":
            if not isinstance(value, str) or not _NAME.match(value.rpartition("/")[-1]):
                issues.append(Issue(where, f"target service name is invalid: {value!r}"))
            else:
                _check_ref_resolves(value, "service", reg, where, issues, what="target service")
        elif not isinstance(value, str) or not _INSTANCE.match(value):
            issues.append(Issue(where, f"target instance must be a ROS-safe name ([a-z][a-z0-9_]*): {value!r}"))
    cfg = m.get("config") if isinstance(m.get("config"), dict) else {}
    payload_path = _rel_payload(pkg, cfg.get("payload"), "config.payload", issues)
    payload = _load_payload_mapping(pkg, payload_path, "delta payload", issues) if payload_path else None
    if payload is not None:
        for banned in ("name", "service"):  # identity comes from the instance row, never a delta
            if banned in payload:
                issues.append(Issue(where, f"overlay deltas must not set `{banned}` — identity belongs "
                                           f"to the instance row"))
    # Staleness signal (tier 1): the promote-stamped provenance vs the registry's CURRENT service.
    # In-registry only (cross-registry refs resolve at install, not here); a version drift is a
    # WARNING — the delta may be fine, but its keys were authored against an older config surface.
    authored = m.get("authored_against")
    if isinstance(authored, dict) and isinstance(authored.get("service"), str):
        match = _QUALIFIED_EXACT.match(authored["service"])
        if match and match["ns"] == reg.namespace:
            dep = reg.packages.get(match["name"])
            if dep is not None and dep.kind == "service" and dep.version != match["ver"]:
                issues.append(Issue(where, f"authored against {authored['service']}; this registry "
                                           f"now carries {dep.name}@{dep.version} — re-verify the "
                                           f"delta's keys against the newer config surface, then "
                                           f"bump authored_against", level="warning"))


def _validate_suite(pkg: Package, reg: Registry, issues: list[Issue]) -> None:
    where = f"suites/{pkg.name}/manifest.yaml"
    m = pkg.manifest
    if (pkg.pkg_dir / "config").exists():
        issues.append(Issue(f"suites/{pkg.name}", "suites are REFERENCES ONLY — a config/ dir is rejected"))
    members = m.get("members")
    if not isinstance(members, dict):
        issues.append(Issue(where, "`members` must be a mapping of services/profiles/overlays lists"))
        return
    kind_of = {"services": "service", "profiles": "profile", "overlays": "overlay"}
    unknown = set(members) - set(kind_of)
    if unknown:
        issues.append(Issue(where, f"unknown member kinds: {', '.join(sorted(unknown))} (no nested suites)"))
    for plural, kind in kind_of.items():
        for ref in members.get(plural) or []:
            match = _QUALIFIED_EXACT.match(str(ref))
            if not match:
                issues.append(Issue(where, f"suite members are fully-qualified exact pins "
                                           f"(ns/name@X.Y.Z), got {ref!r}"))
                continue
            if match["ns"] == reg.namespace:
                dep = reg.packages.get(match["name"])
                if dep is None or dep.kind != kind:
                    issues.append(Issue(where, f"member '{ref}' does not resolve to a {kind} "
                                               f"in this registry"))
                elif dep.version != match["ver"]:
                    issues.append(Issue(where, f"member '{ref}' pins {match['ver']} but this registry "
                                               f"carries {dep.version}"))


_VALIDATORS = {"service": _validate_service, "profile": _validate_profile,
               "overlay": _validate_overlay, "suite": _validate_suite}


# --- minimal JSON-Schema subset (profile payload vs its own overrides_schema) ------------------
# Supports: type, properties, required, items, enum, additionalProperties:false. Unknown keywords
# are ignored (permissive) — the authoritative gate is this module, not a full draft validator,
# and pulling in a jsonschema dependency would break the pyyaml-only packaging promise.

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool,
          "number": (int, float), "null": type(None)}


def _schema_check(value, schema, ctx: str, keypath: str, issues: list[Issue]) -> None:
    if not isinstance(schema, dict):
        return
    label = f"{ctx}: {keypath or '(root)'}"
    t = schema.get("type")
    if t in _TYPES:
        ok = isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            issues.append(Issue(ctx, f"{label}: expected {t}, got {type(value).__name__}"))
            return
    if "enum" in schema and value not in schema["enum"]:
        issues.append(Issue(ctx, f"{label}: {value!r} not in enum {schema['enum']!r}"))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                issues.append(Issue(ctx, f"{label}: missing required key `{req}`"))
        if schema.get("additionalProperties") is False:
            for extra in set(value) - set(props):
                issues.append(Issue(ctx, f"{label}: unknown key `{extra}`"))
        for key, sub in props.items():
            if key in value:
                _schema_check(value[key], sub, ctx, f"{keypath}.{key}".lstrip("."), issues)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            _schema_check(item, schema["items"], ctx, f"{keypath}[{i}]", issues)


# --- load + validate --------------------------------------------------------------------------

def load_registry(root: Path, issues: list[Issue]) -> Registry:
    """Parse registry.yaml + every package manifest. Structural failures (no registry.yaml, a schema
    this rig can't read) raise; everything else lands in `issues`."""
    root = root.resolve()
    meta_path = root / "registry.yaml"
    if not meta_path.is_file():
        raise RigError(f"not a registry (no registry.yaml): {root}")
    meta = load_yaml(meta_path)
    schema = meta.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool):
        issues.append(Issue("registry.yaml", f"`schema` must be an integer, got {schema!r}"))
    elif schema > MAX_SUPPORTED:
        raise RigError(f"{root}: registry schema {schema} is newer than this rig understands "
                       f"(max {MAX_SUPPORTED}) — upgrade rig")
    ns = meta.get("namespace")
    if not isinstance(ns, str) or not _NAME.match(ns):
        issues.append(Issue("registry.yaml", f"`namespace` must match [a-z][a-z0-9-]*, got {ns!r}"))

    packages: dict[str, Package] = {}
    for kind, plural in KIND_DIRS.items():
        kind_dir = root / plural
        if not kind_dir.is_dir():
            continue
        for pkg_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            where = f"{plural}/{pkg_dir.name}/manifest.yaml"
            manifest_path = pkg_dir / "manifest.yaml"
            if not manifest_path.is_file():
                issues.append(Issue(f"{plural}/{pkg_dir.name}", "missing manifest.yaml"))
                continue
            try:
                m = load_yaml(manifest_path)
            except RigError as exc:
                issues.append(Issue(where, str(exc)))
                continue
            name, version = m.get("name"), str(m.get("version") or "")
            if m.get("kind") != kind:
                issues.append(Issue(where, f"`kind` must be '{kind}' under {plural}/, got {m.get('kind')!r}"))
            if name != pkg_dir.name:
                issues.append(Issue(where, f"`name` ({name!r}) must match the package dir ({pkg_dir.name})"))
            if not isinstance(name, str) or not _NAME.match(name or ""):
                issues.append(Issue(where, f"`name` must match [a-z][a-z0-9-]*, got {name!r}"))
                continue
            if kind == "service" and name in ("sensor", "project"):
                issues.append(Issue(where, f"service name '{name}' is reserved — `{name}:` is a "
                                           f"CLI porcelain prefix, so `{name}:<profile>` could "
                                           f"never reach this service's profiles"))
                continue
            if not _VERSION.match(version):
                issues.append(Issue(where, f"`version` must be exact X.Y.Z, got {version!r}"))
                continue
            if name in packages:
                other = packages[name]
                issues.append(Issue(where, f"package name '{name}' collides with "
                                           f"{KIND_DIRS[other.kind]}/{other.name} — names are unique "
                                           f"across ALL kinds (the index keys by bare name)"))
                continue
            packages[name] = Package(kind=kind, name=name, version=version, pkg_dir=pkg_dir, manifest=m)
    return Registry(root=root, meta=meta, packages=packages)


def validate_registry(root: Path, *, check_index: bool = True) -> tuple[Registry, list[Issue]]:
    issues: list[Issue] = []
    reg = load_registry(root, issues)
    for pkg in reg.packages.values():
        _VALIDATORS[pkg.kind](pkg, reg, issues)
    if check_index:
        index_path = reg.root / "index.json"
        if not index_path.is_file():
            issues.append(Issue("index.json", "missing — run tools/gen-index (rig registry index)"))
        else:
            try:
                current = index_path.read_text()
            except OSError as exc:
                current = ""
                issues.append(Issue("index.json", f"unreadable: {exc}"))
            if current and current != render_index(generate_index(reg)):
                issues.append(Issue("index.json", "STALE — regenerate with tools/gen-index "
                                                  "(rig registry index) and commit the result"))
    return reg, issues


# --- index -------------------------------------------------------------------------------------

_TIER_RANK = {"exact": 0, "glob": 1, "fallback": 2}


def _match_tier(identifier: str) -> str:
    if identifier == "*":
        return "fallback"
    return "glob" if any(ch in identifier for ch in "*?[") else "exact"


def generate_index(reg: Registry) -> dict:
    """The derived lookup surface: name → kind/version/path, sensor match-identifier → profiles by
    precedence tier, project tag → overlays/suites. Pure function of the manifests — regenerating
    twice yields identical bytes (CI's staleness check depends on that)."""
    packages = {
        name: {"kind": p.kind, "version": p.version, "path": f"{KIND_DIRS[p.kind]}/{p.name}"}
        for name, p in reg.packages.items()
    }
    sensors: dict[str, list[dict]] = {}
    projects: dict[str, list[str]] = {}
    for name, p in reg.packages.items():
        if p.kind == "profile" and isinstance(p.manifest.get("provides"), dict):
            for block in p.manifest["provides"].get("sensor") or []:
                for ident in (block.get("match") or []) if isinstance(block, dict) else []:
                    entry = {"profile": name, "tier": _match_tier(str(ident))}
                    sensors.setdefault(str(ident), []).append(entry)
        if p.kind in ("overlay", "suite") and p.manifest.get("project"):
            projects.setdefault(str(p.manifest["project"]), []).append(name)
    for entries in sensors.values():
        entries.sort(key=lambda e: (_TIER_RANK[e["tier"]], e["profile"]))
    for names in projects.values():
        names.sort()
    return {"schema": SCHEMA, "namespace": reg.namespace, "packages": packages,
            "sensors": sensors, "projects": projects}


def render_index(index: dict) -> str:
    return json.dumps(index, indent=2, sort_keys=True) + "\n"


def write_index(reg: Registry) -> Path:
    out = reg.root / "index.json"
    out.write_text(render_index(generate_index(reg)))
    return out


# --- CLI handlers (rig registry validate|index) ------------------------------------------------

def _report(issues: list[Issue]) -> int:
    for issue in issues:
        eprint(f"  [{'!' if issue.level == 'warning' else '✗'}] {issue.where}: {issue.message}")
    return 1 if any(i.level == "error" for i in issues) else 0


def cli_validate(root: Path) -> int:
    reg, issues = validate_registry(root)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        eprint(f"rig registry validate: {len(errors)} issue(s) in {reg.root}")
        return _report(issues)
    if issues:  # warnings only — surface them, still green
        _report(issues)
    eprint(f"rig registry validate: OK — namespace '{reg.namespace}', "
           f"{len(reg.packages)} package(s), index fresh"
           + (f", {len(issues)} warning(s)" if issues else ""))
    return 0


def cli_index(root: Path) -> int:
    """Regenerate index.json — but never index a registry whose manifests don't validate (a broken
    index would launder the breakage into consumers' resolution)."""
    reg, issues = validate_registry(root, check_index=False)
    if any(i.level == "error" for i in issues):
        eprint(f"rig registry index: refusing to index an invalid registry:")
        return _report(issues)
    out = write_index(reg)
    eprint(f"rig registry index: wrote {out} ({len(reg.packages)} package(s))")
    return 0
