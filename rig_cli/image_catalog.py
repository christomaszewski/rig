"""Build image identities carried into artifacts and localized replay workspaces."""
from __future__ import annotations
import os
import platform as host_platform
import re
import subprocess
from . import RigError
from .build_contract import variant_spec
from .common import eprint, load_yaml


def canonical_ref(ref):
    if ref and '@' not in ref and ':' not in ref.rsplit('/', 1)[-1]:
        return ref + ':latest'
    return ref


def artifact_catalog(catalog, registry):
    """Keep image identities while removing build-machine registry addresses from a bake."""
    result, refs = {}, {}
    prefix = f'{registry.rstrip("/")}/' if registry else ''
    for original, row in catalog.items():
        image = row.get('image')
        if not image:
            raise RigError(f'bake: build catalog entry {original} lacks its logical image name; rebuild it')
        ref = f'{prefix}{image}:{original.rsplit(":", 1)[-1]}'
        if ref in result and result[ref].get('digest') != row.get('digest'):
            raise RigError(f'conflicting companion digests for {ref}; rebuild the matching release')
        result[ref] = dict(row, ref=ref)
        refs[original] = ref
    return result, refs


def artifact_lock(locked, registry):
    catalog, refs = artifact_catalog(locked.get('build_catalog', {}), registry)
    result = dict(locked, build_catalog=catalog)
    result.pop('replay_execution', None)
    if locked.get('last_build'):
        result['last_build'] = dict(locked['last_build'], outputs=[
            refs[ref] for ref in locked['last_build'].get('outputs', []) if ref in refs])
    return result


def pins(manifest):
    result = {}
    for original, row in manifest.build_catalog.items():
        image = row.get('image')
        digest = row.get('digest')
        if not image or not digest:
            continue
        tag = original.rsplit(':', 1)[-1]
        repo = f'{manifest.image_registry}/{image}' if manifest.image_registry else original.rsplit(':', 1)[0]
        ref, pinned = f'{repo}:{tag}', f'{repo}@{digest}'
        if ref in result and result[ref] != pinned:
            raise RigError(f'conflicting companion digests for {ref}; rebuild the matching release')
        result[ref] = pinned
    return result


def docker_arch(*, fallback=False):
    try:
        proc = subprocess.run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'],
                              capture_output=True, text=True, timeout=10)
        os_name, _, architecture = proc.stdout.strip().partition('/')
        if not proc.returncode and os_name == 'linux':
            architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(architecture, architecture)
            if architecture in ('arm64', 'amd64'):
                return architecture
    except (OSError, subprocess.TimeoutExpired):
        pass
    if fallback:
        return {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'AMD64': 'amd64'}.get(
            host_platform.machine())
    raise RigError('cannot determine Docker engine architecture; start/connect the Linux Docker engine')


def localize(tree, local, requested):
    from .manifest import machine_file
    from .descriptor import load_descriptor
    from .lock import load_lock, save_lock
    vehicle = load_yaml(tree / 'vehicle.yaml')
    recorded = local.get('platform') or vehicle.get('platform')
    routes = load_yaml(tree / 'services.yaml').get('services', {})
    descriptors = {}
    for name, route in routes.items():
        path = route.get('path') if isinstance(route, dict) else route
        if path:
            descriptors[name] = load_descriptor(name, (tree / path).resolve())
    machine = load_yaml(machine_file()) if machine_file().exists() else {}
    choices = {d.replay_platform for d in descriptors.values() if d.replay_platform}
    if requested == 'recorded':
        selected = recorded
    elif requested:
        selected = requested
    elif machine.get('platform'):
        selected = machine['platform']
    elif len(choices) == 1:
        selected = next(iter(choices))
    elif len(choices) > 1:
        raise RigError(f'reconstruct: services disagree on replay runtime {sorted(choices)}; pass --platform')
    else:
        selected = recorded
    for desc in descriptors.values():
        if desc.replay_start and selected and desc.build_platforms and selected not in desc.build_platforms:
            raise RigError(f'reconstruct: {desc.service} does not support replay runtime {selected!r}')
    # Empty is intentional: it prevents the destination machine overriding --platform recorded.
    local['platform'] = selected or ''
    architecture = docker_arch(fallback=True)
    source_arch = local.get('arch') or vehicle.get('arch')
    # Older captured Rig versions reject unknown local keys. Only write an arch override when
    # the source already declares one; every workspace still records CPU selection in the lock.
    if 'arch' in vehicle or 'arch' in local:
        local['arch'] = architecture or ''
    lock = load_lock(tree)
    lock['replay_execution'] = dict(source_platform=recorded, source_architecture=source_arch,
                                    platform=selected, architecture=architecture)
    save_lock(tree, lock)
    eprint(f'reconstruct: recorded runtime {recorded or "generic"}; replay runtime {selected or "generic"}')
    if not lock.get('build_catalog'):
        eprint('reconstruct: legacy capture has no companion image catalog; image tags are unverified. '
               'Build the matching source release to record replay image digests.')


