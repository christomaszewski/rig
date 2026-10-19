"""Real archive metadata and executable guards across Docker image stores."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError
from rig_cli.bake import _write_bundle_guard, _write_scripts
from rig_cli.image_archive import archive_identities
from image_archive_fixture import CONFIG_ID, MANIFEST_ID, digest, encoded, write_archive

REF = 'rig-bundle/test:release'


class ImageArchive(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.archive = self.root / 'images.tar'

    def test_config_and_manifest_store_ids_bind_to_the_same_archive(self):
        write_archive(self.archive, [REF])
        for image_id in (CONFIG_ID, MANIFEST_ID):
            identities = archive_identities(self.archive, {REF: image_id}, 'arm64')
            self.assertEqual(identities['config_ids'], {REF: CONFIG_ID})
            self.assertEqual(set(identities['accepted_ids'][REF]), {CONFIG_ID, MANIFEST_ID})

    def test_extra_image_wrong_cpu_and_unrelated_identity_are_rejected(self):
        for refs, arch, image_id in (([REF, 'extra:tag'], 'arm64', CONFIG_ID),
                                    ([REF], 'amd64', CONFIG_ID),
                                    ([REF], 'arm64', 'sha256:' + 'f' * 64)):
            with self.subTest(refs=refs, arch=arch, image_id=image_id):
                write_archive(self.archive, refs, arch=arch)
                with self.assertRaises(RigError):
                    archive_identities(self.archive, {REF: image_id}, 'arm64')

    def test_index_identity_requires_the_selected_platform(self):
        for architecture in ('arm64', 'amd64'):
            index_ids = []
            def mutate(blobs):
                index = json.loads(blobs['index.json'])
                descriptor = index['manifests'][0]
                child = dict(descriptor, platform={'os': 'linux', 'architecture': architecture})
                data = encoded({'schemaVersion': 2, 'manifests': [child]})
                index_id = digest(data)
                index_ids.append(index_id)
                blobs['blobs/sha256/' + index_id[7:]] = data
                descriptor.update(digest=index_id, size=len(data),
                                  mediaType='application/vnd.oci.image.index.v1+json')
                blobs['index.json'] = encoded(index)
            write_archive(self.archive, [REF], mutate=mutate)
            if architecture == 'arm64':
                result = archive_identities(self.archive, {REF: index_ids[0]}, 'arm64')
                self.assertEqual(set(result['accepted_ids'][REF]), {CONFIG_ID, MANIFEST_ID, index_ids[0]})
            else:
                with self.assertRaisesRegex(RigError, 'descriptor is not linux/arm64'):
                    archive_identities(self.archive, {REF: index_ids[0]}, 'arm64')

    def test_oci_manifest_cannot_refer_to_a_different_config(self):
        def mutate(blobs):
            # A validly hashed second manifest must not grant identity to another config.
            import hashlib
            value = json.loads(blobs['blobs/sha256/' + MANIFEST_ID[7:]])
            value['config']['digest'] = 'sha256:' + 'f' * 64
            data = encoded(value)
            other = hashlib.sha256(data).hexdigest()
            blobs['blobs/sha256/' + other] = data
            index = json.loads(blobs['index.json'])
            index['manifests'][0].update(digest='sha256:' + other, size=len(data))
            blobs['index.json'] = encoded(index)
        write_archive(self.archive, [REF], mutate=mutate)
        with self.assertRaisesRegex(RigError, 'does not match the saved config'):
            archive_identities(self.archive, {REF: CONFIG_ID}, 'arm64')

    def test_corrupt_oci_metadata_and_missing_index_are_rejected(self):
        for mutate in (lambda blobs: blobs.update({'blobs/sha256/' + MANIFEST_ID[7:]: b'bad'}),
                       lambda blobs: blobs.pop('index.json')):
            write_archive(self.archive, [REF], mutate=mutate)
            with self.assertRaises(RigError):
                archive_identities(self.archive, {REF: CONFIG_ID}, 'arm64')

    def test_generated_guards_accept_each_store_and_refuse_wrong_content(self):
        write_archive(self.archive, [REF])
        identities = archive_identities(self.archive, {REF: MANIFEST_ID}, 'arm64')
        bundle = {'image_ids': {REF: MANIFEST_ID}, **identities}
        _write_bundle_guard(self.root, bundle)
        _write_scripts(self.root, [{'sensor': 'cam', 'project': 'cam', 'compose': 'compose.yaml',
                                   'external_volumes': []}], bundle_ids=bundle['image_ids'],
                       bundle_accepted_ids=bundle['accepted_ids'])
        binary = self.root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text('''#!/bin/sh
echo "$*" >> "$STATE/calls"
case "$1" in
 image) cat "$STATE/id" ;;
 load) printf '%s' "$LOADED_ID" > "$STATE/id" ;;
 compose) exit 0 ;;
 *) exit 9 ;;
esac
''')
        docker.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'], STATE=str(self.root))
        wrong = 'sha256:' + 'f' * 64
        for script in ('ensure-images.sh', 'up.sh', 'pull.sh', 'load.sh'):
            for initial, loaded, success in ((CONFIG_ID, CONFIG_ID, True),
                                             (MANIFEST_ID, MANIFEST_ID, True),
                                             (wrong, CONFIG_ID, True), (wrong, MANIFEST_ID, True),
                                             (wrong, wrong, False)):
                with self.subTest(script=script, initial=initial, loaded=loaded):
                    (self.root / 'id').write_text(initial)
                    (self.root / 'calls').write_text('')
                    result = subprocess.run([str(self.root / script)], env=dict(env, LOADED_ID=loaded),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    calls = (self.root / 'calls').read_text()
                    should_load = script == 'load.sh' or initial == wrong
                    self.assertEqual('load -i' in calls, should_load)
                    self.assertEqual('compose ' in calls, success and script == 'up.sh')


if __name__ == '__main__':
    unittest.main()
