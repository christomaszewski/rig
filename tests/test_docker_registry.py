"""Docker-approved HTTP/TLS policy, isolated Buildx configuration and caller integration."""
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, docker_registry as registry
from rig_cli.matrix_build import inspect_raw, preflight

HOST = 'registry.test:5000'
REF = HOST + '/team/core:v1'
POLICY = {'IndexConfigs': {HOST: {'Secure': False}, 'docker.io': {'Secure': True}},
          'InsecureRegistryCIDRs': ['127.0.0.0/8', '::1/128']}
INFO = {'id': 'test-daemon', 'registries': POLICY,
        'driver_status': [['driver-type', 'io.containerd.snapshotter.v1']]}
CLASSIC_INFO = {**INFO, 'driver_status': [['Backing Filesystem', 'extfs']]}


def completed(cmd, output='', code=0, error=''):
    return subprocess.CompletedProcess(cmd, code, output, error)


class RegistryPolicyTests(unittest.TestCase):
    def test_registry_authority_not_namespace_or_tag(self):
        for ref, host in ((REF, HOST), ('[::1]:5000/team/core@sha256:123', '[::1]:5000'),
                          ('localhost:5000/core:v1', 'localhost:5000'),
                          ('LOCALHOST:5000/core:v1', 'LOCALHOST:5000'),
                          ('ubuntu:latest', 'docker.io'), ('team/core:v1', 'docker.io'),
                          ('index.docker.io/team/core:v1', 'docker.io')):
            self.assertEqual(registry.registry_host(ref), host)

    def test_exact_entries_override_cidr_rules(self):
        self.assertTrue(registry.insecure_registry(HOST, POLICY))
        self.assertTrue(registry.insecure_registry(HOST.upper(), POLICY))
        self.assertFalse(registry.insecure_registry('docker.io', POLICY))
        policy = {'IndexConfigs': {'127.0.0.1:5000': {'Secure': True}},
                  'InsecureRegistryCIDRs': ['127.0.0.0/8']}
        self.assertFalse(registry.insecure_registry('127.0.0.1:5000', policy))

    def test_cidr_ipv4_ipv6_dns_and_misses(self):
        self.assertTrue(registry.insecure_registry('127.0.0.2:5000', POLICY))
        self.assertTrue(registry.insecure_registry('[::1]:5000', POLICY))
        with patch.object(registry, '_addresses', return_value=['10.20.0.8']):
            self.assertTrue(registry.insecure_registry('radio.test:5000',
                            {'InsecureRegistryCIDRs': ['10.20.0.0/16', 'invalid']}))
            self.assertFalse(registry.insecure_registry('radio.test:5000', POLICY))
        with patch.object(registry, '_addresses', return_value=[]):
            self.assertFalse(registry.insecure_registry('missing.test:5000', POLICY))

    def test_https_preferred_for_docker_approved_self_signed_registry(self):
        with patch.object(registry, '_probe') as probe:
            self.assertEqual(registry.registry_transport(HOST), 'insecure')
        probe.assert_called_once_with(HOST, 'https')

    def test_http_only_after_tls_fails(self):
        with patch.object(registry, '_probe', side_effect=[ssl.SSLError('wrong version'), None]) as probe:
            self.assertEqual(registry.registry_transport(HOST), 'http')
        self.assertEqual([c.args for c in probe.call_args_list], [(HOST, 'https'), (HOST, 'http')])

    def test_unreachable_registry_fails_before_creating_builder(self):
        with patch.object(registry, '_probe', side_effect=OSError('offline')):
            with self.assertRaisesRegex(RigError, 'cannot reach Docker-approved'):
                registry.registry_transport(HOST)

    def test_authenticated_registry_transport_probe_needs_no_credentials(self):
        for code in (301, 401, 403, 404, 503):
            with self.subTest(code=code), patch.object(registry.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    f'https://{HOST}/v2/', code, 'response', {}, io.BytesIO(b''))
                registry._probe(HOST, 'https')
                args, kwargs = opener.return_value.open.call_args
                self.assertEqual(args, (f'https://{HOST}/v2/',))
                self.assertEqual(kwargs, {'timeout': 3})

    def test_daemon_policy_read_failure_never_grants_http(self):
        for proc in (completed([], code=1), completed([], '{}'), completed([], 'not-json')):
            with patch.object(registry.subprocess, 'run', return_value=proc), \
                    patch.object(registry, '_managed_builder') as builder:
                self.assertEqual(registry.prepare([REF]), registry.Access())
                builder.assert_not_called()

    def test_daemon_query_reads_image_store_capability_with_registry_policy(self):
        with patch.object(registry.subprocess, 'run', return_value=completed([], json.dumps(INFO))) as run:
            self.assertEqual(registry._docker_info(), INFO)
        self.assertIn('{{json .DriverStatus}}', run.call_args.args[0][-1])

    def test_secure_registry_does_not_probe_or_create_builder(self):
        with patch.object(registry, '_docker_info', return_value=INFO), \
                patch.object(registry, 'registry_transport') as transport, \
                patch.object(registry, '_managed_builder') as builder:
            self.assertEqual(registry.prepare(['docker.io/library/alpine:latest']), registry.Access())
            transport.assert_not_called()
            builder.assert_not_called()


