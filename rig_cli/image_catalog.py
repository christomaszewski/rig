"""Build image identities carried into artifacts and localized replay workspaces."""
from __future__ import annotations
import os
import platform as host_platform
import subprocess
from . import RigError
from .build_contract import variant_spec
from .common import eprint, load_yaml


def canonical_ref(ref):
    if ref and '@' not in ref and ':' not in ref.rsplit('/', 1)[-1]:
        return ref + ':latest'
    return ref


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
    raise RigError('cannot determine replay Docker engine architecture; start/connect the Linux Docker engine')


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
    lock = load_lock(tree)
    lock['replay_execution'] = dict(source_platform=recorded, platform=selected,
                                    architecture=docker_arch(fallback=True))
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
