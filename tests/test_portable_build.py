"""Portable release planning and the actual script/report boundary, without Docker builds."""
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, matrix_build as matrix
from rig_cli.build import _build_env, build
from rig_cli.descriptor import load_descriptor
from rig_cli.lock import load_lock
from rig_cli.manifest import Manifest, RosSettings, Sensor

HELPER = Path(__file__).resolve().parents[1] / 'packaging/rig-build.sh'
DIGEST = 'sha256:' + 'a' * 64


def index(arches=('arm64', 'amd64')):
    return {'manifests': [{'platform': {'os': 'linux', 'architecture': a}, 'digest': DIGEST}
                          for a in arches]}


class PortableBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def service(self, name, extra='', images='[core]'):
        repo = self.root / name
        repo.mkdir()
        (repo / 'rigging.yaml').write_text(f'''service: {name}
launcher: up
build:
  command: build.sh
  images: {images}
  architectures: [arm64, amd64]
  capabilities: {{no_cache: true, platforms: true}}
{extra}''')
        (repo / 'build.sh').write_text('#!/bin/sh\nexit 0\n')
        (repo / 'build.sh').chmod(0o755)
        return load_descriptor(name, repo)

    def manifest(self, *names, **kw):
        return Manifest(vehicle='test', ros=RosSettings(0, 'rmw_zenoh_cpp', 'lyrical'), sensors=[
            Sensor(name=n, service=n, config=self.root / (n + '.yaml'), enabled=True, order=0)
            for n in names], image_registry='registry.test:5000', image_tag='v1', **kw)

    def camera(self):
        return self.service('camera', '''  platforms: [jp6m, jp7, dev]
  variants:
    jp6m: {architectures: [arm64], images: [core, bridge]}
    jp7: {architectures: [arm64], images: [core, bridge]}
    dev: {architectures: [arm64, amd64], images: [dev, bridge]}
replay: {platform: dev}
''', '[core, dev, bridge]')

    def test_default_matrix_ignores_execution_host_and_uses_per_variant_outputs(self):
        descs = {'camera': self.camera(), 'radio': self.service('radio', images='[radio]')}
        snapshots = []
        for host in ('jp6m', 'dev', None):
            m = self.manifest(*descs, platform=host, arch='arm64')
            tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None)
            snapshots.append([(t.variant, t.architectures, t.images, t.tag) for t in tasks])
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0], snapshots[2])
        self.assertEqual(snapshots[0], [
            ('jp6m', ['arm64'], ['core', 'bridge'], 'v1-jp6m'),
            ('jp7', ['arm64'], ['core', 'bridge'], 'v1-jp7'),
            ('dev', ['arm64', 'amd64'], ['dev', 'bridge'], 'v1-dev'),
            (None, ['arm64', 'amd64'], ['radio'], 'v1')])

    def test_scopes_union_targets_and_deduplicate_generic_dependencies(self):
        from rig_cli.targets import Target
        descs = {'camera': self.camera(), 'base': self.service('base', '  provides: base\n', '[fleet-ros]')}
        m = self.manifest(*descs, platform='jp6m')
        def plan(scope):
            return matrix.plan(m, descs, registry=m.image_registry, tag=None, scopes=scope)
        deploy = plan('deployment')
        self.assertEqual([(t.variant, t.architectures) for t in deploy],
                         [(None, ['arm64']), ('jp6m', ['arm64'])])
        with patch('rig_cli.targets.host_target', return_value=Target('dev', 'amd64', 'test host')):
            host = plan('host')
            self.assertEqual([(t.variant, t.architectures) for t in host],
                             [(None, ['amd64']), ('dev', ['amd64'])])
            union = plan(['deployment,host', 'deployment'])
        self.assertEqual([(t.variant, t.architectures) for t in union],
                         [(None, ['amd64', 'arm64']), ('jp6m', ['arm64']), ('dev', ['amd64'])])
        replay = plan('deployment,replay')
        self.assertEqual([(t.variant, t.architectures) for t in replay],
                         [(None, ['amd64', 'arm64']), ('jp6m', ['arm64']), ('dev', ['amd64', 'arm64'])])

    def test_scopes_reject_invalid_or_ambiguous_requests_before_building(self):
        descs = {'camera': self.camera()}
        m = self.manifest(*descs, platform='dev')
        for opts, message in (({'scopes': 'deployment'}, 'architecture is ambiguous'),
                              ({'scopes': 'all,host'}, 'cannot be combined'),
                              ({'scopes': 'typo'}, 'accepts all'),
                              ({'scopes': 'host', 'arch': 'arm64'}, 'require deployment')):
            with self.assertRaisesRegex(RigError, message):
                matrix.plan(m, descs, registry=m.image_registry, tag=None, **opts)
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None,
                            scopes='deployment', arch='amd64')
        self.assertEqual(tasks[0].architectures, ['amd64'])

    def test_replay_scope_respects_startup_policy_and_includes_disabled_player(self):
        descs = {'camera': self.camera(), 'radio': self.service('radio', images='[radio]'),
                 'ros2-bag-player': self.service('ros2-bag-player', images='[player]')}
        m = self.manifest(*descs, platform='jp6m')
        m.sensors[-1] = dataclasses.replace(m.sensors[-1], enabled=False)
        m.replay_policy = {'services': {'radio': {'start': False}}}
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None, scopes='replay')
        self.assertEqual({t.service for t in tasks}, {'camera', 'ros2-bag-player'})
        self.assertTrue(all(set(t.architectures) == {'arm64', 'amd64'} for t in tasks))
        m.replay_policy['instances'] = {'radio': {'start': True}}
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None, scopes='replay')
        self.assertEqual({t.service for t in tasks}, set(descs))

    def test_replay_matrix_provider_follows_consumers_runtime_and_cpu(self):
        base = self.service('base', '  provides: base\n  platforms: [jp6m, dev]\n', '[fleet-ros]')
        camera = self.camera()
        descs = {'base': base, 'camera': camera}
        m = self.manifest(*descs, platform='jp6m')
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None, scopes='replay')
        self.assertEqual([(t.service, t.variant) for t in tasks], [('base', 'dev'), ('camera', 'dev')])
        self.assertTrue(all(set(t.architectures) == {'arm64', 'amd64'} for t in tasks))
        base.build_architectures = ['arm64']
        with self.assertRaisesRegex(RigError, 'shared replay provider lacks'):
            matrix.plan(m, descs, registry=m.image_registry, tag=None, scopes='replay')

    def test_selectors_narrow_generic_dependencies_and_reject_unsupported_pairs(self):
        descs = {'camera': self.camera(), 'radio': self.service('radio', images='[radio]')}
        m = self.manifest(*descs)
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None, platform='jp6m')
        self.assertTrue(all(t.architectures == ['arm64'] for t in tasks))
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag='try', platform='dev', arch='amd64')
        self.assertEqual([t.tag for t in tasks], ['try-dev', 'try'])
        self.assertTrue(all(t.narrowed for t in tasks))
        for opts in ({'platform': 'jp6m', 'arch': 'amd64'}, {'platform': 'typo'}):
            with self.assertRaises(RigError):
                matrix.plan(m, descs, registry=m.image_registry, tag=None, **opts)

    def test_missing_version_retains_bare_runtime_and_latest_tags(self):
        descs = {'camera': self.camera(), 'base': self.service('base', '  provides: base\n', '[fleet-ros]')}
        m = self.manifest(*descs)
        m.image_tag = None
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None)
        self.assertEqual([t.tag for t in tasks], ['latest', 'jp6m', 'jp7', 'dev'])
        self.assertTrue(all(t.base == 'registry.test:5000/fleet-ros:latest' for t in tasks[1:]))

    def test_base_deduplicates_even_when_consumer_appears_first(self):
        base = self.service('base', '  provides: base\n', '[fleet-ros]')
        player = dataclasses.replace(base, service='player', build_provides=None,
                                     build_architectures=['amd64', 'arm64'])
        descs = {'player': player, 'base': base}
        m = self.manifest(*descs)
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].stage, 0)
        self.assertIsNone(tasks[0].base)
        tasks = matrix.plan(m, descs, registry=m.image_registry, tag=None, base='external/base:v2')
        self.assertEqual(tasks, [])

    def test_parent_architectures_must_cover_consumers(self):
        base = self.service('base', '  provides: base\n', '[fleet-ros]')
        base.build_architectures = ['arm64']
        consumer = self.service('consumer')
        m = self.manifest('base', 'consumer')
        with self.assertRaisesRegex(RigError, 'shared base .* lacks'):
            matrix.plan(m, {'base': base, 'consumer': consumer}, registry=m.image_registry, tag=None)

    def test_conflicting_output_scripts_and_missing_contract_fail_before_build(self):
        descs = {n: self.service(n) for n in ('a', 'b')}
        m = self.manifest(*descs)
        with self.assertRaisesRegex(RigError, 'conflicting builds'):
            matrix.plan(m, descs, registry=m.image_registry, tag=None)
        descs['a'].build_multiarch = False
        with self.assertRaisesRegex(RigError, 'capabilities.platforms'):
            matrix.plan(m, descs, registry=m.image_registry, tag=None)

    def test_narrowing_cannot_overwrite_a_complete_index(self):
        desc = self.service('a')
        m = self.manifest('a')
        tasks = matrix.plan(m, {'a': desc}, registry=m.image_registry, tag=None, arch='arm64')
        supported = subprocess.CompletedProcess([], 0, 'Platforms: linux/amd64, linux/arm64', '')
        with patch.object(matrix.subprocess, 'run', return_value=supported), \
                patch.object(matrix, 'inspect_raw', return_value=index()):
            with self.assertRaisesRegex(RigError, 'replace architecture members'):
                matrix.preflight(tasks, m.image_registry)
        with patch.object(matrix.subprocess, 'run', return_value=supported), \
                patch.object(matrix, 'inspect_raw', return_value=None):
            matrix.preflight(tasks, m.image_registry)
        with patch.object(matrix.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, 'Platforms: linux/amd64', '')):
            with self.assertRaisesRegex(RigError, 'builder lacks'):
                matrix.preflight(tasks, m.image_registry)

    def test_no_cache_rejected_without_capability_and_build_channels_do_not_leak(self):
        desc = self.service('a')
        desc.build_no_cache = None
        m = self.manifest('a')
        with patch.object(matrix, 'preflight') as preflight:
            with self.assertRaisesRegex(RigError, 'no declared no-cache'):
                build(m, {'a': desc}, registry=None, tag=None, dry_run=False, no_cache=True)
            preflight.assert_not_called()
        with patch.dict(os.environ, {'RIG_BUILD_PLATFORMS': 'bad', 'RIG_BUILD_RESULT': 'bad',
                                     'RIG_BUILD_PULL': '0', 'RIG_BUILD_NO_CACHE': '1'}):
            env = _build_env(None)
            self.assertFalse(any(k in env for k in ('RIG_BUILD_PLATFORMS', 'RIG_BUILD_RESULT',
                                                    'RIG_BUILD_PULL', 'RIG_BUILD_NO_CACHE')))

    def test_dry_run_does_not_prepare_registry_or_create_builder(self):
        desc = self.service('a')
        with patch.object(matrix.docker_registry, 'prepare') as prepare, \
                patch.object(matrix, 'preflight') as preflight:
            self.assertEqual(build(self.manifest('a'), {'a': desc}, registry=None, tag=None,
                                   dry_run=True), 0)
        prepare.assert_not_called()
        preflight.assert_not_called()

    def test_sequential_and_parallel_builds_pin_fresh_parent_and_save_catalog(self):
        base = self.service('base', '  provides: base\n  msgs_overlay: {command: msgs.sh, image: fleet-msgs}\n',
                            '[fleet-ros]')
        consumer = self.service('consumer', images='[core]')
        consumer.msgs_apt = ['diagnostic_msgs']
        descs = {'consumer': consumer, 'base': base}
        for d, script, image in ((base, 'build.sh', 'fleet-ros'), (base, 'msgs.sh', 'fleet-msgs'),
                                  (consumer, 'build.sh', 'core')):
            shutil.copy2(HELPER, d.repo / 'helper.sh')
            path = d.repo / script
            path.write_text(f'''#!/bin/sh
set -eu
. ./helper.sh
rig_build "$1/{image}:$2" --build-arg "BASE=${{RIG_BASE_IMAGE:-none}}" .
''')
            path.chmod(0o755)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
