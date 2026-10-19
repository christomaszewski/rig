"""Architecture-specific export, deferred fleet image planning and runtime image membership."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, artifact_images, artifact_runtime, bake, cli
from rig_cli.catalog import load_catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env
from rig_cli.lock import save_lock, load_lock
from rig_cli.manifest import Manifest, RosSettings, Sensor, load_manifest
from rig_cli.resolve import materialize_manifest
from rig_cli.targets import Target
from image_archive_fixture import CONFIG_ID, MANIFEST_ID, write_archive

INDEX = 'sha256:' + '1' * 64
CHILD = 'sha256:' + '2' * 64
CONFIG = CONFIG_ID
OTHER = 'sha256:' + '4' * 64


class Docker:
    def __init__(self):
        self.calls = []
        self.arch = 'arm64'
        self.api = '1.48'
        self.info = dict(Id=CONFIG, Os='linux', Architecture=self.arch)
        self.images = {'reg/live@' + CHILD: self.info}
        self.actual_run = subprocess.run

    def run(self, cmd, **kwargs):
        if cmd[0] != 'docker':
            return self.actual_run(cmd, **kwargs)
        self.calls.append(cmd)
        args = cmd[1:]
        result, code = '', 0
        if args[0] == 'version':
            result = self.api
        elif args[:2] == ['image', 'inspect']:
            info = self.images.get(args[-1])
            code = 0 if info else 1
            if info and '--format' in args:
                result = json.dumps(info) if args[args.index('--format') + 1] == '{{json .}}' else info['Id']
        elif args[0] == 'pull':
            self.images[args[-1]] = dict(self.info, Architecture=self.arch)
        elif args[0] == 'tag':
            self.images[args[-1]] = next(info for info in self.images.values() if info['Id'] == args[1])
        elif args[0] == 'save':
            assert args[1:3] == ['--platform', 'linux/arm64'], args
            write_archive(Path(args[args.index('-o') + 1]), args[args.index('-o') + 2:])
        elif args[:2] == ['image', 'rm']:
            del self.images[args[-1]]
        elif args[0] == 'inspect':
            result = '[]'
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, code, result, '')


class ArtifactImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.docker = Docker()
        self.target = Target('jp6m', 'arm64', 'test')
        self.catalog = {
            'reg/live:v1-jp6m': dict(image='live', digest=INDEX, architectures=['arm64'], children={'arm64': CHILD}),
            'reg/replay:v1-dev': dict(image='replay', digest=OTHER, architectures=['arm64', 'amd64'],
                                      children={'arm64': OTHER, 'amd64': OTHER}),
        }
        self.manifest = Manifest(vehicle='test', ros=RosSettings(0, 'rmw_zenoh_cpp', None), sensors=[],
                                 image_registry='reg', image_tag='v1', platform='jp6m', arch='arm64',
                                 build_catalog=self.catalog)

    def test_exports_selected_child_and_cleans_only_its_temporary_aliases(self):
        with patch('subprocess.run', side_effect=self.docker.run):
            result = artifact_images.export_images(self.root, ['reg/live@' + INDEX], self.manifest, self.target)
        self.assertEqual(set(self.docker.images), {'reg/live@' + CHILD})
        self.assertEqual(set(result['image_ids'].values()), {CONFIG})
        self.assertIn('reg/live:v1-jp6m', result['refs'])
        self.assertEqual(result['sources']['reg/live@' + INDEX]['source'], 'reg/live@' + CHILD)
        saves = [c for c in self.docker.calls if c[1] == 'save']
        self.assertEqual(len(saves), 1)
        self.assertNotIn('replay', ' '.join(' '.join(c) for c in self.docker.calls))
        self.assertEqual(len(self.manifest.build_catalog), 2)

    def test_missing_local_image_pulls_exact_architecture_and_child(self):
        self.docker.images.clear()
        with patch('subprocess.run', side_effect=self.docker.run):
            artifact_images.export_images(self.root, ['reg/live:v1-jp6m'], self.manifest, self.target)
        self.assertIn(['docker', 'pull', '--platform', 'linux/arm64', 'reg/live@' + CHILD], self.docker.calls)

    def test_containerd_identity_records_config_and_exported_manifest(self):
        self.docker.info['Id'] = MANIFEST_ID
        with patch('subprocess.run', side_effect=self.docker.run):
            result = artifact_images.export_images(self.root, ['reg/live:v1-jp6m'], self.manifest, self.target)
        self.assertEqual(set(result['image_ids'].values()), {MANIFEST_ID})
        self.assertEqual(set(result['config_ids'].values()), {CONFIG})
        self.assertEqual(set(next(iter(result['accepted_ids'].values()))), {CONFIG, MANIFEST_ID})

    def test_missing_child_wrong_cpu_and_old_daemon_refuse(self):
        self.catalog['reg/live:v1-jp6m']['children'] = {}
        with self.assertRaisesRegex(RigError, 'child digest'):
            artifact_images.source_for(self.manifest, 'reg/live:v1-jp6m', self.target)
        self.catalog['reg/live:v1-jp6m']['children'] = {'arm64': CHILD}
        self.docker.images.clear()
        self.docker.arch = 'amd64'
        with patch('subprocess.run', side_effect=self.docker.run):
            with self.assertRaisesRegex(RigError, 'not a verified linux/arm64'):
                artifact_images.export_images(self.root, ['reg/live:v1-jp6m'], self.manifest, self.target)
        self.assertFalse(any(c[1] == 'save' for c in self.docker.calls))
        self.docker.api = '1.47'
        with patch('subprocess.run', side_effect=self.docker.run):
            with self.assertRaisesRegex(RigError, '1.48'):
                artifact_images.export_images(self.root, ['reg/live:v1-jp6m'], self.manifest, self.target)

    def test_catalog_runtime_and_planner_contract_are_validated(self):
        self.catalog['reg/live:v1-jp6m']['variant'] = 'jp6m'
        with self.assertRaisesRegex(RigError, 'selects runtime jp6m'):
            artifact_images.source_for(self.manifest, 'reg/live:v1-jp6m', Target('dev', 'arm64', 'test'))
        self.fleet()
        descriptor = self.root / 'camera/rigging.yaml'
        descriptor.write_text(descriptor.read_text().replace('images: images', 'images: true'))
        with self.assertRaisesRegex(RigError, 'verbs.images'):
            load_descriptor('camera', descriptor.parent)

    def test_bootstrap_dry_run_does_not_load_images(self):
        bake._write_bootstrap(self.root)
        (self.root / 'ensure-images.sh').write_text('#!/bin/sh\nexit 99\n')
        (self.root / 'ensure-images.sh').chmod(0o755)
        (self.root / 'rig').write_text('import sys\nassert sys.argv[1:] == ["up", "--dry-run"]\n')
        result = subprocess.run([str(self.root / 'run.sh'), 'up', '--dry-run'],
                                env=dict(os.environ, PATH=str(Path(sys.executable).parent) + ':' + os.environ['PATH']),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_load_failure_returns_without_starting_containers(self):
        (self.root / 'metadata.yaml').write_text('bundle: {refs: {old: alias}}\n')
        with patch('rig_cli.bake._compose_only', return_value=([], [])), \
                patch('rig_cli.artifact_runtime.check_target'), \
                patch('subprocess.run', return_value=subprocess.CompletedProcess([], 9)) as run:
            self.assertEqual(artifact_runtime.run_bundle(self.root, self.manifest, {}, {}), 9)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], [str(self.root / 'ensure-images.sh')])

    def test_explicit_disabled_instance_is_validated_against_bundle(self):
        self.manifest.sensors = [Sensor(name='optional', service='camera', config=self.root / 'cam.yaml',
                                        enabled=False, order=0)]
        args = SimpleNamespace(rig_root=self.root, dry_run=True, force=False, target_state=None,
                               run=None, names=['optional'])
        with patch('rig_cli.cli.doctor_mod.collect', return_value=[]), \
                patch('rig_cli.cli.dispatch.fleet_env', return_value={}), \
                patch('rig_cli.artifact_runtime.run_bundle', return_value=0) as run:
            self.assertEqual(cli.cmd_up(args, self.manifest, {}, {'camera': None}), 0)
        selected = run.call_args.args[1].sensors
        self.assertEqual([s.name for s in selected], ['optional'])
        self.assertTrue(selected[0].enabled)  # render it and enforce membership, never silently skip it

    def fleet(self):
        (self.root / 'vehicle.yaml').write_text('''vehicle: "{{vehicle}}"
vehicle_id: "{{vehicle_id}}"
platform: jp6m
images: {registry: reg, tag: v1}
env: {DEVICE_ID: "{{vehicle_id}}"}
sensors: [{name: cam, service: camera, config: config/cam.yaml}]
''')
        (self.root / 'vehicle.local.yaml').write_text('vehicle: bench\nvehicle_id: 99\nplatform: dev\narch: amd64\n')
        (self.root / 'config').mkdir()
        (self.root / 'config/cam.yaml').write_text('name: cam\nservice: camera\naddress: "10.0.{{vehicle_id}}.1"\n')
        repo = self.root / 'camera'
        repo.mkdir()
        (self.root / 'services.yaml').write_text('services: {camera: {path: camera}}\n')
        (repo / 'rigging.yaml').write_text('''service: camera
launcher: up
launch_surface: [up]
verbs: {images: images}
build:
  command: build.sh
  images: [live, replay]
  platforms: [jp6m, dev]
  architectures: [arm64, amd64]
  variants: {jp6m: {architectures: [arm64], images: [live]}, dev: {images: [replay]}}
  capabilities: {platforms: true}
''')
        (repo / 'up').write_text('''#!/bin/sh
case "$2" in
  images)
    [ "${BENCH_IMAGE-unset}" = unset ] || exit 8
    [ "${VEHICLE_ID-unset}" = unset ] || exit 10
    [ "${DEVICE_ID-unset}" = unset ] || exit 11
    grep -q '{{vehicle_id}}' "$1" || exit 9
    echo '[reg/live:v1-jp6m]' ;;
  config) echo 'services: {app: {image: reg/live:v1-jp6m}}' ;;
  *) exit 1 ;;
esac
''')
        (repo / 'up').chmod(0o755)
        save_lock(self.root, {'schema': 1, 'build_catalog': self.catalog,
                              'replay_execution': {'platform': 'local-only'}})

    def test_fleet_bundles_vehicle_images_keeps_templates_and_renders_on_vehicle(self):
        self.fleet()
        machine = self.root / 'machine.yaml'
        machine.write_text('vehicle: real-vehicle\nvehicle_id: 7\nplatform: jp6m\narch: arm64\n')
        with patch.dict(os.environ, RIG_VEHICLE_LOCAL=str(machine), RIG_HOME=str(self.root / 'user'),
                        BENCH_IMAGE='local:bad'), patch('subprocess.run', side_effect=self.docker.run), \
                contextlib.redirect_stderr(io.StringIO()):
            tar = bake.bake_fleet(self.root, 'release', bundle_images=True)
        self.assertTrue(tar.is_file())
        tree = self.root / 'var/bake/release'
        self.assertIn('{{vehicle_id}}', (tree / 'config/cam.yaml').read_text())
        self.assertFalse((tree / 'vehicle.local.yaml').exists())
        lock = load_lock(tree)
        self.assertEqual(len(lock['build_catalog']), 2)
        self.assertNotIn('replay_execution', lock)
        meta = yaml.safe_load((tree / 'metadata.yaml').read_text())
        self.assertEqual(meta['target']['arch'], 'arm64')
        self.assertEqual(len(meta['bundle']['image_ids']), 1)
        self.assertTrue((tree / 'ensure-images.sh').is_file())
        with patch.dict(os.environ, RIG_VEHICLE_LOCAL=str(machine), RIG_HOME=str(self.root / 'user')):
            manifest = materialize_manifest(load_manifest(tree), tree)
            routes = load_catalog(tree)
            descs = {'camera': load_descriptor('camera', routes['camera'].path)}
            with patch('rig_cli.bake._resolve_digest', return_value=None):
                self.assertEqual(artifact_runtime.run_bundle(tree, manifest, descs,
                                 fleet_env(manifest, descs), dry_run=True), 0)
            compose = yaml.safe_load((tree / 'var/bundle-runtime/compose/cam/docker-compose.yaml').read_text())
            self.assertTrue(compose['services']['app']['image'].startswith('rig-bundle/'))
            self.assertEqual(compose['services']['app']['pull_policy'], 'never')
            self.assertIn('10.0.7.1', manifest.sensors[0].config.read_text())
            with patch('rig_cli.image_catalog.docker_arch', return_value='amd64'):
                with self.assertRaisesRegex(RigError, 'requires arm64'):
                    artifact_runtime.check_target(tree, manifest)
            launcher = tree / 'services/camera/up'
            launcher.write_text(launcher.read_text().replace('image: reg/live:v1-jp6m', 'image: unbundled:new'))
            with patch('rig_cli.bake._resolve_digest', return_value=None):
                with self.assertRaisesRegex(RigError, 'unbundled image'):
                    artifact_runtime.run_bundle(tree, manifest, descs, fleet_env(manifest, descs), dry_run=True)


if __name__ == '__main__':
    unittest.main()
