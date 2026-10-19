"""Build output, persistent diagnostics, and CLI progress/concurrency controls."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, cli, docker_registry
from rig_cli.build_progress import BuildProgress


class ProgressTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stderr(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_quiet_success_logs_both_streams_and_counts_mirrors(self):
        with BuildProgress(['build camera/dev', 'mirror helper'], root=self.root) as progress:
            with progress.step(0) as step:
                step.run([sys.executable, '-c',
                          'import sys; print("docker stdout"); print("docker stderr", file=sys.stderr)'])
                log = step.log
                self.assertNotIn('DONE', self.output.getvalue())
            with progress.step(1):
                pass
        output = self.output.getvalue()
        self.assertNotIn('docker stdout', output)
        self.assertNotIn('docker stderr', output)
        self.assertIn('docker stdout', log.read_text())
        self.assertIn('docker stderr', log.read_text())
        self.assertIn('[1/2] START build camera/dev; 0/2 complete, 2 remaining', output)
        self.assertIn('1/2 complete, 1 remaining', output)
        self.assertIn('2/2 complete, 0 remaining', output)
        self.assertIn('rig build: finished in', output)
        self.assertEqual(log.parent.parent, self.root / 'var' / 'build')

    def test_verbose_output_streams_before_command_exits_and_is_saved(self):
        marker = self.root / 'output-seen'
        with BuildProgress(['build camera/dev'], root=self.root, verbose=True) as progress:
            write = progress.write

            def observe(message):
                write(message)
                if 'live stdout' in message:
                    marker.touch()

            progress.write = observe
            with progress.step(0) as step:
                # The child cannot succeed until its first line reaches the console.
                step.run([sys.executable, '-c', '''
import pathlib, sys, time
print('live stdout', flush=True)
marker = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 5
while not marker.exists() and time.monotonic() < deadline:
    time.sleep(.01)
assert marker.exists(), 'output was buffered until exit'
print('live stderr', file=sys.stderr)
''', str(marker)])
        self.assertIn('[1] live stdout', self.output.getvalue())
        self.assertIn('[1] live stderr', self.output.getvalue())
        self.assertIn('live stdout\nlive stderr', step.log.read_text())

    def test_failure_shows_bounded_tail_and_keeps_full_log(self):
        with self.assertRaisesRegex(RigError, 'exit code 7'):
            with BuildProgress(['build bad', 'build pending'], root=self.root) as progress:
                with progress.step(0) as step:
                    step.run([sys.executable, '-c',
                              'import sys; print("early detail"); '
                              'print("\\n".join("detail " + str(i) for i in range(100))); '
                              'print("actionable failure"); sys.exit(7)'])
        output = self.output.getvalue()
        self.assertNotIn('early detail', output)
        self.assertIn('early detail', step.log.read_text())
        self.assertIn('actionable failure', output)
        self.assertIn('0/2 complete, 1 failed, 1 remaining', output)
        self.assertIn('Full log: ' + str(step.log), output)
        self.assertIn('rig build: failed in', output)
        self.assertNotIn('DONE', output)

    def test_heartbeat_includes_verification_and_stops_with_context(self):
        running, verifying = threading.Event(), threading.Event()
        with BuildProgress(['build slow'], root=self.root, interval=.01) as progress:
            write = progress.write

            def observe(message):
                write(message)
                if '] RUNNING ' in message:
                    running.set()
                if '] VERIFYING ' in message:
                    verifying.set()

            progress.write = observe
            with progress.step(0) as step:
                self.assertTrue(running.wait(2))
                step.verifying()
                self.assertTrue(verifying.wait(2))
                self.assertNotIn('DONE', self.output.getvalue())
        self.assertIn('elapsed); 0/1 complete, 1 remaining', self.output.getvalue())
        self.assertFalse(progress.thread.is_alive())

    def test_unique_session_logs_do_not_overwrite(self):
        paths = []
        for _ in range(2):
            with BuildProgress(['build one'], root=self.root) as progress:
                with progress.step(0) as step:
                    paths.append(step.log)
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(path.is_file() for path in paths))

    def test_cli_defaults_and_overrides_reach_build(self):
        for argv, jobs, verbose in (([], 2, False), (['-j', '1', '-v'], 1, True),
                                    (['--jobs', '4', '--verbose'], 4, True)):
            args = cli.build_parser().parse_args(['build', *argv])
            with patch.object(cli, '_load', return_value=(SimpleNamespace(missing_identity=[]), {}, {})), \
                    patch.object(cli.build_mod, 'build', return_value=0) as build:
                self.assertEqual(cli.cmd_build(args, self.root), 0)
            self.assertEqual(build.call_args.kwargs['jobs'], jobs)
            self.assertEqual(build.call_args.kwargs['verbose'], verbose)

    def test_mirror_output_uses_progress_runner_with_registry_policy(self):
        access = docker_registry.Access(registry_env={'BUILDX_BUILDER': 'registry-helper'})
        with BuildProgress(['mirror helper'], root=self.root) as progress:
            with progress.step(0) as step:
                with patch.object(step, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                    docker_registry.imagetools(['create', '-t', 'reg/helper:v1', 'helper:v1'],
                                               ['helper:v1', 'reg/helper:v1'], access=access, runner=step.run)
                self.assertEqual(run.call_args.args[0][:3], ['docker', 'buildx', 'imagetools'])
                self.assertEqual(run.call_args.kwargs['env']['BUILDX_BUILDER'], 'registry-helper')


if __name__ == '__main__':
    unittest.main()
