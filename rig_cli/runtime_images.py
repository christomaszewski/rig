"""Replay runtime images: launcher inventory, local validation, and explicit acquisition.

The inventory is the launcher's active image set, never its build outputs. Querying and
inspection are read-only; acquisition is a separate operation used only by execution.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

from . import RigError, dispatch
from .artifact_images import run_image_query, source_for
from .common import eprint
from .image_catalog import canonical_ref
from .targets import Target

QUERY_TIMEOUT = 15
INSPECT_TIMEOUT = 15
PULL_TIMEOUT = 600
_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
_PIN_ENV = ("RIG_RUNTIME_IMAGE_MAP", "RIG_RUNTIME_IMAGE_PINS")


def _reference(value):
    return isinstance(value, str) and bool(_REF.fullmatch(value)) and (
        "@" not in value or bool(_ID.fullmatch(value.rsplit("@", 1)[-1])))


def parse_inventory(output, *, instance):
    """Strict versioned launcher response; role references must belong to the active set."""
    try:
        document = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise RigError(f"{instance}: invalid runtime-images JSON: {exc}") from exc
    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise RigError(f"{instance}: runtime-images must return version: 1")
    images = document.get("images")
    if (not isinstance(images, list) or not images or any(not _reference(ref) for ref in images)
            or len(set(images)) != len(images)):
        raise RigError(f"{instance}: runtime-images must return a nonempty list of unique resolved image refs")
    roles = document.get("roles", {})
    if (not isinstance(roles, dict) or any(not isinstance(role, str) or not role
            or not isinstance(ref, str) or ref not in images for role, ref in roles.items())):
        raise RigError(f"{instance}: runtime-images roles must map logical image names to active image refs")
    return dict(images=images, roles=roles)


def _query(row, desc, config, env):
    if "runtime-images" not in desc.verbs:
        raise RigError(f"{row.name} [{row.service}]: upgrade the service launcher to declare "
                       "verbs.runtime-images and support runtime image pinning before replay")
    if not isinstance(config, dict):
        raise RigError(f"{row.name}: runtime image planning needs its resolved configuration")
    # The query must see the same instance environment as up. An earlier invocation's
    # pin map must not alter what the launcher reports as its original image references.
    query_env = dispatch.instance_env(env, row, desc)
    for key in _PIN_ENV:
        query_env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="rig-runtime-images-") as folder:
        path = Path(folder) / "config.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        query_row = replace(row, config=path)
        output = run_image_query(query_row, desc, query_env, verb="runtime-images", timeout=QUERY_TIMEOUT)
    return parse_inventory(output, instance=row.name)


def _bundle_source(bundle, ref, architecture, runtime):
    target = bundle.get("target", {})
    if not isinstance(target, dict):
        raise RigError("Replay bundle has invalid target metadata")
    if target.get("arch") and target["arch"] != architecture:
        raise RigError(f"Replay bundle requires {target['arch']}; Docker engine is {architecture}")
    if target.get("runtime") and target["runtime"] != runtime:
        raise RigError(f"Replay bundle requires runtime {target['runtime']}; selected runtime is {runtime}")
    refs, identities = bundle.get("refs"), bundle.get("image_ids")
    if not isinstance(refs, dict) or not isinstance(identities, dict):
        raise RigError("Replay bundle lacks image reference/identity metadata; rebuild the bundle")
    alias = refs.get(ref) or refs.get(canonical_ref(ref))
    if alias is None and ref in identities:
        alias = ref
    if not _reference(alias) or alias not in identities:
        raise RigError(f"Replay requires unbundled image {ref}; build a bundle containing this companion")
    expected = identities[alias]
    accepted = bundle.get("accepted_ids", {})
    if not isinstance(accepted, dict):
        raise RigError("Replay bundle has invalid accepted image identities")
    ids = accepted.get(alias, [expected])
    if (not isinstance(ids, list) or not ids or expected not in ids
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in ids)):
        raise RigError(f"Replay bundle has invalid recorded identities for {alias}")
    return alias, sorted(set(ids))


def inventory(manifest, descriptors, rows, configs, *, architecture, env=None,
              required_labels=None, bundle=None):
    """Resolve the exact selected rows, including disabled rows selected for replay.

    required_labels maps instance -> logical role -> required labels. Roles describe
    alternative runtime variants: requirements apply to every matching active role;
    no match is an error rather than silently bypassing replay-reader validation.
    bundle is the deployment metadata.yaml's bundle block, when present.
    """
    if architecture not in ("arm64", "amd64"):
        raise RigError("Replay image planning requires the Linux Docker engine's arm64 or amd64 architecture")
    if bundle is not None and not isinstance(bundle, dict):
        raise RigError("Replay bundle metadata must be a mapping")
    bundled = bundle is not None
    env = dict(dispatch.fleet_env(manifest, descriptors) if env is None else env)
    for key in _PIN_ENV:
        env.pop(key, None)
    target = Target(manifest.platform, architecture, "replay Docker engine")
    result = dict(version=1, architecture=architecture, bundled=bundled, instances={}, images={})
    for row in rows:
        if row.name in result["instances"]:
            raise RigError(f"Replay image inventory repeats instance {row.name}")
        desc = descriptors.get(row.service)
        if desc is None:
            raise RigError(f"{row.name}: service descriptor unavailable for runtime image planning")
        selected = _query(row, desc, configs.get(row.name), env)
        result["instances"][row.name] = selected
        for ref in selected["images"]:
            if ref not in result["images"]:
                if bundled:
                    source, expected = _bundle_source(bundle, ref, architecture, manifest.platform)
                else:
                    if ref.startswith("rig-bundle/"):
                        raise RigError(f"Replay content alias {ref} requires its recorded bundle metadata")
                    try:
                        source, companions = source_for(manifest, ref, target)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RigError(f"Replay companion metadata for {ref} is invalid: {exc}") from exc
                    if manifest.build_catalog and not companions:
                        raise RigError(f"Replay image {ref} has no matching companion catalog entry; "
                                       "remove the override or build the matching release")
                    expected = [ref] if _ID.fullmatch(ref) else []
                result["images"][ref] = dict(source=source, owners=[], expected_ids=expected,
                                             required_labels={})
            result["images"][ref]["owners"].append(row.name)
        requirements = (required_labels or {}).get(row.name, {})
        if requirements and not set(requirements) & set(selected["roles"]):
            raise RigError(f"{row.name}: runtime-images must identify an active replay reader role "
                           f"({', '.join(sorted(requirements))}); upgrade the service launcher")
        for role, labels in requirements.items():
            if role not in selected["roles"]:
                continue
            if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                   for k, v in labels.items()):
                raise RigError(f"{row.name}: invalid required image labels for role {role}")
            item = result["images"][selected["roles"][role]]
            for label, value in labels.items():
                previous = item["required_labels"].get(label)
                if previous is not None and previous != value:
                    raise RigError(f"Replay readers disagree on required image label {label}")
                item["required_labels"][label] = value
    return result


def _inspect(source):
    try:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", source],
                                capture_output=True, text=True, timeout=INSPECT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(status="unavailable", message=f"cannot inspect {source}: {exc}")
    if result.returncode:
        message = (result.stderr or "").strip()
        if re.search(r"no such (?:image|object)", message, re.IGNORECASE):
            return dict(status="missing", message=f"image is unavailable locally: {source}")
        return dict(status="unavailable", message=f"cannot inspect {source}: "
                    f"{message[:500] or 'Docker returned exit ' + str(result.returncode)}")
    try:
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            raise ValueError("expected one image object")
        image_id, labels = info.get("Id"), (info.get("Config") or {}).get("Labels") or {}
        if not isinstance(image_id, str) or not _ID.fullmatch(image_id):
            raise ValueError("missing or invalid image ID")
        if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in labels.items()):
            raise ValueError("invalid image labels")
        if not isinstance(info.get("Os"), str) or not isinstance(info.get("Architecture"), str):
            raise ValueError("missing image platform")
        return dict(status="present", image_id=image_id, labels=labels,
                    os=info["Os"], architecture=info["Architecture"])
    except (AttributeError, TypeError, ValueError) as exc:
        return dict(status="unavailable", message=f"invalid Docker image inspection for {source}: {exc}")


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_pulls(inventory, inspection, policy="missing", *, offline=False):
    """Proposed network mutations; callers may display these without executing them."""
    if policy not in ("missing", "always", "never"):
        raise RigError("Replay image pull policy must be missing, always, or never")
    if policy == "always" and (offline or inventory["bundled"]):
        raise RigError("--pull always conflicts with offline/bundled replay; use --pull never")
    if policy == "never" or offline or inventory["bundled"]:
        return []
    return sorted({item["source"] for ref, item in inventory["images"].items()
                   if not _ID.fullmatch(item["source"]) and (
                       policy == "always" or inspection["images"][ref]["status"] == "missing")})


def _report(inventory, observed, *, policy, offline):
    report = dict(ready=False, missing=[], errors=[], validation_errors={}, images={},
                  proposed_pulls=[], pins={}, observed=observed,
                  inventory_fingerprint=_fingerprint(inventory))
    for ref, item in inventory["images"].items():
        value = observed[item["source"]]
        status, errors = value["status"], []
        if status == "present":
            if value["os"] != "linux" or value["architecture"] != inventory["architecture"]:
                status = "missing"  # A foreign CPU cache entry is not the required native member.
            else:
                if item["expected_ids"] and value["image_id"] not in item["expected_ids"]:
                    errors.append("local image identity differs from the recorded image")
                errors.extend(f"requires image label {key}={wanted}" for key, wanted in
                              item["required_labels"].items() if value["labels"].get(key) != wanted)
                status = "invalid" if errors else "ready"
        elif status == "unavailable":
            errors.append(value["message"])
        if status == "missing":
            report["missing"].append(ref)
            if inventory["bundled"]:
                errors.append("bundled native image is missing; load the verified bundle archive "
                              "or migrate to a bundle containing this replay companion")
            elif offline:
                errors.append("native image is missing in offline mode; provision it before replay")
            elif _ID.fullmatch(item["source"]):
                errors.append("local image ID is missing; load the required image before replay")
            elif policy == "never":
                errors.append(f"native linux/{inventory['architecture']} image is missing with --pull never; "
                              "pull it explicitly or allow --pull missing")
        report["images"][ref] = dict(source=item["source"], status=status,
                                      image_id=value.get("image_id"), labels=value.get("labels", {}))
        if errors:
            report["validation_errors"][ref] = errors
    report["proposed_pulls"] = plan_pulls(inventory, report, policy, offline=offline)
    for ref, errors in report["validation_errors"].items():
        # An explicit refresh may repair cached labels/identity, but an inaccessible
        # Docker daemon cannot be treated as cache absence and does not authorize pulls.
        if (policy == "always" and report["images"][ref]["status"] == "invalid"
                and inventory["images"][ref]["source"] in report["proposed_pulls"]):
            continue
        owners = ", ".join(inventory["images"][ref]["owners"])
        report["errors"].extend(f"{owners}: {ref}: {message}" for message in errors)
    report["ready"] = not report["proposed_pulls"] and all(
        value["status"] == "ready" for value in report["images"].values())
    report["pins"] = {name: dict(version=1, images={ref: report["images"][ref]["image_id"]
                     for ref in item["images"]}) for name, item in inventory["instances"].items()
                     if all(report["images"][ref]["status"] == "ready" and
                            inventory["images"][ref]["source"] not in report["proposed_pulls"]
                            for ref in item["images"])}
    return report


def inspect_images(inventory, *, policy="missing", offline=False):
    """Inspect each distinct local source once; never contact a registry or change Docker."""
    observed = {source: _inspect(source) for source in
                sorted({item["source"] for item in inventory["images"].values()})}
    return _report(inventory, observed, policy=policy, offline=offline)


def acquire_images(inventory, *, policy="missing", inspection=None, offline=False):
    """Pull the proposed native refs, then validate identities/labels before issuing pins."""
    if inspection is not None and inspection.get("inventory_fingerprint") != _fingerprint(inventory):
        raise RigError("Runtime image inventory changed since inspection; inspect again before pulling")
    report = (_report(inventory, inspection["observed"], policy=policy, offline=offline)
              if inspection is not None else inspect_images(inventory, policy=policy, offline=offline))
    if report["errors"]:
        return report
    pulled, observed = [], dict(report["observed"])
    for source in report["proposed_pulls"]:
        eprint(f"rig replay: pulling linux/{inventory['architecture']} {source}")
        try:
            result = subprocess.run(["docker", "pull", "--platform", f"linux/{inventory['architecture']}", source],
                                    stdout=sys.stderr, stderr=sys.stderr, text=True, timeout=PULL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RigError(f"Replay could not pull {source}: {exc}") from exc
        if result.returncode:
            raise RigError(f"Replay could not pull {source} (exit {result.returncode}); "
                           "check registry access and credentials")
        pulled.append(source)
        observed[source] = _inspect(source)
    result = _report(inventory, observed, policy="never", offline=offline)
    result["pulled"] = pulled
    return result
