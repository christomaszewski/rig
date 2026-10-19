"""Build-machine registry defaults remain separate from deployment artifacts."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli, matrix_build
from rig_cli.build import build_registry
from rig_cli.image_catalog import artifact_catalog
from rig_cli.lock import load_lock
from rig_cli.manifest import load_manifest

DIGEST = 'sha256:' + 'a' * 64
CHILD = 'sha256:' + 'b' * 64


class BuildRegistry(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / 'vehicle.local.yaml'
        self.machine = self.root / 'machine.yaml'
        env = patch.dict(os.environ, RIG_VEHICLE_LOCAL=str(self.machine),
                         RIG_HOME=str(self.root / 'user'), RIG_VAR_label='shell-only')
        env.start()
        self.addCleanup(env.stop)
        (self.root / 'vehicle.yaml').write_text('''vehicle: demo
vehicle_id: 1
platform: jp6m
arch: arm64
images: {registry: vehicles:5000/release, tag: v1}
vars: {label: release-value}
sensors: [{name: cam, service: camera, config: config/camera.yaml}]
''')
        (self.root / 'config').mkdir()
        (self.root / 'config/camera.yaml').write_text('name: cam\nservice: camera\nlabel: release-value\n')
        (self.root / 'services.yaml').write_text('services: {camera: {path: service}}\n')
        service = self.root / 'service'
        service.mkdir()
        (service / 'rigging.yaml').write_text('''service: camera
launcher: up
launch_surface: [up]
build:
  command: build.py
  images: [camera]
  platforms: [jp6m, dev]
  architectures: [arm64, amd64]
  variants: {jp6m: {architectures: [arm64]}}
  capabilities: {platforms: true}
images: {override_env: {camera: CAMERA_IMAGE}}
replay: {platform: dev}
''')
        (service / 'build.py').write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['RIG_BUILD_RESULT'], 'w') as output:
    output.write(json.dumps({{'ref': sys.argv[1] + '/camera:' + sys.argv[2], 'digest': '{DIGEST}'}}) + '\\n')
''')
        (service / 'build.py').chmod(0o755)
        (service / 'up').write_text('''#!/bin/sh
echo "services: {camera: {image: '${CAMERA_IMAGE}'}}"
''')
        (service / 'up').chmod(0o755)
        self.local.write_text('''images: {registry: localhost:5000/bench, tag: local-tag, base: local-base:bad}
platform: dev
arch: amd64
vars: {label: local-value}
env: {LOCAL_VALUE: local-only}
''')
        self.machine.write_text('''images: {registry: machine:5000/cache, tag: machine-tag}
platform: dev
arch: amd64
vehicle_id: 99
env: {MACHINE_VALUE: machine-only}
''')

    def manifest(self):
        return load_manifest(self.root, context='deployment')

    def test_registry_precedence_and_blank_fallback(self):
        m = self.manifest()
        self.assertEqual(build_registry(self.root, m, 'explicit:5000')[0], 'explicit:5000')
        self.assertEqual(build_registry(self.root, m), ('localhost:5000/bench', str(self.local)))
        for body in ('{}', 'images: {tag: local}', 'images: {registry: null}',
                     'images: {registry: ""}', 'images: {registry: "  "}'):
            self.local.write_text(body)
            self.assertEqual(build_registry(self.root, m), ('machine:5000/cache', str(self.machine)))
        self.local.unlink()
        self.machine.unlink()
        self.assertEqual(build_registry(self.root, m), ('vehicles:5000/release', 'vehicle.yaml'))

    def test_invalid_registry_fails_clearly_but_explicit_argument_bypasses_local_files(self):
        m = self.manifest()
        for body in ('images: []', 'images: {registry: [wrong]}',
                     'images: {registry: "{{host}}:5000"}'):
            self.local.write_text(body)
            with self.assertRaisesRegex(RigError, 'images'):
                build_registry(self.root, m)
            self.assertEqual(build_registry(self.root, m, 'explicit:5000')[0], 'explicit:5000')
        self.local.write_text('images: {registry: " localhost:5000/bench/ "}')
        self.assertEqual(build_registry(self.root, m)[0], 'localhost:5000/bench')

    def test_build_uses_local_registry_without_changing_deployment_inputs(self):
        for local, registry in ((True, 'localhost:5000/bench'), (False, 'machine:5000/cache')):
            with self.subTest(local=local):
                if not local:
                    self.local.unlink()
                with patch.object(matrix_build, 'run', return_value=0) as run, \
                        contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(cli.main(['--root', str(self.root), 'build', '--scope', 'deployment']), 0)
                m = run.call_args.args[0]
                self.assertEqual(run.call_args.kwargs['registry'], registry)
                self.assertIn(registry, output.getvalue())
                self.assertEqual((m.image_registry, m.image_tag, m.image_base, m.platform, m.arch, m.vehicle_id),
                                 ('vehicles:5000/release', 'v1', None, 'jp6m', 'arm64', 1))
                self.assertEqual(m.vars['label'], 'release-value')
                self.assertEqual(m.extra_env, {})

    def test_local_registry_can_supply_missing_deployment_registry_for_build_only(self):
        path = self.root / 'vehicle.yaml'
        path.write_text(path.read_text().replace('registry: vehicles:5000/release, ', ''))
        self.assertIsNone(self.manifest().image_registry)
        with patch.object(matrix_build, 'run', return_value=0) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), 'build', '--dry-run']), 0)
        self.assertEqual(run.call_args.kwargs['registry'], 'localhost:5000/bench')
        self.assertIsNone(run.call_args.args[0].image_registry)

    def test_build_then_bake_rewrites_catalog_and_keeps_local_settings_out(self):
        raw = {'manifests': [{'platform': {'os': 'linux', 'architecture': arch}, 'digest': CHILD}
                            for arch in ('arm64', 'amd64')]}
        with patch.object(matrix_build.docker_registry, 'prepare', return_value=matrix_build.docker_registry.Access()), \
                patch.object(matrix_build, 'preflight'), patch.object(matrix_build, 'inspect_raw', return_value=raw), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), 'build']), 0)
        original = load_lock(self.root)
        self.assertEqual(set(original['build_catalog']),
                         {'localhost:5000/bench/camera:v1-jp6m', 'localhost:5000/bench/camera:v1-dev'})
        # Bake reads neither local file, even if a build-machine override is now malformed.
        self.local.write_text('invalid: [')
        self.machine.write_text('invalid: [')
        for registry in (None, 'other-vehicles:5000/images'):
            with self.subTest(registry=registry):
                tag = 'default' if registry is None else 'explicit'
                args = ['--root', str(self.root), 'bake', '--tag', tag]
                if registry:
                    args += ['--registry', registry]
                with patch('rig_cli.bake._resolve_digest', side_effect=lambda ref, **kw: ref), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(args), 0)
                tree = self.root / 'var/bake' / tag
                target_registry = registry or 'vehicles:5000/release'
                vehicle = yaml.safe_load((tree / 'vehicle.yaml').read_text())
                self.assertEqual(vehicle['images'], {'registry': target_registry, 'tag': 'v1'})
                self.assertEqual((vehicle['platform'], vehicle['arch'], vehicle['vehicle_id']), ('jp6m', 'arm64', 1))
                lock = load_lock(tree)
                refs = {f'{target_registry}/camera:v1-{runtime}' for runtime in ('jp6m', 'dev')}
                self.assertEqual(set(lock['build_catalog']), refs)
                self.assertEqual(set(lock['last_build']['outputs']), refs)
                for ref, row in lock['build_catalog'].items():
                    self.assertEqual(row['ref'], ref)
                    self.assertEqual(row['digest'], DIGEST)
                    self.assertTrue(all(digest == CHILD for digest in row['children'].values()))
                compose = (tree / 'compose/cam/docker-compose.yaml').read_text()
                self.assertIn(f'{target_registry}/camera@{DIGEST}', compose)
                self.assertFalse((tree / 'vehicle.local.yaml').exists())
                for relative in ('vehicle.yaml', 'metadata.yaml', 'rig.lock', 'compose/cam/docker-compose.yaml',
                                 'config/sensors/cam.yaml'):
                    text = (tree / relative).read_text()
                    for value in ('localhost:5000/bench', 'machine:5000/cache', 'local-tag',
                                  'machine-tag', 'local-base:bad', 'local-value', 'shell-only'):
                        self.assertNotIn(value, text, relative)
        self.assertEqual(load_lock(self.root), original)
        # Templated artifacts defer registry selection until execution on the vehicle.
        (self.root / 'config/camera.yaml').write_text('name: cam\nservice: camera\nlabel: "{{label}}"\n')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), 'bake', '--tag', 'templated']), 0)
        tree = self.root / 'var/bake/templated'
        lock = load_lock(tree)
        self.assertEqual(set(lock['build_catalog']), {'camera:v1-jp6m', 'camera:v1-dev'})
        self.assertNotIn('localhost:5000/bench', (tree / 'rig.lock').read_text())
        self.assertFalse((tree / 'vehicle.local.yaml').exists())

    def test_artifact_catalog_without_registry_uses_logical_names(self):
        original = {'localhost:5000/bench/camera:v1-dev':
                    {'image': 'camera', 'digest': DIGEST, 'ref': 'localhost:5000/bench/camera:v1-dev'}}
        catalog, refs = artifact_catalog(original, None)
        self.assertEqual(catalog['camera:v1-dev']['ref'], 'camera:v1-dev')
        self.assertEqual(refs, {'localhost:5000/bench/camera:v1-dev': 'camera:v1-dev'})
        self.assertIn('localhost:5000/bench/camera:v1-dev', original)


if __name__ == '__main__':
    unittest.main()