class ManagedBuilderTests(unittest.TestCase):
    def test_config_preserves_reference_authority_case(self):
        with patch.object(registry, '_docker_info', return_value=INFO), \
                patch.object(registry, 'registry_transport', return_value='http'), \
                patch.object(registry, '_managed_builder', return_value='rig-test') as builder:
            registry.prepare([HOST.upper() + '/core:v1'])
        builder.assert_called_once_with(INFO, {HOST.upper(): 'http'})

    def test_config_exact_hosts_no_global_selection_and_reuse(self):
        calls, configs = [], []
        exists = False

        def run(cmd, **kwargs):
            nonlocal exists
            calls.append(cmd)
            if cmd[:3] == ['docker', 'buildx', 'inspect']:
                return completed(cmd, 'Driver: docker-container\n', 0 if exists else 1)
            self.assertEqual(cmd[:3], ['docker', 'buildx', 'create'])
            configs.append(Path(cmd[cmd.index('--buildkitd-config') + 1]).read_text())
            self.assertNotIn('--use', cmd)
            self.assertNotIn('--bootstrap', cmd)
            exists = True
            return completed(cmd)

        with patch.object(registry.subprocess, 'run', side_effect=run):
            name = registry._managed_builder(INFO, {HOST: 'http'})
            self.assertEqual(registry._managed_builder(INFO, {HOST: 'http'}), name)
        self.assertTrue(name.startswith('rig-registry-'))
        self.assertEqual(configs, ['[registry."registry.test:5000"]\n  http = true\n'])
        self.assertEqual(sum(c[2] == 'create' for c in calls), 1)

    def test_builder_identity_tracks_daemon_and_policy(self):
        with patch.object(registry.subprocess, 'run', return_value=completed([], 'Driver: docker-container\n')):
            names = {registry._managed_builder(info, settings) for info, settings in (
                (INFO, {HOST: 'http'}), ({**INFO, 'id': 'other-daemon'}, {HOST: 'http'}),
                (INFO, {HOST: 'insecure'}), (INFO, {HOST: 'http', 'extra.test:5000': 'http'}))}
        self.assertEqual(len(names), 4)

    def test_creation_race_reuses_matching_builder(self):
        with patch.object(registry.subprocess, 'run', side_effect=[completed([], code=1),
                completed([], code=1, error='already exists'), completed([], 'Driver: docker-container\n')]):
            self.assertTrue(registry._managed_builder(INFO, {HOST: 'http'}).startswith('rig-registry-'))

    def test_creation_failure_and_wrong_driver_are_errors(self):
        for responses in ([completed([], code=1)] * 3, [completed([], 'Driver: remote\n')]):
            with patch.object(registry.subprocess, 'run', side_effect=responses):
                with self.assertRaises(RigError):
                    registry._managed_builder(INFO, {HOST: 'http'})

    def prepare_with_driver(self, driver, *, overrides=None, endpoints=('default',), info=INFO,
                            insecure=True, context='default'):
        output = f'Name: selected\nDriver: {driver}\nNodes:\n' + ''.join(
            f'Name: node{i}\nEndpoint: {ep}\n' for i, ep in enumerate(endpoints))
        with patch.dict(os.environ, {'BUILDX_BUILDER': '', 'BUILDKIT_HOST': '', 'DOCKER_HOST': '',
                                     'DOCKER_CONTEXT': '',
                                     **(overrides or {})}), \
                patch.object(registry, '_docker_info', return_value=info), \
                patch.object(registry, 'registry_transport', return_value='http'), \
                patch.object(registry, '_managed_builder', return_value='rig-registry-test') as builder, \
                patch.object(registry.subprocess, 'run', side_effect=[completed([], output),
                                                                    completed([], context + '\n')]):
            access = registry.prepare([REF, REF] if insecure else ['docker.io/team/core:v1'],
                                      for_build=True)
        self.assertEqual(access.registry_env, {'BUILDX_BUILDER': 'rig-registry-test'} if insecure else {})
        if access.build_env or access.registry_env:
            builder.assert_called_once_with(info, {HOST: 'http'} if insecure else {})
        else:
            builder.assert_not_called()
        return access

    def test_containerd_daemon_keeps_its_cache_and_build_selection(self):
        for insecure in (True, False):
            with self.subTest(insecure=insecure):
                self.assertEqual(self.prepare_with_driver('docker', insecure=insecure).build_env, {})

    def test_classic_daemon_uses_container_builder_for_attestations_and_multiarch(self):
        for insecure in (True, False):
            for status in (CLASSIC_INFO['driver_status'], [], None):
                with self.subTest(insecure=insecure, status=status):
                    access = self.prepare_with_driver('docker', insecure=insecure,
                                                      info={**INFO, 'driver_status': status})
                    self.assertEqual(access.build_env, {'BUILDX_BUILDER': 'rig-registry-test'})

    def test_classic_daemon_selection_respects_context_and_host(self):
        cases = [({'DOCKER_HOST': 'unix:///docker.sock'}, 'unix:///docker.sock', 'default'),
                 ({'DOCKER_CONTEXT': 'engine', 'DOCKER_HOST': 'tcp://ignored:2376'}, 'engine', 'engine')]
        for overrides, endpoint, context in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.prepare_with_driver('docker', info=CLASSIC_INFO,
                    overrides=overrides, endpoints=(endpoint,), context=context).build_env,
                    {'BUILDX_BUILDER': 'rig-registry-test'})

    def test_classic_daemon_explicit_or_other_endpoint_selection_is_preserved(self):
        for insecure in (True, False):
            for overrides, endpoints in (({'BUILDX_BUILDER': 'chosen'}, ('default',)),
                                         ({'BUILDKIT_HOST': 'tcp://builder.test:1234'}, ('default',)),
                                         ({}, ('other-engine',))):
                with self.subTest(insecure=insecure, overrides=overrides, endpoints=endpoints):
                    self.assertEqual(self.prepare_with_driver('docker', info=CLASSIC_INFO,
                        insecure=insecure, overrides=overrides, endpoints=endpoints).build_env, {})

    def test_local_container_builder_automatically_gets_policy(self):
        self.assertEqual(self.prepare_with_driver('docker-container').build_env,
                         {'BUILDX_BUILDER': 'rig-registry-test'})
        self.assertEqual(self.prepare_with_driver('docker-container', insecure=False).build_env, {})

    def test_explicit_remote_and_multinode_build_choices_are_preserved(self):
        for driver, overrides, endpoints in (
                ('docker-container', {'BUILDX_BUILDER': 'chosen'}, ('default',)),
                ('docker-container', {'BUILDKIT_HOST': 'tcp://builder.test:1234'}, ('default',)),
                ('remote', {}, ('default',)), ('cloud', {}, ('default',)),
                ('docker-container', {}, ('default', 'other')),
                ('docker-container', {}, ('ssh://builder.test',))):
            with self.subTest(driver=driver, overrides=overrides, endpoints=endpoints):
                self.assertEqual(self.prepare_with_driver(driver, overrides=overrides,
                                                         endpoints=endpoints).build_env, {})


