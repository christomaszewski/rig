"""Baked lifecycle waves and cancellation, using real shell processes and fake Docker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rig_cli import artifact_runtime, bake
from rig_cli.descriptor import load_descriptor
from rig_cli.manifest import Manifest, RosSettings, Sensor


DOCKER = r'''
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
def event(kind, name):
    fd = os.open(root / 'events', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, (json.dumps([kind, name]) + '\n').encode())
    os.close(fd)
if args[0] == 'descendant':
    name, level = args[1], int(args[2])
    (root / (name + '.' + str(level) + '.pid')).write_text(str(os.getpid()))
    if level:
        subprocess.Popen([sys.executable, __file__, 'descendant', name, str(level - 1)])
    time.sleep(30)
    sys.exit(0)
if args[0] == 'info':
    event('guard', 'target')
    print('linux/arm64')
elif args[:2] == ['image', 'inspect']:
    event('guard', 'image')
    print('sha256:' + 'a' * 64)
elif args[:2] == ['volume', 'create']:
    event('volume', args[2])
    if args[2] == 'broken':
        sys.exit(7)
elif args[0] == 'guard':
    event('guard', args[1])
elif args[0] == 'compose':
    name = args[args.index('-p') + 1]
    spec = json.loads((root / 'spec.json').read_text()).get(name, {})
    if spec.get('verb'):
        assert args[-1] == spec['verb'], args
    for other in spec.get('after', []):
        assert (root / (other + '.done')).exists(), (name, 'started before', other)
    if spec.get('run'):
        assert pathlib.Path(spec['run']).is_dir(), 'run guard has not completed'
    if spec.get('offline'):
        assert args[-2:] == ['--pull', 'never'], args
    if spec.get('alias'):
        assert spec['alias'] in pathlib.Path(args[args.index('-f') + 1]).read_text()
    (root / (name + '.pid')).write_text(str(os.getpid()))
    event('start', name)
    (root / (name + '.started')).touch()
    if spec.get('tree'):
        subprocess.Popen([sys.executable, __file__, 'descendant', name, '1'])
        time.sleep(30)
    limit = time.monotonic() + 5
    for peer in spec.get('peers', []):
        while not (root / (peer + '.started')).exists():
            assert time.monotonic() < limit, (name, 'did not overlap', peer)
            time.sleep(.01)
    time.sleep(spec.get('delay', 0))
    (root / (name + '.done')).touch()
    event('done', name)
    sys.exit(spec.get('fail', 0))
else:
    raise AssertionError(args)
'''


class BakedConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.executable(self.bin / 'docker', '#!' + sys.executable + '\n' + DOCKER)
        # The generated deployment scripts must work without Python on PATH.
        self.executable(self.bin / 'python3', '#!/bin/sh\nexit 1\n')
        self.env = dict(os.environ, PATH=str(self.bin) + ':/usr/bin:/bin', FAKE_STATE=str(self.state))

    @staticmethod
    def executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def entry(self, name, tier='sensor', order=10):
        return dict(sensor=name, project=name, compose=f'compose/{name}/docker-compose.yaml',
                    external_volumes=[], tier=tier, order=order)

    def spec(self, **entries):
        (self.state / 'spec.json').write_text(json.dumps(entries))

    def events(self):
        path = self.state / 'events'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_script(self, name='up.sh', *args):
        result = subprocess.run([str(self.root / name), *args], env=self.env, capture_output=True,
                                text=True, timeout=12)
        return result

    def test_bootstrap_overlaps_peers_and_respects_order_and_tier_barriers(self):
        entries = [self.entry('router', 'infra', 10), self.entry('a'), self.entry('b'),
                   self.entry('later', order=20), self.entry('planner', 'autonomy', 10)]
        self.spec(router={}, a={'peers': ['b'], 'after': ['router']},
                  b={'peers': ['a'], 'after': ['router']},
                  later={'after': ['a', 'b']}, planner={'after': ['later']})
        bake._write_scripts(self.root, entries)
        bake._write_bootstrap(self.root)
        result = self.run_script('run.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        for name in ('a', 'b'):
            other = 'b' if name == 'a' else 'a'
            self.assertLess(events.index(['start', name]), events.index(['done', other]))
        self.assertEqual(events[-1], ['done', 'planner'])

    def test_legacy_entries_remain_serial(self):
        entries = [self.entry('a'), self.entry('b')]
        for entry in entries:
            entry.pop('tier')
            entry.pop('order')
        self.spec(a={}, b={'after': ['a']})
        bake._write_scripts(self.root, entries)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [['start', 'a'], ['done', 'a'], ['start', 'b'], ['done', 'b']])

    def test_down_bootstrap_overlaps_peers_and_reverses_order_and_tier_barriers(self):
        entries = [self.entry('router', 'infra', 99), self.entry('a'), self.entry('b'),
                   self.entry('later', order=20), self.entry('planner', 'autonomy', 1)]
        for entry in entries:
            entry['external_volumes'] = ['broken']  # teardown must not create them
        self.spec(planner={'verb': 'down'}, later={'verb': 'down', 'after': ['planner']},
                  a={'verb': 'down', 'peers': ['b'], 'after': ['later']},
                  b={'verb': 'down', 'peers': ['a'], 'after': ['later']},
                  router={'verb': 'down', 'after': ['a', 'b']})
        bake._write_scripts(self.root, entries, target={'arch': 'arm64'},
                            bundle_ids={'image:v1': 'sha256:' + 'a' * 64})
        bake._write_bootstrap(self.root)
        result = self.run_script('run.sh', 'down')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        for name, other in (('a', 'b'), ('b', 'a')):
            self.assertLess(events.index(['start', name]), events.index(['done', other]))
        self.assertEqual(events[0], ['start', 'planner'])
        self.assertEqual(events[-1], ['done', 'router'])
        self.assertFalse(any(event[0] in ('volume', 'guard') for event in events))

    def test_down_failure_drains_peers_and_continues_teardown_with_nonzero_result(self):
        self.spec(a={'verb': 'down', 'peers': ['b'], 'fail': 9},
                  b={'verb': 'down', 'peers': ['a'], 'delay': .15},
                  router={'verb': 'down', 'after': ['a', 'b']})
        bake._write_scripts(self.root, [self.entry('router', 'infra'), self.entry('a'), self.entry('b')])
        result = self.run_script('down.sh')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('rig down: a failed (exit 9)', result.stderr)
        self.assertTrue((self.state / 'b.done').exists())
        self.assertEqual(self.events()[-1], ['done', 'router'])

    def test_down_legacy_entries_remain_serial_in_reverse(self):
        entries = [self.entry('a'), self.entry('b')]
        for entry in entries:
            entry.pop('tier')
            entry.pop('order')
        self.spec(b={'verb': 'down'}, a={'verb': 'down', 'after': ['b']})
        bake._write_scripts(self.root, entries)
        result = self.run_script('down.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [['start', 'b'], ['done', 'b'], ['start', 'a'], ['done', 'a']])

    def test_failure_drains_entire_group_and_does_not_start_next_group(self):
        self.spec(a={'peers': ['b'], 'fail': 9}, b={'peers': ['a'], 'delay': .15}, later={})
        bake._write_scripts(self.root, [self.entry('a'), self.entry('b'), self.entry('later', order=20)])
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('a failed (exit 9)', result.stderr)
        self.assertTrue((self.state / 'b.done').exists())
        self.assertFalse((self.state / 'later.started').exists())

    def test_volume_failure_is_attributed_and_other_group_members_finish(self):
        entries = [self.entry('broken_sensor'), self.entry('b'), self.entry('later', order=20)]
        entries[0]['external_volumes'] = ['broken']
        self.spec(b={})
        bake._write_scripts(self.root, entries)
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('broken_sensor failed (exit 7)', result.stderr)
        self.assertTrue((self.state / 'b.done').exists())
        self.assertFalse((self.state / 'broken_sensor.started').exists())
        self.assertFalse((self.state / 'later.started').exists())

    def test_target_run_and_image_guards_finish_before_offline_wave(self):
        data = self.root / 'data'
        common = {'run': str(data / 'current'), 'offline': True}
        self.spec(a={**common, 'peers': ['b']}, b={**common, 'peers': ['a']})
        bake._write_scripts(self.root, [self.entry('a'), self.entry('b')],
                            target={'arch': 'arm64'}, bundle_ids={'image:v1': 'sha256:' + 'a' * 64},
                            run_ctx={'data_dir': str(data), 'vehicle': 'test', 'vehicle_id': 7, 'tag': 'test'})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        first_start = next(i for i, e in enumerate(events) if e[0] == 'start')
        self.assertLess(events.index(['guard', 'target']), first_start)
        self.assertLess(events.index(['guard', 'image']), first_start)

    def bundle_fixture(self):
        repo = self.root / 'services' / 'demo'
        repo.mkdir(parents=True)
        (repo / 'rigging.yaml').write_text('service: demo\nlauncher: launch\n')
        self.executable(repo / 'launch', '#!/bin/sh\nprintf "services: {app: {image: old}}\\n"\n')
        rows = []
        for name in ('a', 'b'):
            config = self.root / (name + '.yaml')
            config.write_text(f'service: demo\nname: {name}\n')
            rows.append(Sensor(name, 'demo', config, True, 10))
        manifest = Manifest(vehicle='test', ros=RosSettings(0, 'rmw_zenoh_cpp', None), sensors=rows)
        return manifest, {'demo': load_descriptor('demo', repo)}

    def test_bundle_runtime_carries_group_metadata_through_render_and_starts_peers(self):
        manifest, descriptors = self.bundle_fixture()
        alias = 'rig-bundle/fake:content'
        (self.root / 'metadata.yaml').write_text('bundle:\n  refs: {old: ' + alias + '}\n')
        self.executable(self.root / 'ensure-images.sh', '#!/bin/sh\ndocker guard ensured\n')
        self.spec(a={'peers': ['b'], 'alias': alias}, b={'peers': ['a'], 'alias': alias})
        with patch.dict(os.environ, self.env), patch('rig_cli.bake._resolve_digest', return_value=None):
            result = artifact_runtime.run_bundle(self.root, manifest, descriptors, self.env)
        self.assertEqual(result, 0)
        self.assertEqual(self.events()[0], ['guard', 'ensured'])
        self.assertTrue((self.state / 'a.done').exists())
        self.assertTrue((self.state / 'b.done').exists())

    def assert_staged_cli_startup(self, image_bundle):
        """Exercise the shipped shim/package and CLI routing, outside this checkout."""
        bake._stage_rig(self.root)
        repo = self.root / 'services' / 'demo'
        repo.mkdir(parents=True)
        (repo / 'rigging.yaml').write_text('service: demo\nlauncher: launch\n')
        self.executable(repo / 'launch', '#!' + sys.executable + '\n' + '''
import os, pathlib, sys, yaml
config = pathlib.Path(sys.argv[1])
row = yaml.safe_load(config.read_text())
if sys.argv[2] == 'config':
    print('services: {app: {image: old}}')
elif sys.argv[2] == 'up':
    os.execvp('docker', ['docker', 'compose', '-p', os.environ['COMPOSE_PROJECT_NAME'],
                        '-f', str(config), 'up', '-d'])
else:
    raise AssertionError(sys.argv)
''')
        (self.root / 'services.yaml').write_text('services: {demo: {path: services/demo}}\n')
        (self.root / 'vehicle.yaml').write_text('''vehicle: staged-test
ros: {domain_id: 7, rmw: rmw_cyclonedds_cpp}
infra:
  - {name: router, service: demo, config: router.yaml, order: 10}
sensors:
  - {name: later, service: demo, config: later.yaml, order: 20}
  - {name: b, service: demo, config: b.yaml, order: 10}
  - {name: a, service: demo, config: a.yaml, order: 10}
  - {name: disabled, service: demo, config: disabled.yaml, order: 10, enabled: false}
autonomy:
  - {name: planner, service: demo, config: planner.yaml, order: 1}
''')
        for name in ('router', 'a', 'b', 'later', 'planner', 'disabled'):
            (self.root / (name + '.yaml')).write_text(f'service: demo\nname: {name}\n')
        alias = 'rig-bundle/fake:content'
        required = {'alias': alias} if image_bundle else {}
        self.spec(router=required,
                  a={**required, 'peers': ['b'], 'after': ['router']},
                  b={**required, 'peers': ['a'], 'after': ['router']},
                  later={**required, 'after': ['a', 'b']},
                  planner={**required, 'after': ['later']})
        if image_bundle:
            (self.root / 'metadata.yaml').write_text('bundle:\n  refs: {old: ' + alias + '}\n')
            self.executable(self.root / 'ensure-images.sh', '#!/bin/sh\ndocker guard ensured\n')
        else:
            (self.root / 'metadata.yaml').write_text('tag: staged-test\n')
        env = {key: value for key, value in self.env.items()
               if key not in ('RIG_VEHICLE_ID', 'RIG_VEHICLE_NAME', 'PYTHONPATH')
               and not key.startswith('RIG_VAR_')}
        env.update(RIG_VEHICLE_LOCAL=str(self.root / 'missing-machine.yaml'),
                   RIG_HOME=str(self.root / 'user'))
        result = subprocess.run([sys.executable, str(self.root / 'rig'), '--root', str(self.root), 'up'],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        for name, other in (('a', 'b'), ('b', 'a')):
            self.assertLess(events.index(['start', name]), events.index(['done', other]))
        self.assertEqual(events[-1], ['done', 'planner'])
        self.assertFalse((self.state / 'disabled.started').exists())
        if image_bundle:
            self.assertLess(events.index(['guard', 'ensured']), events.index(['start', 'router']))
            generated = self.root / 'var' / 'bundle-runtime' / 'up.sh'
            self.assertTrue(generated.is_file())
        else:
            self.assertFalse((self.root / 'var' / 'bundle-runtime').exists())

    def test_staged_rig_cli_uses_concurrent_native_launchers(self):
        self.assert_staged_cli_startup(image_bundle=False)

    def test_staged_rig_cli_uses_concurrent_image_bundle_scripts(self):
        self.assert_staged_cli_startup(image_bundle=True)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            self.assertLess(time.monotonic(), deadline, 'timed out waiting for process fixture')
            time.sleep(.02)

    @staticmethod
    def alive(pid):
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
        return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith('Z')

    def assert_cancellation(self, sig, wrapper=False, verb='up'):
        try:
            inspected = subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'pid='], capture_output=True)
        except OSError:
            self.skipTest('process inspection unavailable; run outside the restricted process sandbox')
        if inspected.returncode:
            self.skipTest('process inspection unavailable; run outside the restricted process sandbox')
        self.spec(a={'tree': True}, b={'tree': True}, later={})
        entries = [self.entry('a'), self.entry('b'), self.entry('later', order=20)]
        if verb == 'down':
            entries = [self.entry('later', order=0), self.entry('a'), self.entry('b')]
        bake._write_scripts(self.root, entries)
        command = [str(self.root / (verb + '.sh'))]
        if wrapper:
            command = [sys.executable, '-c',
                       'from pathlib import Path; import sys; '
                       'from rig_cli.artifact_runtime import _run_startup_script; '
                       'sys.exit(_run_startup_script(Path(sys.argv[1])))', str(self.root / 'up.sh')]
        process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.wait_for(lambda: len(list(self.state.glob('*.pid'))) == 6)
            pids = [int(path.read_text()) for path in self.state.glob('*.pid')]
            process.send_signal(sig)
            _, err = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 128 + sig, err)
            self.wait_for(lambda: not any(self.alive(pid) for pid in pids))
            self.assertFalse((self.state / 'later.started').exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            for path in self.state.glob('*.pid'):
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=3)

    def test_sigint_stops_all_shell_children_and_grandchildren(self):
        self.assert_cancellation(signal.SIGINT)

    def test_sigterm_stops_all_shell_children_and_grandchildren(self):
        self.assert_cancellation(signal.SIGTERM)

    def test_down_sigint_stops_all_shell_children_and_grandchildren(self):
        self.assert_cancellation(signal.SIGINT, verb='down')

    def test_down_sigterm_stops_all_shell_children_and_grandchildren(self):
        self.assert_cancellation(signal.SIGTERM, verb='down')

    def test_bundle_python_wrapper_forwards_interrupt_and_waits_for_cleanup(self):
        self.assert_cancellation(signal.SIGINT, wrapper=True)

    def test_bundle_python_wrapper_forwards_termination_and_waits_for_cleanup(self):
        self.assert_cancellation(signal.SIGTERM, wrapper=True)


if __name__ == '__main__':
    unittest.main()
