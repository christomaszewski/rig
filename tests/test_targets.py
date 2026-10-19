"""Deployment/host/execution boundaries and target selection; no Docker daemon required."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli
from rig_cli.descriptor import load_descriptor
from rig_cli.dispatch import fleet_env
from rig_cli.manifest import load_manifest
from rig_cli.targets import deployment_target, host_target


class Targets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.machine = self.root / 'machine.yaml'
        self.machine.write_text('platform: dev\narch: amd64\nvehicle_id: 99\n')
        self.env = patch.dict(os.environ, RIG_VEHICLE_LOCAL=str(self.machine),
                              RIG_HOME=str(self.root / 'user'), RIG_VEHICLE_ID='88',
                              BENCH_IMAGE='bench-only:bad')
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / 'vehicle.yaml').write_text('''vehicle: source
platform: jp6m
arch: arm64
images: {registry: release:5000, tag: v1}
env: {SOURCE_VALUE: committed}
sensors: [{name: cam, service: camera, config: camera.yaml}]
''')
        (self.root / 'vehicle.local.yaml').write_text('''platform: dev
arch: amd64
images: {registry: local:5000, tag: local}
env: {LOCAL_VALUE: local}
''')
        (self.root / 'fleet.yaml').write_text('vars: {fleet_only: local}\n')
        (self.root / 'camera.yaml').write_text('name: cam\nservice: camera\n')
        repo = self.root / 'service'
        repo.mkdir()
        (self.root / 'services.yaml').write_text('services: {camera: {path: service}}\n')
        (repo / 'rigging.yaml').write_text('''service: camera
launcher: up
launch_surface: [up]
build:
  command: build.sh
  images: [camera]
  platforms: [jp6m, dev]
  architectures: [arm64, amd64]
  variants: {jp6m: {architectures: [arm64]}}
  capabilities: {platforms: true}
replay: {platform: dev}
''')
        (repo / 'up').write_text('''#!/bin/sh
echo "services: {camera: {image: '${BENCH_IMAGE:-release:5000/camera:v1}'}}"
''')
        (repo / 'up').chmod(0o755)
        self.descs = {'camera': load_descriptor('camera', repo)}

    def test_deployment_sources_are_isolated_but_execution_keeps_overrides(self):
        release = load_manifest(self.root, context='deployment')
        local = load_manifest(self.root)
        self.assertEqual((release.platform, release.arch, release.image_registry, release.vehicle_id),
                         ('jp6m', 'arm64', 'release:5000', None))
        self.assertEqual((local.platform, local.arch, local.image_registry, local.vehicle_id),
                         ('dev', 'amd64', 'local:5000', '88'))
        self.assertEqual(release.extra_env, {'SOURCE_VALUE': 'committed'})
        self.assertNotIn('fleet_only', release.vars)
        self.assertNotIn('BENCH_IMAGE', fleet_env(release))
        self.assertIn('BENCH_IMAGE', fleet_env(local))

    def test_bake_serializes_only_deployment_inputs(self):
        with patch('rig_cli.bake._resolve_digest', return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), 'bake', '--tag', 'test']), 0)
        tree = self.root / 'var/bake/test'
        data = yaml.safe_load((tree / 'vehicle.yaml').read_text())
        self.assertEqual(data['platform'], 'jp6m')
        self.assertEqual(data['images']['registry'], 'release:5000')
        self.assertEqual(data['env'], {'SOURCE_VALUE': 'committed'})
        self.assertNotIn('vehicle_id', data)
        self.assertNotIn('bench-only', (tree / 'compose/cam/docker-compose.yaml').read_text())
        self.assertFalse((tree / 'vehicle.local.yaml').exists())

    def test_deployment_can_defer_external_variables_but_rejects_cycles(self):
        file = self.root / 'vehicle.yaml'
        file.write_text('vehicle: "{{vehicle}}"\nvars: {camera_ip: "10.0.{{vehicle_id}}.1"}\nsensors: []\n')
        manifest = load_manifest(self.root, context='deployment')
        self.assertEqual(manifest.vars['camera_ip'], '10.0.{{vehicle_id}}.1')
        self.assertIn('vehicle', manifest.missing_identity)
        file.write_text('vehicle: demo\nvars: {a: "{{b}}", b: "{{a}}"}\nsensors: []\n')
        with self.assertRaisesRegex(RigError, 'cycle'):
            load_manifest(self.root, context='deployment')

    def test_target_inference_and_host_selection_use_distinct_sources(self):
        m = load_manifest(self.root, context='deployment')
        m.arch = None
        self.assertEqual(deployment_target(m, self.descs).arch, 'arm64')
        with self.assertRaisesRegex(RigError, 'ambiguous'):
            deployment_target(m, self.descs, platform='dev')
        with self.assertRaisesRegex(RigError, 'unsupported architecture'):
            deployment_target(m, self.descs, arch='amd64')
        self.assertEqual(deployment_target(m, self.descs, platform='dev', arch='amd64').runtime, 'dev')
        with patch('rig_cli.image_catalog.docker_arch', return_value='amd64'):
            self.assertEqual(host_target(self.descs).runtime, 'dev')
        with patch('rig_cli.image_catalog.docker_arch', return_value='arm64'):
            with self.assertRaisesRegex(RigError, 'does not match'):
                host_target(self.descs)
        self.machine.write_text('{}\n')
        with patch('rig_cli.image_catalog.docker_arch', return_value='arm64'):
            self.assertEqual(host_target(self.descs).arch, 'arm64')

    def test_target_arguments_resolve_dependent_variables_before_planning(self):
        file = self.root / 'vehicle.yaml'
        file.write_text('''vehicle: demo
platform: jp6m
arch: arm64
vars: {runtime_label: "selected-{{platform}}-{{arch}}"}
env: {LABEL: "{{runtime_label}}"}
sensors: []
''')
        manifest = load_manifest(self.root, context='deployment',
                                 target_overrides={'platform': 'dev', 'arch': 'amd64'})
        self.assertEqual(manifest.extra_env['LABEL'], 'selected-dev-amd64')
        self.assertEqual(manifest.vars['runtime_label'], 'selected-dev-amd64')


if __name__ == '__main__':
    unittest.main()