class RegistryCallerTests(unittest.TestCase):
    def test_imagetools_uses_scoped_env_preserving_auth_context(self):
        access = registry.Access(registry_env={'BUILDX_BUILDER': 'rig-registry-test'})
        with patch.dict(os.environ, DOCKER_CONFIG='/auth/config', DOCKER_CONTEXT='remote-engine'), \
                patch.object(registry.subprocess, 'run', return_value=completed([], '{}')) as run:
            registry.imagetools(['inspect', '--raw', REF], [REF], access=access)
            env = run.call_args.kwargs['env']
            self.assertEqual(env['BUILDX_BUILDER'], 'rig-registry-test')
            self.assertEqual(env['DOCKER_CONFIG'], '/auth/config')
            self.assertEqual(env['DOCKER_CONTEXT'], 'remote-engine')
            self.assertNotEqual(os.environ.get('BUILDX_BUILDER'), 'rig-registry-test')

    def test_inspection_and_mirror_create_use_same_policy(self):
        access = registry.Access(registry_env={'BUILDX_BUILDER': 'rig-registry-test'})
        with patch.object(registry.subprocess, 'run', return_value=completed([], '{}')) as run:
            self.assertEqual(inspect_raw(REF, access=access), {})
            registry.imagetools(['create', '-t', REF, 'alpine:latest'], [REF, 'alpine:latest'], access=access)
        self.assertTrue(all(c.kwargs['env']['BUILDX_BUILDER'] == 'rig-registry-test'
                            for c in run.call_args_list))

    def test_standalone_tls_failure_retries_only_with_daemon_permission(self):
        for env in ({}, {'BUILDX_BUILDER': 'rig-registry-test'}):
            error = completed([], code=1, error='http: server gave HTTP response to HTTPS client')
            with patch.object(registry.subprocess, 'run', side_effect=[error, completed([], '{}')]) as run, \
                    patch.object(registry, 'prepare', return_value=registry.Access(registry_env=env)):
                if env:
                    self.assertEqual(inspect_raw(REF), {})
                    self.assertEqual(run.call_count, 2)
                else:
                    with self.assertRaisesRegex(RigError, 'HTTP response'):
                        inspect_raw(REF)
                    self.assertEqual(run.call_count, 1)

    def test_auth_errors_are_not_retried_as_http(self):
        with patch.object(registry.subprocess, 'run', return_value=completed([], code=1, error='unauthorized')), \
                patch.object(registry, 'prepare') as prepare:
            with self.assertRaisesRegex(RigError, 'unauthorized'):
                inspect_raw(REF)
            prepare.assert_not_called()

    def test_preflight_bootstraps_build_builder_not_registry_helper(self):
        access = registry.Access(build_env={'BUILDX_BUILDER': 'build'},
                                 registry_env={'BUILDX_BUILDER': 'inspect'})
        with patch.object(registry.subprocess, 'run', return_value=completed([], 'Platforms: linux/arm64')) as run:
            preflight([], HOST, access=access)
        self.assertEqual(run.call_args.kwargs['env']['BUILDX_BUILDER'], 'build')

    def test_bake_digest_fallback_inherits_docker_policy(self):
        from rig_cli.bake import _resolve_digest
        digest = 'sha256:' + 'b' * 64
        with patch.object(registry.subprocess, 'run', side_effect=[completed([], code=1),
                completed([], code=1, error='HTTP response to HTTPS client'), completed([], digest)]), \
                patch.object(registry, 'prepare', return_value=registry.Access(
                    registry_env={'BUILDX_BUILDER': 'rig-registry-test'})):
            self.assertEqual(_resolve_digest(REF), HOST + '/team/core@' + digest)


if __name__ == '__main__':
    unittest.main()