assert a[:2] == ['buildx', 'build'], a
assert os.environ['BUILDX_BUILDER'] == 'rig-scoped-test'
with open(os.environ['BUILD_LOG'], 'a') as f: f.write(json.dumps(a)+'\\n')
Path(a[a.index('--metadata-file')+1]).write_text(json.dumps({'containerimage.digest': 'sha256:'+'a'*64}))
''')
        docker.chmod(0o755)
        m = self.manifest(*descs)
        log = self.root / 'build.log'
        for jobs in (1, 2):
            log.write_text('')
            with patch.dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                            BUILD_LOG=str(log), PUSH='0'), \
                    patch.object(matrix.docker_registry, 'prepare', return_value=matrix.docker_registry.Access(
                        build_env={'BUILDX_BUILDER': 'rig-scoped-test'},
                        registry_env={'BUILDX_BUILDER': 'rig-registry-test'})) as prepare, \
                    patch.object(matrix, 'preflight'), patch.object(matrix, 'inspect_raw', return_value=index()):
                self.assertEqual(build(m, descs, registry=None, tag=None, dry_run=False,
                                       jobs=jobs, no_cache=True, root=self.root), 0)
            self.assertTrue(prepare.call_args.kwargs['for_build'])
            self.assertIn('registry.test:5000/fleet-ros:v1', prepare.call_args.args[0])
            self.assertIn('registry.test:5000/core:v1', prepare.call_args.args[0])
            calls = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([c[c.index('-t')+1].split('/')[-1] for c in calls],
                             ['fleet-ros:v1', 'fleet-msgs:v1', 'core:v1'])
            for c in calls:
                self.assertIn('--no-cache', c)
                self.assertIn('--push', c)
                self.assertEqual(c[c.index('--platform')+1], 'linux/arm64,linux/amd64')
            for c in calls[1:]:
                self.assertIn(f'BASE=registry.test:5000/fleet-ros@{DIGEST}', c)
            lock = load_lock(self.root)
            self.assertTrue(lock['last_build']['complete'])
            self.assertEqual(len(lock['build_catalog']), 3)
            self.assertTrue(all(r['digest'] == DIGEST and r['no_cache']
                                for r in lock['build_catalog'].values()))


if __name__ == '__main__':
    unittest.main()
