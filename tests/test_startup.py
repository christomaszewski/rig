"""Concurrent ordinary startup/teardown, using real launchers without Docker or hardware.

Run: python3 tests/test_startup.py. File handshakes prove overlap and barriers;
timeouts only bound failures, rather than asserting machine-dependent timings.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from itertools import product
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rig_cli import RigError, dispatch
from rig_cli.cli import cmd_down
from rig_cli.descriptor import load_descriptor
from rig_cli.manifest import load_manifest


LAUNCHER = r'''
import json, os, pathlib, subprocess, sys, time
import yaml
cfg = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
name = cfg['name']
root = pathlib.Path(cfg['events'])
verb = sys.argv[2]
if verb != 'up':
    (root / (name + '.' + verb)).touch()
if verb not in ('up', 'down'):
    sys.exit(0)
for dependency in cfg.get('after', []):
    assert (root / (dependency + '.finished')).exists(), (name, dependency)
(root / (name + '.pid')).write_text(str(os.getpid()))
with (root / (name + '.launches')).open('a') as launches:
    launches.write(str(os.getpid()) + '\n')
(root / (name + '.started')).write_text(json.dumps({
    'argv': sys.argv[2:], 'project': os.environ.get('COMPOSE_PROJECT_NAME'),
    'posture': os.environ.get('RIG_TARGET_STATE'), 'domain': os.environ.get('ROS_DOMAIN_ID'),
    'cwd': os.getcwd(),
}))
if cfg.get('hold'):
    # A Docker-like grandchild must also be stopped on cancellation.
    code = 'import pathlib, time; pathlib.Path(%r).touch(); time.sleep(60)' % str(root / (name + '.child-ready'))
    child = subprocess.Popen([sys.executable, '-c', code])
    (root / (name + '.child-pid')).write_text(str(child.pid))
    time.sleep(60)
if cfg.get('writer'):
    code = """
import os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 5
while not (root / 'release-writer').exists():
    if time.monotonic() > deadline:
        (root / 'writer-timeout').touch()
        break
    try:
        os.write(1, b'x' * 4096 + b'\\n')
    except BrokenPipeError:
        pass
    (root / 'writer-ready').touch()
    time.sleep(.001)
