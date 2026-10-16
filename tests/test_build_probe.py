"""Mutant build scripts must fail certification; unavailable containment must never pass."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import build_probe as probe
from rig_cli.certify import Check, run_targets
from rig_cli.descriptor import load_descriptor


class BuildProbe(unittest.TestCase):
    def test_real_sandbox_denies_host_unix_socket_and_writes(self):
        with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as outside:
            tmp = Path(stage).resolve()
            sock_path = str(Path(outside).resolve() / 'daemon.sock')
            with socket.socket(socket.AF_UNIX) as server:
                try:
                    server.bind(sock_path)
                    server.listen()
                except PermissionError:
                    self.skipTest('outer environment cannot bind the test socket')
                code = '''import errno, socket, sys
from pathlib import Path
try:
    s = socket.socket(socket.AF_UNIX)
    s.connect(sys.argv[1])
except OSError as e:
    assert e.errno in (errno.EPERM, errno.EACCES), e
else:
    raise SystemExit('host Unix socket was reachable')
try:
    Path(sys.argv[2]).write_text('escaped')
except OSError as e:
    assert e.errno in (errno.EPERM, errno.EACCES, errno.EROFS), e
else:
    raise SystemExit('write outside staging succeeded')
'''
                cmd = probe.sandbox_command(tmp, tmp, [sys.executable, '-c', code,
                                                       sock_path, str(Path(outside)/'sentinel')])
                if cmd is None:
                    self.skipTest('supported containment unavailable')
                result = probe.run_probe(cmd, cwd=tmp, env=dict(os.environ))
                if 'sandbox_apply:' in result.stderr or 'bwrap:' in result.stderr:
                    self.skipTest(result.stderr.strip())
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_command_boundary_mutants(self):
        good = [['build', '--no-cache', '-t', 'a', '.'],
                ['buildx', 'build', '--no-cache=true', '--tag=b', '.']]
        self.assertEqual(probe.check_commands(good, {'a', 'b'}, True), [])
        for commands in ([], good[:1], [good[0], ['build', '-t', 'b', '.']]):
            self.assertTrue(probe.check_commands(commands, {'a', 'b'}, True))
        self.assertTrue(probe.check_commands(good, {'a', 'b'}, False))
        self.assertTrue(probe.check_commands(good, {'a', 'b'}, True, 'linux/arm64,linux/amd64'))

    def test_script_matrix_capture_uses_clean_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'rigging.yaml').write_text('''service: fake
launcher: up
build:
  command: build.sh
  images: [a, b]
  architectures: [arm64, amd64]
  capabilities: {no_cache: true, platforms: true}
''')
            script = root / 'build.sh'
            shutil.copy2(Path(__file__).resolve().parents[1] / 'packaging/rig-build.sh', root / 'helper.sh')
            script.write_text('''#!/bin/sh
set -eu
. ./helper.sh
rig_build "$1/a:$2" .
rig_build "$1/b:$2" .
''')
            script.chmod(0o755)
            desc = load_descriptor('fake', root)
            # Only our known fixture bypasses containment; production sandbox invocation is tested separately.
            with patch.object(probe, 'sandbox_command', side_effect=lambda tmp, cwd, cmd: cmd), \
                    patch.dict(os.environ, IMAGES='a', PUSH='0', RIG_BUILD_NO_CACHE='1'):
                check = probe.certify_build(desc)[0]
                self.assertEqual(check.level, 'OK', check.detail)
                script.write_text(script.read_text().replace('rig_build "$1/b:$2" .', 'docker build -t "$1/b:$2" .'))
                check = probe.certify_build(desc)[0]
                self.assertEqual(check.level, 'ERROR')
                self.assertIn('missing --no-cache', check.detail)
                self.assertIn('certify.invalid/b:certify', check.detail)
            with patch.object(probe, 'sandbox_command', return_value=None):
                check = probe.certify_build(desc)[0]
                self.assertEqual(check.level, 'WARN')
                self.assertIn('NOT CHECKED', check.detail)
            script.unlink()
            self.assertIn('context absent', probe.certify_build(desc)[0].detail)

    def test_timeout_is_error_and_ci_requires_completed_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'rigging.yaml').write_text('service: fake\nlauncher: up\nbuild: {command: b.sh, images: [a]}\n')
            (root / 'b.sh').write_text('#!/bin/sh\nexit 0\n')
            desc = load_descriptor('fake', root)
            with patch.object(probe, 'sandbox_command', return_value=['probe']), \
                    patch.object(probe, 'run_probe', side_effect=subprocess.TimeoutExpired(['probe'], 30)):
                self.assertEqual(probe.certify_build(desc)[0].level, 'ERROR')
            with patch('rig_cli.certify.certify_target', side_effect=lambda *a, **k:
                       [Check('WARN', 'build-no-cache', 'NOT CHECKED')]):
                self.assertEqual(run_targets([('fake', desc, root/'c.yaml')], {}, require_build_check=True), 1)
                self.assertEqual(run_targets([('fake', desc, root/'c.yaml')], {}), 0)


if __name__ == '__main__':
    unittest.main()
