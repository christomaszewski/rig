"""``rig registry init <dir>`` — scaffold a new, empty package registry.

The result is immediately usable as a *local-dir* registry (no git, no remote, no CI): point
``rig registry add <name> --path <dir>`` at it and promote/install against it. When it's time
to share, ``git init`` + push it and consumers add it by URL — the scaffolded CI wrappers
(GitHub Actions AND GitLab, ~10 lines each) come alive then, both calling the same neutral
``tools/validate`` entrypoint that `rig pkg promote` and humans use. All validation logic lives
in rig itself (``rig_cli.registry``) — the scaffold ships shims and documentation, never a
vendored validator that could drift. The ``schemas/`` JSON Schemas are informational mirrors of
rig's rules for external tooling and reviewers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import RigError
from .common import eprint
from .registry import KIND_DIRS, SCHEMA, load_registry, write_index

_NAME_RE = "^[a-z][a-z0-9-]*$"
_VERSION_RE = r"^\d+\.\d+\.\d+$"

_TOOL_VALIDATE = """\
#!/bin/sh
# Neutral validation entrypoint — the CI wrappers, `rig pkg promote`, and humans all call THIS.
# All rules live in rig itself (one source of truth); install it once:
#   pipx install git+https://github.com/christomaszewski/rig
set -e
cd "$(dirname "$0")/.."
if command -v rig >/dev/null 2>&1; then exec rig registry validate .; fi
exec python3 -m rig_cli registry validate .
"""

_TOOL_GEN_INDEX = """\
#!/bin/sh
# Regenerate index.json (validates first — an invalid registry is never indexed).
set -e
cd "$(dirname "$0")/.."
if command -v rig >/dev/null 2>&1; then exec rig registry index .; fi
exec python3 -m rig_cli registry index .
"""

_GHA_CI = """\
# Thin wrapper — ALL validation logic lives in rig (tools/validate -> `rig registry validate`),
# so this file stays tiny and can never drift from the client's rules.
name: registry-ci
on:
  push: {branches: [main]}
  pull_request:
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install git+https://github.com/christomaszewski/rig
      - run: ./tools/validate
"""

_GITLAB_CI = """\
# Thin wrapper — same neutral entrypoint as the GitHub workflow (tools/validate).
validate:
  image: python:3.12
  script:
    - pip install git+https://github.com/christomaszewski/rig
    - ./tools/validate
"""

_README = """\
# {name} — a rig package registry (namespace `{namespace}`)

A registry is a git repo (or a plain shared folder) of **manifests**; rig resolves and installs
from it. Four package kinds:

| kind | dir | carries |
|---|---|---|
| `service` | `services/<name>/` | pointer to a code repo pinned by FULL commit SHA and/or an image pinned by digest |
| `profile` | `profiles/<name>/` | device/sensor profile: hardware `match` identifiers, a required service, a **nameless** config payload |
| `overlay` | `overlays/<name>/` | versioned, project-tagged config **delta** targeting a service or instance |
| `suite`   | `suites/<name>/`   | ordered *references* to the above (fully-qualified exact pins) — never payloads |

Kind decision rule: different hardware or new match identifiers → new **profile**; project
tuning of an existing config → **overlay**.

## Consume

```
rig registry add {namespace} <url-of-this-repo>     # git-hosted (managed clone + `rig registry sync`)
rig registry add {namespace} --path <this-dir>      # or use this folder in place (local-dir)
```

## Author

Add or edit a package (usually via `rig pkg promote` from a deployment, or by hand), then:

```
./tools/validate      # every rule CI enforces, locally
./tools/gen-index     # regenerate index.json (GENERATED — never hand-edit)
```

Commit both the package and the regenerated `index.json`. CI (GitHub Actions and GitLab
wrappers are both included) re-runs the same `tools/validate`.

`schemas/` holds informational JSON Schemas mirroring rig's validation rules for external
tooling; the authoritative validator is rig itself (`rig registry validate`).
"""

_CONTRIBUTING = """\
# Contributing packages

