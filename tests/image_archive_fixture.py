"""Small Docker/OCI save archive with real, distinct config and manifest digests."""
import hashlib
import io
import json
from pathlib import Path
import tarfile


def encoded(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode()


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


CONFIG_BYTES = encoded({'architecture': 'arm64', 'os': 'linux', 'config': {'Cmd': ['/test']},
                        'rootfs': {'type': 'layers', 'diff_ids': []}})
CONFIG_ID = digest(CONFIG_BYTES)
MANIFEST_BYTES = encoded({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                         'config': {'mediaType': 'application/vnd.oci.image.config.v1+json',
                                    'digest': CONFIG_ID, 'size': len(CONFIG_BYTES)}, 'layers': []})
MANIFEST_ID = digest(MANIFEST_BYTES)


def write_archive(path, refs, *, arch='arm64', mutate=None):
    def qualified(ref):
        first = ref.split('/')[0]
        if '.' in first or ':' in first or first == 'localhost':
            return ref
        return 'docker.io/' + (ref if '/' in ref else 'library/' + ref)
    config = json.loads(CONFIG_BYTES)
    config['architecture'] = arch
    config_bytes = encoded(config)
    config_id = digest(config_bytes)
    manifest = json.loads(MANIFEST_BYTES)
    manifest['config'].update(digest=config_id, size=len(config_bytes))
    manifest_bytes = encoded(manifest)
    manifest_id = digest(manifest_bytes)
    blobs = {'blobs/sha256/' + config_id[7:]: config_bytes,
             'blobs/sha256/' + manifest_id[7:]: manifest_bytes,
             'manifest.json': encoded([{'Config': 'blobs/sha256/' + config_id[7:],
                                        'RepoTags': refs, 'Layers': []}]),
             'index.json': encoded({'schemaVersion': 2, 'manifests': [
                 {'digest': manifest_id, 'size': len(manifest_bytes),
                  'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                  'annotations': {'io.containerd.image.name': qualified(ref)}} for ref in refs]})}
    if mutate:
        mutate(blobs)
    with tarfile.open(path, 'w') as tar:
        for name, content in blobs.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    return config_id, manifest_id