def validate_replay(manifest, descriptors, *, dry_run=False, fleet_descriptors=None):
    """Validate only selected services, before starting infra. Old captures retain explicit warnings."""
    if not manifest.build_catalog:
        eprint('rig replay: no companion image catalog; replay image tags are unverified')
        return dict(manifest.replay_execution)
    from .build import _service_tag, resolve_base_image, resolve_msgs_image
    image_pins = pins(manifest)
    by_ref = {ref: row for original, row in manifest.build_catalog.items()
              for ref in pins_for_row(manifest, original, row)}
    architecture = manifest.replay_execution.get('architecture') if dry_run else docker_arch()
    fleet_descriptors = fleet_descriptors or descriptors
    replaced = ({d.build_images[0] for d in fleet_descriptors.values() if d.build_provides == 'base'}
                if manifest.image_base else set())
    refs = set()
    for desc in descriptors.values():
        _, images = variant_spec(desc, manifest.platform)
        images = [image for image in images if image not in replaced]
        tag = _service_tag(desc, manifest.image_tag, manifest.platform) or 'latest'
        prefix = f'{manifest.image_registry}/' if manifest.image_registry else ''
        refs.update(f'{prefix}{image}:{tag}' for image in images)
        for image in images:
            key = desc.image_env.get(image)
            override = manifest.extra_env.get(key, os.environ.get(key)) if key else None
            ref = f'{prefix}{image}:{tag}'
            if override and override != image_pins.get(ref):
                raise RigError(f'replay: {key} overrides the recorded companion digest for {ref}; '
                               'remove the override to replay this release')
    for resolve in (resolve_base_image, resolve_msgs_image):
        ref, _, error = resolve(manifest, fleet_descriptors)
        ref = canonical_ref(ref)
        if error:
            raise RigError(error)
        if ref and ref in image_pins:
            refs.add(ref)
    selected = {}
    for ref in sorted(refs):
        row = by_ref.get(ref)
        if row is None or ref not in image_pins:
            raise RigError(f'replay: missing companion image {ref}; rebuild the matching release '
                           f'with --platform {manifest.platform or "<runtime>"}')
        if architecture and architecture not in row.get('architectures', []):
            raise RigError(f'replay: {ref} has no {architecture} image; rebuild for this architecture')
        selected[ref] = image_pins[ref]
        if not dry_run:
            try:
                local = subprocess.run(['docker', 'image', 'inspect', image_pins[ref]],
                                       capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RigError(f'replay: cannot inspect {image_pins[ref]}: {exc}') from exc
            if local.returncode:
                from .matrix_build import inspect_raw
                if inspect_raw(image_pins[ref]) is None:
                    raise RigError(f'replay: {image_pins[ref]} is unavailable in the replay registry')
    return dict(manifest.replay_execution, platform=manifest.platform,
                architecture=architecture, images=selected)


def pins_for_row(manifest, original, row):
    image = row.get('image')
    if image:
        repo = f'{manifest.image_registry}/{image}' if manifest.image_registry else original.rsplit(':', 1)[0]
        yield repo + ':' + original.rsplit(':', 1)[-1]


def adopted_rows(catalog):
    """The build-catalog keys `rig image adopt` recorded: images rig did not build, whose rows
    carry no source revision. Audit warns about them; bake refuses them without --allow-local."""
    return sorted(key for key, row in (catalog or {}).items() if isinstance(row, dict) and row.get('adopted'))


ADOPTED_HINT = 'rebuild them with rig build before shipping, or pass --allow-local'


def adopted_note(keys):
    return f'adopted with rig image adopt, so rig has no build provenance for them: {", ".join(keys)}'


def adopt(root, manifest, ref, *, like=None, registry=None, dry_run=False):
    """`rig image adopt REF`: record an image rig did not build (a hand-built or hand-pushed tag)
    in rig.lock's build catalog, so replay's companion check accepts it. The row comes from the
    registry's own manifest — digest, architectures and per-architecture children — and never
    copies another row's provenance: it is marked ``adopted`` with no source revision. ``like``
    names a catalog row to take only its service and runtime variant from."""
    import datetime
    import json
    from . import docker_registry
    from .build import build_registry
    from .lock import load_lock, save_lock
    from .matrix_build import inspect_raw, tag_architectures
    ref = canonical_ref(ref)
    if '@' in ref:
        raise RigError(f'image adopt: {ref} is a digest; adopt the tag a deployment refers to (repo:tag)')
    repo, tag = ref.rsplit(':', 1)
    reg, source = build_registry(root, manifest, registry)
    if reg and not repo.startswith(reg + '/'):
        raise RigError(f"image adopt: {ref} is not in the deployment's registry {reg} (from {source}); "
                       f'copy it there first: docker buildx imagetools create --tag {reg}/<image>:{tag} {ref}')
    image = repo[len(reg) + 1:] if reg else repo
    raw = inspect_raw(ref)
    if raw is None:
        raise RigError(f'image adopt: {ref} is not in its registry; push it first')
    result = docker_registry.imagetools(['inspect', '--format', '{{json .Manifest}}', ref], [ref],
                                        capture_output=True, text=True, timeout=45)
    try:
        digest = json.loads(result.stdout).get('digest', '') if not result.returncode else ''
    except ValueError:
        digest = ''
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''):
        raise RigError(f'image adopt: cannot read the manifest digest of {ref}: {result.stderr.strip()}')
    if 'manifests' in raw:
        children = {m['platform']['architecture']: m['digest'] for m in raw['manifests']
                    if (m.get('platform') or {}).get('os') == 'linux' and m.get('digest')}
    else:  # a single-platform image: it is its own architecture member
        children = {arch: digest for arch in tag_architectures(ref, raw)}
    if not children:
        raise RigError(f'image adopt: {ref} names no linux architecture rig can record')
    catalog = load_lock(root).get('build_catalog') or {}
    template = None
    if like:
        template = catalog.get(canonical_ref(like))
        if template is None:
            raise RigError(f"image adopt: --like {like} is not in rig.lock's build catalog "
                           f'({", ".join(sorted(catalog)) or "it is empty"})')
    else:
        kinds = {(row.get('service'), row.get('variant')) for row in catalog.values() if row.get('image') == image}
        if len(kinds) > 1:
            raise RigError(f'image adopt: {image} has catalog rows for several services or runtimes; '
                           'pass --like <catalog key> to choose one')
        template = next((row for row in catalog.values() if row.get('image') == image), None)
    row = dict(ref=ref, image=image, digest=digest, architectures=sorted(children), children=children,
               service=(template or {}).get('service'), variant=(template or {}).get('variant'),
               source_revision=None, source_dirty=None, no_cache=None,
               adopted=dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                            like=canonical_ref(like) if like else None))
    replaced = [key for key, old in catalog.items()
                if old.get('image') == image and key.rsplit(':', 1)[-1] == tag]
    for key in replaced:
        old = catalog[key]
        what = 'adopted' if old.get('adopted') else f"built from {str(old.get('source_revision') or 'unknown')[:12]}"
        eprint(f'rig image adopt: replaces {key} ({what}, {str(old.get("digest"))[:19]}…)')
    eprint(f'rig image adopt: {ref} {digest[:19]}… ({", ".join(sorted(children))})'
           + (f' as {row["service"]}' + (f'/{row["variant"]}' if row['variant'] else '') if row['service'] else ''))
    if dry_run:
        eprint('rig image adopt: dry run — rig.lock unchanged')
        return 0
    lock = load_lock(root)
    entries = lock.setdefault('build_catalog', {})
    for key in replaced:
        entries.pop(key, None)
    entries[ref] = row
    save_lock(root, lock)
    eprint('rig image adopt: recorded as adopted — rig image audit warns about it and rig bake '
           'refuses it without --allow-local until rig build replaces it')
    return 0