Ground rules `tools/validate` (and CI) enforce:

- **Names** are `[a-z][a-z0-9-]*` and unique across ALL kinds — the index keys by bare name.
  (Package names are registry identifiers; deployment *instance* names remain underscore-only.)
- **Versions** are exact `X.Y.Z`. The registry's HEAD carries the current version of each
  package; history lives in git, and consumers pin the registry commit in their lockfile.
- **Services**: `source.rev` is a FULL commit SHA (no branches, no short SHAs); `image.ref`
  is digest-pinned (`…@sha256:<64 hex>`) — tag refs are rejected. `source.path` points at the
  service's dir inside a collection repo (e.g. one repo carrying several services).
- **Profiles**: `requires.service` must resolve (in-registry, or fully qualified for another
  registry) and be satisfied by that service's version. The config payload is a **nameless**
  config — the deployment's instance row supplies `name`.
- **Overlays**: the payload is a **delta only** (never a full config copy; `null` deletes a
  key). It must not set `name`/`service` — identity belongs to the instance row.
- **Suites**: references only — fully-qualified exact pins (`ns/name@X.Y.Z`), no `config/`
  dir, no nested suites.
- **index.json is GENERATED** (`tools/gen-index`). A stale or hand-edited index fails CI.

Flow: branch → add/edit the package → `./tools/validate` → `./tools/gen-index` → commit
(package + index) → PR. The easiest correct path from a working deployment is
`rig pkg promote … --to <this registry>`, which scaffolds all of the above.
"""


def _schemas() -> dict[str, dict]:
    """Informational JSON Schemas mirroring rig_cli.registry's rules (kept adjacent to the scaffold
    so a rules change updates both in one review)."""
    name = {"type": "string", "pattern": _NAME_RE}
    version = {"type": "string", "pattern": _VERSION_RE}
    relpath = {"type": "string"}
    common = {"kind": {"type": "string"}, "name": name, "version": version}
    return {
        "registry": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "registry.yaml", "type": "object",
            "required": ["schema", "name", "namespace"],
            "properties": {"schema": {"type": "integer"}, "name": {"type": "string"},
                           "namespace": name, "description": {"type": "string"}},
        },
        "service": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "service manifest", "type": "object",
            "required": ["kind", "name", "version"],
            "properties": {**common, "kind": {"const": "service"},
                           "platforms": {"type": "array", "items": {"type": "string"}},
                           "ros_distro": {"type": "string"}, "instantiable": {"type": "boolean"},
                           "source": {"type": "object", "required": ["repo", "rev"],
                                      "properties": {"repo": {"type": "string"},
                                                     "rev": {"type": "string",
                                                             "pattern": "^[0-9a-f]{40}$|^[0-9a-f]{64}$"},
                                                     "path": relpath}},
                           "image": {"type": "object", "required": ["ref"],
                                     "properties": {"ref": {"type": "string",
                                                            "pattern": "@sha256:[0-9a-f]{64}$"}}},
                           "config": {"type": "object", "properties": {"schema": relpath}}},
        },
        "profile": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "profile manifest", "type": "object",
            "required": ["kind", "name", "version", "requires", "config"],
            "properties": {**common, "kind": {"const": "profile"},
                           "provides": {"type": "object", "properties": {
                               "sensor": {"type": "array", "items": {
                                   "type": "object", "required": ["match"],
                                   "properties": {"model": {"type": "string"},
                                                  "match": {"type": "array", "minItems": 1,
                                                            "items": {"type": "string"}}}}}}},
                           "requires": {"type": "object", "required": ["service"],
                                        "properties": {"service": {"type": "string"}}},
                           "config": {"type": "object", "required": ["payload"],
                                      "properties": {"payload": relpath,
                                                     "overrides_schema": relpath}}},
        },
        "overlay": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "overlay manifest", "type": "object",
            "required": ["kind", "name", "version", "targets", "config"],
            "properties": {**common, "kind": {"const": "overlay"},
                           "targets": {"type": "array", "minItems": 1, "items": {
                               "type": "object", "minProperties": 1, "maxProperties": 1,
                               "properties": {"service": {"type": "string"},
                                              "instance": {"type": "string",
                                                           "pattern": "^[a-z][a-z0-9_]*$"}}}},
                           "project": {"type": "string"},
                           "authored_against": {"type": "object", "properties": {
                               "service": {"type": "string"},
                               "profile": {"type": "string"}}},
                           "config": {"type": "object", "required": ["payload"],
                                      "properties": {"payload": relpath}}},
        },
        "suite": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "suite manifest", "type": "object",
            "required": ["kind", "name", "version", "members"],
            "properties": {**common, "kind": {"const": "suite"}, "project": {"type": "string"},
                           "members": {"type": "object", "additionalProperties": False,
                                       "properties": {k: {"type": "array", "items": {
                                           "type": "string",
                                           "pattern": "^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*@"
                                                      r"\d+\.\d+\.\d+$"}}
                                           for k in ("services", "profiles", "overlays")}}},
        },
        "index": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "index.json (GENERATED)", "type": "object",
            "required": ["schema", "namespace", "packages", "sensors", "projects"],
            "properties": {"schema": {"type": "integer"}, "namespace": name,
                           "packages": {"type": "object"}, "sensors": {"type": "object"},
                           "projects": {"type": "object"}},
        },
    }


def registry_init(target: Path, namespace: str | None = None) -> int:
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise RigError(f"registry init: {target} exists and is not empty")
    ns = namespace or re.sub(r"[^a-z0-9-]+", "-", target.name.lower()).strip("-")
    if not re.match(_NAME_RE, ns):
        raise RigError(f"registry init: cannot derive a namespace from '{target.name}' — "
                       f"pass --namespace ([a-z][a-z0-9-]*)")

    (target / "tools").mkdir(parents=True, exist_ok=True)
    (target / "schemas").mkdir()
    (target / ".github" / "workflows").mkdir(parents=True)
    for plural in KIND_DIRS.values():
        (target / plural).mkdir()
        (target / plural / ".gitkeep").touch()

    (target / "registry.yaml").write_text(
        f"# Registry metadata. `schema` gates client compatibility (older rigs refuse newer\n"
        f"# schemas with an upgrade pointer); `namespace` is the prefix consumers see\n"
        f"# (`{ns}/<package>`).\n"
        f"schema: {SCHEMA}\nname: {target.name}\nnamespace: {ns}\ndescription: \"\"\n")
    (target / "tools" / "validate").write_text(_TOOL_VALIDATE)
    (target / "tools" / "validate").chmod(0o755)
    (target / "tools" / "gen-index").write_text(_TOOL_GEN_INDEX)
    (target / "tools" / "gen-index").chmod(0o755)
    (target / ".github" / "workflows" / "ci.yaml").write_text(_GHA_CI)
    (target / ".gitlab-ci.yml").write_text(_GITLAB_CI)
    (target / "README.md").write_text(_README.format(name=target.name, namespace=ns))
    (target / "CONTRIBUTING.md").write_text(_CONTRIBUTING)
    for stem, schema in _schemas().items():
        (target / "schemas" / f"{stem}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    write_index(load_registry(target, issues := []))
    if issues:  # a fresh scaffold must be self-valid, full stop
        raise RigError(f"registry init: scaffold failed self-validation: {issues[0].message}")

    eprint(f"initialized registry '{target.name}' (namespace '{ns}') at {target}")
    eprint(f"  use locally now:   rig registry add {ns} --path {target}")
    eprint(f"  share it later:    git init && git add -A && git commit, push, then consumers:")
    eprint(f"                     rig registry add {ns} <url>   (CI wrappers for GitHub+GitLab included)")
    eprint(f"  add packages:      rig pkg promote … --to {ns}   (or by hand; then ./tools/validate)")
    return 0
