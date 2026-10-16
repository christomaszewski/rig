"""Companion selection, immutable pins and reconstruction runtime localization."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, image_catalog as catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env, service_env
from rig_cli.lock import load_lock, save_lock
from rig_cli.manifest import Manifest, RosSettings

DIGEST = 'sha256:' + 'a' * 64


class ImageCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'rigging.yaml').write_text('''service: camera
launcher: up
build:
  command: b.sh
  images: [core, dev, bridge]
  platforms: [jp6m, dev]
  architectures: [arm64, amd64]
  variants:
    jp6m: {architectures: [arm64], images: [core, bridge]}
    dev: {images: [dev, bridge]}
images: {override_env: {core: CORE_IMAGE, dev: DEV_IMAGE, bridge: BRIDGE_IMAGE}}
replay: {platform: dev}
''')
        self.desc = load_descriptor('camera', self.root)
        self.m = Manifest(vehicle='test', sensors=[], ros=RosSettings(0, 'rmw_zenoh_cpp', None),
                          image_registry='mirror:5000', image_tag='v1', platform='dev',
                          replay_execution={'source_platform': 'jp6m', 'architecture': 'amd64'})
        self.m.build_catalog = {f'original:5000/{image}:v1-dev':
                               {'image': image, 'digest': DIGEST, 'architectures': ['arm64', 'amd64']}
                               for image in ('dev', 'bridge')}

    def test_replay_uses_companion_and_bridge_digests_at_relocated_registry(self):
        with patch.object(catalog, 'docker_arch', return_value='amd64'), \
                patch.object(catalog.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                patch('rig_cli.matrix_build.inspect_raw', return_value={'manifests': []}) as inspect:
            result = catalog.validate_replay(self.m, {'camera': self.desc})
        self.assertEqual(result['source_platform'], 'jp6m')
        self.assertEqual(result['platform'], 'dev')
        self.assertEqual(result['images']['mirror:5000/dev:v1-dev'], f'mirror:5000/dev@{DIGEST}')
        self.assertEqual(inspect.call_count, 2)
        env = service_env(fleet_env(self.m), self.desc)
        self.assertEqual(env['DEV_IMAGE'], f'mirror:5000/dev@{DIGEST}')
        self.assertEqual(env['BRIDGE_IMAGE'], f'mirror:5000/bridge@{DIGEST}')
        self.assertNotIn('CORE_IMAGE', env)

    def test_missing_companion_architecture_or_override_fails_before_launch(self):
        with patch.dict(os.environ, DEV_IMAGE='different:tag'):
            with self.assertRaisesRegex(RigError, 'overrides the recorded companion'):
                catalog.validate_replay(self.m, {'camera': self.desc}, dry_run=True)
        self.m.build_catalog['original:5000/dev:v1-dev']['architectures'] = ['arm64']
        with self.assertRaisesRegex(RigError, 'has no amd64'):
            catalog.validate_replay(self.m, {'camera': self.desc}, dry_run=True)
        del self.m.build_catalog['original:5000/dev:v1-dev']
        with self.assertRaisesRegex(RigError, 'missing companion image mirror:5000/dev:v1-dev'):
            catalog.validate_replay(self.m, {'camera': self.desc}, dry_run=True)

    def test_bake_keeps_index_digest_and_refuses_an_incomplete_offline_bundle(self):
        from rig_cli import bake
        ref = 'registry/dev@' + DIGEST
        with patch.object(bake.subprocess, 'run') as run:
            self.assertEqual(bake._resolve_digest(ref), ref)
            run.assert_not_called()
        with self.assertRaisesRegex(RigError, 'companion indexes'):
            bake.bake(self.root, self.m, {}, {}, {}, 'test', bundle_images=True)

    def test_localize_prefers_host_then_software_mapping_and_preserves_recorded_choice(self):
        (self.root / 'vehicle.yaml').write_text('vehicle: test\nplatform: jp6m\n')
        (self.root / 'services.yaml').write_text('services: {camera: {path: .}}\n')
        machine = self.root / 'machine.yaml'
        machine.write_text('platform: dev\n')
        save_lock(self.root, {'schema': 1, 'build_catalog': self.m.build_catalog,
                              'images': {'original:5000/core:v1-jp6m': DIGEST}})
        for requested, expected in ((None, 'dev'), ('recorded', 'jp6m'), ('dev', 'dev')):
            local = {'platform': 'jp6m'}
            with patch('rig_cli.manifest.machine_file', return_value=machine), \
                    patch.object(catalog, 'docker_arch', return_value='amd64'):
                catalog.localize(self.root, local, requested)
            self.assertEqual(local['platform'], expected)
            lock = load_lock(self.root)
            self.assertEqual(lock['replay_execution']['source_platform'], 'jp6m')
            self.assertEqual(lock['build_catalog'], self.m.build_catalog)
            self.assertEqual(lock['images'], {'original:5000/core:v1-jp6m': DIGEST})
        machine.unlink()
        with patch('rig_cli.manifest.machine_file', return_value=machine), \
                patch.object(catalog, 'docker_arch', return_value='arm64'):
            local = {'platform': 'jp6m'}
            catalog.localize(self.root, local, None)
            self.assertEqual(local['platform'], 'dev')
            with self.assertRaisesRegex(RigError, 'does not support'):
                catalog.localize(self.root, {}, 'typo')


if __name__ == '__main__':
    unittest.main()
