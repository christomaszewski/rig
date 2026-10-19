"""Verify saved image membership and identities for both Docker image stores."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tarfile

from . import RigError
from .image_catalog import canonical_ref


def _archive_ref(ref):
    if not isinstance(ref, str):
        raise ValueError('invalid saved image name')
    for prefix in ('docker.io/', 'index.docker.io/'):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if ref.startswith('library/'):
        ref = ref[len('library/'):]
    return canonical_ref(ref)


def archive_identities(path: Path, image_ids: dict[str, str], arch: str | None = None) -> dict:
    """Bind every alias to its config and exported OCI manifest/index digests.

    Classic stores inspect images by config digest; containerd stores inspect the
    manifest/index instead. Read the exported OCI graph because classic Docker
    may generate a new manifest during save, distinct from the registry digest.
    """
    expected = {_archive_ref(ref): ref for ref in image_ids}
    configs, accepted = {}, {}
    try:
        with tarfile.open(path, 'r:') as saved:
            def read(name):
                member = saved.getmember(name)
                if member.size > 8 * 1024 * 1024:
                    raise ValueError(f'image metadata is too large: {name}')
                stream = saved.extractfile(member)
                if stream is None:
                    raise ValueError(f'missing image metadata: {name}')
                return stream.read()

            def blob(descriptor):
                digest = descriptor['digest']
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                    raise ValueError(f'invalid image digest: {digest}')
                data = read('blobs/sha256/' + digest[7:])
                if (len(data) != descriptor['size']
                        or 'sha256:' + hashlib.sha256(data).hexdigest() != digest):
                    raise ValueError(f'image metadata digest/size mismatch: {digest}')
                return json.loads(data)

            for entry in json.loads(read('manifest.json')):
                data = read(entry['Config'])
                config = json.loads(data)
                if arch and (config.get('os'), config.get('architecture')) != ('linux', arch):
                    raise ValueError(f'image archive contains a non-linux/{arch} image')
                digest = 'sha256:' + hashlib.sha256(data).hexdigest()
                for name in entry['Layers']:
                    saved.getmember(name)  # refuse an incomplete export before publishing
                for tag in entry['RepoTags'] or []:
                    ref = expected.get(_archive_ref(tag))
                    if ref is None or ref in configs:
                        raise ValueError(f'unexpected or duplicate image alias: {tag}')
                    configs[ref] = digest
                    accepted[ref] = {digest}
            if configs.keys() != image_ids.keys():
                raise ValueError('saved image aliases do not match the selected images')

            def visit(descriptor, config_id, ancestors=frozenset()):
                digest = descriptor['digest']
                if digest in ancestors:
                    raise ValueError('cyclic OCI image index')
                platform = descriptor.get('platform')
                if arch and platform and (platform.get('os'), platform.get('architecture')) != ('linux', arch):
                    raise ValueError(f'OCI image descriptor is not linux/{arch}')
                value = blob(descriptor)
                ids = {digest}
                if 'manifests' in value:
                    if not value['manifests']:
                        raise ValueError('empty OCI image index')
                    for child in value['manifests']:
                        ids.update(visit(child, config_id, ancestors | {digest}))
                else:
                    if value['config']['digest'] != config_id:
                        raise ValueError('OCI image does not match the saved config')
                    blob(value['config'])
                    for layer in value['layers']:
                        algorithm, hex_digest = layer['digest'].split(':', 1)
                        member = saved.getmember(f'blobs/{algorithm}/{hex_digest}')
                        if member.size != layer['size']:
                            raise ValueError('image layer size mismatch')
                return ids

            # Docker's supported save API emits both Docker and OCI metadata.
            seen = set()
            for descriptor in json.loads(read('index.json'))['manifests']:
                name = descriptor.get('annotations', {}).get('io.containerd.image.name', '')
                ref = expected.get(_archive_ref(name))
                if ref is None or ref in seen:
                    raise ValueError(f'unexpected or duplicate OCI image alias: {name}')
                seen.add(ref)
                accepted[ref].update(visit(descriptor, configs[ref]))
            if seen != set(image_ids):
                raise ValueError('OCI image aliases do not match the selected images')
            for ref, image_id in image_ids.items():
                if image_id not in accepted[ref]:
                    raise ValueError(f'saved image identity differs from Docker inspection: {ref}')
    except (OSError, tarfile.TarError, KeyError, TypeError, ValueError) as exc:
        raise RigError(f'bundle: invalid image archive: {exc}') from exc
    return {'config_ids': configs, 'accepted_ids': {ref: sorted(ids) for ref, ids in accepted.items()}}
