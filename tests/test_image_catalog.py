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

    def test_bake_keeps_index_digest_and_requires_a_bundle_target(self):
        from rig_cli import bake
        ref = 'registry/dev@' + DIGEST
        with patch.object(bake.subprocess, 'run') as run:
            self.assertEqual(bake._resolve_digest(ref), ref)
            run.assert_not_called()
        with self.assertRaisesRegex(RigError, 'architecture is unknown'):
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
            self.assertNotIn('arch', local)  # legacy embedded Rig may not accept this local key
            lock = load_lock(self.root)
            self.assertEqual(lock['replay_execution']['architecture'], 'amd64')
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


class ImageAdopt(unittest.TestCase):
    """`rig image adopt`: a row from the registry's own manifest, marked adopted — never another
    row's provenance — so replay accepts a hand-pushed tag and bake still refuses to ship it."""
    REG = 'reg.test:5000'
    INDEX, AMD, ARM = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64, 'sha256:' + '3' * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {'RIG_VEHICLE_LOCAL': str(self.root / 'absent.yaml')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.built = {'ref': f'{self.REG}/fleet-ros:v1', 'image': 'fleet-ros', 'digest': DIGEST,
                      'architectures': ['amd64', 'arm64'], 'children': {'amd64': DIGEST, 'arm64': DIGEST},
                      'service': 'fleet-ros', 'variant': None, 'source_revision': 'abc123def456789'}
        save_lock(self.root, {'build_catalog': {f'{self.REG}/fleet-ros:v1': self.built}})
        self.m = Manifest(vehicle='test', sensors=[], ros=RosSettings(0, 'rmw_zenoh_cpp', None),
                          image_registry=self.REG, image_tag='v1')

    def registry(self, raw, *, manifest=None, image=None):
        def tools(args, refs, **kwargs):
            fmt = args[args.index('--format') + 1]
            doc = manifest if fmt == '{{json .Manifest}}' else image
            import json
            return subprocess.CompletedProcess(args, 0 if doc else 1, json.dumps(doc) if doc else '', 'denied')
        return patch('rig_cli.matrix_build.inspect_raw', return_value=raw), \
            patch('rig_cli.docker_registry.imagetools', side_effect=tools)

    def adopt(self, ref, raw, *, manifest=None, image=None, **kwargs):
        inspect, tools = self.registry(raw, manifest=manifest, image=image)
        with inspect, tools, patch('sys.stderr') as err:
            catalog.adopt(self.root, self.m, ref, **kwargs)
        return ''.join(call.args[0] for call in err.write.call_args_list)

    def index(self):
        return {'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': self.AMD},
                              {'platform': {'os': 'linux', 'architecture': 'arm64'}, 'digest': self.ARM},
                              {'platform': {'os': 'unknown', 'architecture': 'unknown'}, 'digest': DIGEST}]}

    def test_an_index_is_recorded_from_the_registry_and_marked_adopted(self):
        ref = f'{self.REG}/fleet-ros:sim'
        self.adopt(ref, self.index(), manifest={'digest': self.INDEX})
        rows = load_lock(self.root)['build_catalog']
        row = rows[ref]
        self.assertEqual((row['digest'], row['children'], row['architectures']),
                         (self.INDEX, {'amd64': self.AMD, 'arm64': self.ARM}, ['amd64', 'arm64']))
        self.assertEqual((row['image'], row['service']), ('fleet-ros', 'fleet-ros'))
        self.assertIsNone(row['source_revision'])  # never the flown row's provenance
        self.assertIn('at', row['adopted'])
        self.assertEqual(rows[f'{self.REG}/fleet-ros:v1'], self.built)  # another tag stays
        self.assertEqual(catalog.adopted_rows(rows), [ref])
        # replay's companion check now finds it (it refused before: "no matching companion catalog entry")
        from rig_cli.artifact_images import source_for
        from rig_cli.targets import Target
        m = Manifest(vehicle='test', sensors=[], ros=RosSettings(0, 'rmw_zenoh_cpp', None),
                     image_registry=self.REG, build_catalog=rows)
        source, companions = source_for(m, ref, Target(None, 'amd64', 'replay'))
        self.assertEqual(source, f'{self.REG}/fleet-ros@{self.AMD}')
        self.assertIn(ref, companions)

    def test_a_single_platform_push_replaces_the_rows_for_its_tag(self):
        ref = f'{self.REG}/fleet-ros:v1'
        plain = {'schemaVersion': 2, 'config': {'digest': DIGEST}, 'layers': []}
        out = self.adopt(ref, plain, manifest={'digest': self.AMD}, image={'os': 'linux', 'architecture': 'amd64'})
        self.assertIn('replaces reg.test:5000/fleet-ros:v1 (built from abc123def456', out)
        row = load_lock(self.root)['build_catalog'][ref]
        self.assertEqual((row['digest'], row['children']), (self.AMD, {'amd64': self.AMD}))
        self.assertTrue(row['adopted'])

    def test_refusals_name_the_fix(self):
        for ref, raw, needle in (
                ('elsewhere:5000/fleet-ros:sim', self.index(), 'docker buildx imagetools create'),
                (f'{self.REG}/fleet-ros@{DIGEST}', self.index(), 'adopt the tag'),
                (f'{self.REG}/fleet-ros:gone', None, 'push it first')):
            with self.subTest(ref=ref), self.assertRaisesRegex(RigError, needle):
                self.adopt(ref, raw, manifest={'digest': self.INDEX})
        with self.assertRaisesRegex(RigError, 'cannot read the manifest digest'):
            self.adopt(f'{self.REG}/fleet-ros:sim', self.index(), manifest=None)
        with self.assertRaisesRegex(RigError, 'names no linux architecture'):
            self.adopt(f'{self.REG}/fleet-ros:sim', {'config': {}}, manifest={'digest': self.AMD}, image=None)
        self.assertNotIn(f'{self.REG}/fleet-ros:sim', load_lock(self.root)['build_catalog'])

    def test_like_picks_the_service_and_runtime_and_dry_run_writes_nothing(self):
        lock = load_lock(self.root)
        lock['build_catalog'][f'{self.REG}/fleet-ros:v1-jp7'] = dict(self.built, variant='jp7')
        save_lock(self.root, lock)
        with self.assertRaisesRegex(RigError, 'several services or runtimes; pass --like'):
            self.adopt(f'{self.REG}/fleet-ros:sim', self.index(), manifest={'digest': self.INDEX})
        with self.assertRaisesRegex(RigError, 'is not in rig.lock'):
            self.adopt(f'{self.REG}/fleet-ros:sim', self.index(), manifest={'digest': self.INDEX},
                       like=f'{self.REG}/fleet-ros:nope')
        before = (self.root / 'rig.lock').read_text()
        self.adopt(f'{self.REG}/fleet-ros:sim', self.index(), manifest={'digest': self.INDEX},
                   like=f'{self.REG}/fleet-ros:v1-jp7', dry_run=True)
        self.assertEqual((self.root / 'rig.lock').read_text(), before)
        self.adopt(f'{self.REG}/fleet-ros:sim', self.index(), manifest={'digest': self.INDEX},
                   like=f'{self.REG}/fleet-ros:v1-jp7')
        row = load_lock(self.root)['build_catalog'][f'{self.REG}/fleet-ros:sim']
        self.assertEqual((row['variant'], row['adopted']['like']), ('jp7', f'{self.REG}/fleet-ros:v1-jp7'))

    def test_bake_refuses_an_adopted_row_unless_allowed(self):
        from rig_cli import bake
        rows = {f'{self.REG}/fleet-ros:sim': {'image': 'fleet-ros', 'adopted': {'at': 'now'}},
                f'{self.REG}/fleet-ros:v1': self.built}
        with self.assertRaisesRegex(RigError, r'adopted with rig image adopt.*fleet-ros:sim.*--allow-local'):
            bake._refuse_adopted(rows, False)
        with patch('sys.stderr') as err:
            bake._refuse_adopted(rows, True)
        self.assertIn('warning (--allow-local)', ''.join(c.args[0] for c in err.write.call_args_list))
        bake._refuse_adopted({f'{self.REG}/fleet-ros:v1': self.built}, False)  # rig-built rows ship

    def test_cli_spelling(self):
        (self.root / 'vehicle.yaml').write_text(f'vehicle: t\nimages: {{registry: "{self.REG}"}}\nsensors: []\n')
        (self.root / 'services.yaml').write_text('services: {}\n')
        from rig_cli import cli
        inspect, tools = self.registry(self.index(), manifest={'digest': self.INDEX})
        with inspect, tools, patch('sys.stderr'):
            self.assertEqual(cli.main(['--root', str(self.root), 'image', 'adopt',
                                       f'{self.REG}/fleet-ros:sim', '--dry-run']), 0)


if __name__ == '__main__':
    unittest.main()