(root / 'writer-done').touch()
"""
    child = subprocess.Popen([sys.executable, '-c', code, str(root)])
    (root / 'writer-pid').write_text(str(child.pid))
    while not (root / 'writer-ready').exists():
        time.sleep(.01)
if cfg.get('release_writer'):
    (root / 'release-writer').touch()
if cfg.get('output_tail'):
    os.write(1, b'first\n')
    while not (root / 'release-tail').exists():
        time.sleep(.01)
    os.write(1, b'x' * 8192 + b'\n')
deadline = time.monotonic() + 8
for suffix, peers in [('started', cfg.get('peers', [])), ('finished', cfg.get('wait_finished', []))]:
    while not all((root / (p + '.' + suffix)).exists() for p in peers):
        assert time.monotonic() < deadline, (name, 'peers did not overlap', peers)
        time.sleep(.01)
print('stdout-' + name, flush=True)
print('stderr-' + name, file=sys.stderr, flush=True)
print('tail-' + name, end='', flush=True)
(root / (name + '.finished')).touch()
sys.exit(cfg.get('rc', 0))
'''


class ConcurrentStartup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rig-startup-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.repo = self.root / 'service'
        self.repo.mkdir()
        (self.repo / 'rigging.yaml').write_text('service: fake\nlauncher: fake-up\n')
        launcher = self.repo / 'fake-up'
        launcher.write_text('#!' + sys.executable + '\n' + LAUNCHER)
        launcher.chmod(0o755)
        (self.root / 'services.yaml').write_text('services: {fake: {path: service}}\n')
        self.document = dict(vehicle='test', vehicle_id=7,
                             ros=dict(rmw='rmw_fastrtps_cpp'), infra=[], sensors=[], autonomy=[])
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('RIG_') and k != 'VEHICLE_ID'}
        self.env.update(RIG_HOME=str(self.root / 'home'),
                        RIG_VEHICLE_LOCAL=str(self.root / 'absent.yaml'),
                        PATH=str(Path(sys.executable).parent) + ':' + os.environ.get('PATH', ''))
        self.command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'rig'),
                        '--root', str(self.root)]

    def add(self, name, *, tier='sensors', order=10, enabled=True, **fixture):
        config = self.root / (name + '.yaml')
        config.write_text(json.dumps(dict(service='fake', name=name, events=str(self.events), **fixture)))
        self.document[tier].append(dict(name=name, service='fake', config=config.name,
                                        order=order, enabled=enabled))
        (self.root / 'vehicle.yaml').write_text(json.dumps(self.document))

    def run_cli(self, *args):
        return subprocess.run([*self.command, *args], env=self.env, text=True,
                              capture_output=True, timeout=20)

    def wait_for(self, *names):
        deadline = time.monotonic() + 8
        while not all((self.events / name).exists() for name in names):
            self.assertLess(time.monotonic(), deadline, f'missing markers: {names}')
            time.sleep(.01)

    def test_equal_order_overlaps_and_all_order_and_tier_barriers_hold(self):
        self.add('router', tier='infra', order=99)
        self.add('late', order=20, after=['a', 'b'])  # YAML order deliberately disagrees.
        self.add('a', after=['router'], peers=['a', 'b'])
        self.add('b', after=['router'], peers=['a', 'b'])
        self.add('disabled', enabled=False)
        self.add('planner', tier='autonomy', order=10, after=['a', 'b', 'late'])
        result = self.run_cli('up', '--standby')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.events / 'disabled.started').exists())
        for name in ('router', 'a', 'b', 'late', 'planner'):
            self.assertTrue((self.events / (name + '.finished')).exists(), result.stderr)
            state = json.loads((self.events / (name + '.started')).read_text())
            self.assertEqual(state['project'], name + '-vehicle-7')
            self.assertEqual(state['posture'], 'standby')
            self.assertEqual(state['domain'], '7')
            self.assertEqual(state['argv'], ['up', '-d'])
            self.assertEqual(Path(state['cwd']).resolve(), self.repo.resolve())
            for message in ('stdout-', 'stderr-', 'tail-'):
                self.assertIn('[' + name + '] ' + message + name, result.stderr)

    def test_failures_wait_for_peers_continue_later_groups_and_summarize_stably(self):
        self.add('a', peers=['a', 'b'], wait_finished=['b'], rc=3)
        self.add('b', peers=['a', 'b'], rc=7)
        self.add('later', order=20, after=['a', 'b'])
        result = self.run_cli('up')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.events / 'later.finished').exists())
        self.assertIn('2/3 failed: a, b', result.stderr)

    def test_down_overlaps_peers_and_reverses_order_and_tier_barriers(self):
        self.add('router', tier='infra', order=99, after=['a', 'b'])
        self.add('a', after=['late'], peers=['a', 'b'])
        self.add('b', after=['late'], peers=['a', 'b'])
        self.add('late', order=20, after=['planner'])
        self.add('planner', tier='autonomy', order=10)
        self.add('disabled', enabled=False)
        result = self.run_cli('down')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('planner', 'late', 'a', 'b', 'router'):
            self.assertTrue((self.events / (name + '.finished')).exists(), result.stderr)
            state = json.loads((self.events / (name + '.started')).read_text())
            self.assertEqual(state['argv'], ['down'])
            self.assertEqual(state['project'], name + '-vehicle-7')
        self.assertFalse((self.events / 'disabled.started').exists())

    def test_down_failure_waits_for_peers_continues_teardown_and_aggregates_errors(self):
        self.add('a', peers=['a', 'b'], wait_finished=['b'], rc=3)
        self.add('b', peers=['a', 'b'], rc=7)
        self.add('router', tier='infra', after=['a', 'b'])
        result = self.run_cli('down')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.events / 'router.finished').exists())
        self.assertIn('2/3 failed: b, a', result.stderr)

    def test_down_explicit_names_deduplicate_and_include_disabled(self):
        self.add('a', peers=['a', 'b'])
        self.add('b', enabled=False, peers=['a', 'b'])
        self.add('unselected')
        result = self.run_cli('down', 'a', 'b', 'a')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('a', 'b'):
            self.assertEqual(len((self.events / (name + '.launches')).read_text().splitlines()), 1)
        self.assertFalse((self.events / 'unselected.started').exists())

    def test_down_dry_run_shows_reverse_groups_without_launching(self):
        self.add('router', tier='infra', order=99)
        self.add('a')
        self.add('b')
        self.add('planner', tier='autonomy', order=0)
        result = self.run_cli('down', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('concurrent sensor order 10: b, a', result.stderr)
        positions = [result.stderr.index(n + ' [fake]') for n in ('planner', 'b', 'a', 'router')]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(list(self.events.iterdir()), [])

    def test_down_captures_logs_before_teardown_and_purges_before_successful_seal(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                names = ['fa', 'fb'] if failure else ['a', 'b']
                self.document['sensors'] = []
                for name in names:
                    self.add(name, peers=names, rc=5 if failure else 0)
                manifest = load_manifest(self.root)
                manifest.data_dir = str(self.root / 'data')
                descriptor = load_descriptor('fake', self.repo)
                args = SimpleNamespace(names=[], end_run=True, purge=True, dry_run=False, rig_root=self.root)
                events = []

                def capture(*args, **kwargs):
                    self.assertFalse(any((self.events / (name + '.started')).exists() for name in names))
                    events.append('capture')

                def purge(sensor, *args, **kwargs):
                    self.assertTrue(all((self.events / (name + '.finished')).exists() for name in names))
                    events.append('purge:' + sensor.name)

                def seal(*args, **kwargs):
                    events.append('seal')

                with contextlib.redirect_stderr(io.StringIO()), \
                        patch('rig_cli.runs.capture_docker_logs', side_effect=capture), \
                        patch('rig_cli.dispatch.purge_external_volumes', side_effect=purge), \
                        patch('rig_cli.runs.end_run', side_effect=seal):
                    rc = cmd_down(args, manifest, {}, {'fake': descriptor})
                self.assertEqual(rc, int(failure))
                self.assertEqual(events, ['capture', 'purge:' + names[1], 'purge:' + names[0]]
                                 + ([] if failure else ['seal']))

    def test_explicit_selection_includes_disabled_but_not_unselected_peers(self):
        self.add('a', peers=['a', 'b'])
        self.add('b', enabled=False, peers=['a', 'b'])
        self.add('unselected')
        result = self.run_cli('up', 'b', 'a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.events / 'b.finished').exists())
        self.assertFalse((self.events / 'unselected.started').exists())

    def test_dry_run_is_stable_shows_group_and_launches_nothing(self):
        self.add('b')
        self.add('a')
        result = self.run_cli('up', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('concurrent sensor order 10: b, a', result.stderr)
        self.assertLess(result.stderr.index('b [fake]'), result.stderr.index('a [fake]'))
        self.assertEqual(list(self.events.iterdir()), [])

    def test_repeated_explicit_names_launch_each_instance_only_once(self):
        self.add('a', peers=['a', 'b'])
        self.add('b', peers=['a', 'b'])
        result = self.run_cli('up', 'a', 'a', 'b')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('a', 'b'):
            self.assertEqual(len((self.events / (name + '.launches')).read_text().splitlines()), 1)

    def test_detached_descendant_output_does_not_hold_next_group(self):
        self.add('a', writer=True)
        self.add('b', order=20, after=['a'], release_writer=True)
        try:
            result = self.run_cli('up')
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.wait_for('writer-done')
            self.assertFalse((self.events / 'writer-timeout').exists())
            self.assertTrue((self.events / 'b.finished').exists())
        finally:
            if (self.events / 'writer-pid').exists():
                try:
                    os.kill(int((self.events / 'writer-pid').read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_exit_between_read_and_poll_preserves_queued_final_diagnostics(self):
        self.add('a', output_tail=True)
        manifest = load_manifest(self.root)
        desc = load_descriptor('fake', self.repo)
        real_popen, real_read = subprocess.Popen, os.read
        children = []
        raced = False

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        def read(fd, size):
            nonlocal raced
            data = real_read(fd, size)
            if data and children and fd == children[0].stdout.fileno() and not raced:
                raced = True
                (self.events / 'release-tail').touch()
                # Simulate descheduling between read() and poll(): the launcher
                # writes its remaining diagnostics and exits before we resume.
                children[0].wait(timeout=5)
            return data

        output = io.StringIO()
        with contextlib.redirect_stderr(output), \
                patch.object(dispatch.subprocess, 'Popen', side_effect=spawn), \
                patch.object(os, 'read', side_effect=read):
            outcomes = dispatch.run_lifecycle([(manifest.sensors[0], desc)], self.env, 'up')
        self.assertEqual(outcomes[0].returncode, 0)
        self.assertIn('x' * 8192, output.getvalue())
        self.assertIn('[a] tail-a', output.getvalue())

    def test_other_verbs_keep_serial_dispatch(self):
        self.add('a')
        self.add('b')
        manifest = load_manifest(self.root)
        desc = load_descriptor('fake', self.repo)
        # Replay/simulation and non-startup verbs retain run_verb's interface.
        pairs = [(s, desc) for s in manifest.select([], enabled_only=True)]
        with contextlib.redirect_stderr(io.StringIO()):
            outcomes = dispatch.run_verb(pairs, self.env, 'pull')
        self.assertEqual([o.sensor.name for o in outcomes], ['a', 'b'])
        self.assertTrue((self.events / 'a.pull').exists())
        self.assertTrue((self.events / 'b.pull').exists())

    def test_spawn_error_cleans_up_already_started_peers(self):
        self.add('a', hold=True)
        self.add('b')
        manifest = load_manifest(self.root)
        desc = load_descriptor('fake', self.repo)
        pairs = [(s, desc) for s in manifest.select([], enabled_only=True)]
        real_popen = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            if children:
                raise OSError('fixture spawn failure')
            child = real_popen(*args, **kwargs)
            children.append(child)
            self.wait_for('a.child-ready')
            return child

        with contextlib.redirect_stderr(io.StringIO()), \
                patch.object(dispatch.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(RigError, 'b: cannot start launcher: fixture spawn failure'):
                dispatch.run_lifecycle(pairs, self.env, 'up')
        self.assertIsNotNone(children[0].poll())
        pid = int((self.events / 'a.child-pid').read_text())
        deadline = time.monotonic() + 3
        while self.running(pid) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.running(pid))
        self.assertFalse((self.events / 'b.started').exists())

    def test_interrupt_stops_every_launcher_and_grandchild_and_skips_later_groups(self):
        for verb, sig in product(('up', 'down'), (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)):
            with self.subTest(verb=verb, signal=sig):
                prefix = verb + str(int(sig))
                names = [prefix + 'a', prefix + 'b']
                self.document['sensors'] = []
                for name in names:
                    self.add(name, hold=True)
                self.add(prefix + 'later', order=20 if verb == 'up' else 0)
                process = subprocess.Popen([*self.command, verb], env=self.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                pids = []
                try:
                    self.wait_for(*(name + '.child-ready' for name in names))
                    pids = [int((self.events / (name + suffix)).read_text())
                            for name in names for suffix in ('.pid', '.child-pid')]
                    process.send_signal(sig)
                    _, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 128 + sig, stderr)
                    self.assertFalse((self.events / (prefix + 'later.started')).exists())
                    deadline = time.monotonic() + 3
                    while any(self.running(pid) for pid in pids) and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertFalse([pid for pid in pids if self.running(pid)])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    process.stdout.close()
                    process.stderr.close()
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @staticmethod
    def running(pid):
        # Linux's orphaned grandchildren can remain zombies until init reaps them.
        try:
            if Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                return False
        except FileNotFoundError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


if __name__ == '__main__':
    unittest.main()
