"""Artifact publication, overwrite confirmation, inspection and bounded file cleanup."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, artifacts, cli
from rig_cli.completions import candidates


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'deployment'
        self.root.mkdir()
        (self.root / 'vehicle.yaml').write_text('vehicle: demo\nimages: {tag: v1}\nsensors: []\n')
        (self.root / 'services.yaml').write_text('services: {}\n')

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(['--root', str(self.root), *args])
        return result, out.getvalue(), err.getvalue()

    def artifact(self, tag='v1', timestamp=1):
        locations = artifacts.paths(self.root, tag, include_unbaked=True)
        staging = locations['staging']
        staging.mkdir(parents=True)
        meta = dict(tag=tag, vehicle='demo', target={'runtime': 'dev', 'arch': 'amd64'},
                    sources={'camera': {'ref': 'release'}}, bundle={'image_ids': {'alias:v1': 'sha256:abc'}})
        (staging / 'metadata.yaml').write_text(yaml.safe_dump(meta))
        (staging / 'rig.lock').write_text(yaml.safe_dump({'build_catalog': {'reg/image:v1': {'digest': 'sha256:def'}}}))
        (staging / 'generated').write_text('staging data')
        artifacts.publish(staging, self.root, tag)
        os.utime(locations['archive'], (timestamp, timestamp))
        locations['unbaked'].mkdir(parents=True)
        (locations['unbaked'] / 'editable').write_text('field edits')
        return locations

    def test_bake_first_write_and_noninteractive_overwrite_refusal(self):
        for fleet in (False, True):
            with self.subTest(fleet=fleet):
                (self.root / 'vehicle.yaml').write_text(
                    'vehicle: ' + ('"{{vehicle}}"' if fleet else 'demo') + '\nimages: {tag: v1}\nsensors: []\n')
                tag = 'fleet' if fleet else 'v1'
                args = ['bake', '--tag', tag] if fleet else ['bake']
                with patch('sys.stdin.isatty', return_value=False), patch('builtins.input') as ask:
                    self.assertEqual(self.command(*args)[0], 0)
                    paths = artifacts.paths(self.root, tag)
                    original = paths['archive'].read_bytes()
                    marker = paths['staging'] / 'untouched'
                    marker.write_text('keep')
                    rc, _, err = self.command(*args)
                    self.assertEqual(rc, 1)
                    self.assertIn('--overwrite', err)
                    ask.assert_not_called()
                self.assertEqual(paths['archive'].read_bytes(), original)
                self.assertEqual(marker.read_text(), 'keep')

    def test_bake_decline_eof_confirm_and_explicit_overwrite(self):
        for fleet in (False, True):
            tag = 'fleet' if fleet else 'v1'
            locations = self.artifact(tag)
            (self.root / 'vehicle.yaml').write_text('vehicle: ' + ('"{{vehicle}}"' if fleet else 'demo') + '\nsensors: []\n')
            original = locations['archive'].read_bytes()
            for response in ('', 'n', EOFError()):
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', **kwargs):
                    self.assertEqual(self.command('artifact', 'bake', '--tag', tag)[0], 1)
                self.assertEqual(locations['archive'].read_bytes(), original)
                self.assertTrue((locations['staging'] / 'generated').exists())
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='yes') as ask:
                self.assertEqual(self.command('bake', '--tag', tag)[0], 0)
                ask.assert_called_once()
            self.assertFalse((locations['staging'] / 'generated').exists())
            with patch('sys.stdin.isatty', return_value=False), patch('builtins.input') as ask:
                self.assertEqual(self.command('bake', '--tag', tag, '--overwrite')[0], 0)
                ask.assert_not_called()

    def test_failed_or_racing_publication_preserves_existing_archive(self):
        locations = self.artifact()
        original = locations['archive'].read_bytes()
        with patch('tarfile.TarFile.add', side_effect=OSError('archive write failed')):
            result, _, error = self.command('bake', '--overwrite')
            self.assertEqual(result, 1)
            self.assertIn('cannot publish', error)
        self.assertEqual(locations['archive'].read_bytes(), original)
        self.assertEqual(list(locations['archive'].parent.glob('.*.tmp')), [])
        with self.assertRaisesRegex(RigError, 'appeared during'):
            artifacts.publish(locations['staging'], self.root, 'v1')
        self.assertEqual(locations['archive'].read_bytes(), original)

    def test_list_show_and_completion_need_no_resolved_identity(self):
        locations = self.artifact()
        (self.root / 'vehicle.yaml').write_text('vehicle: "{{vehicle}}"\nvehicle_id: "{{vehicle_id}}"\n')
        rc, out, _ = self.command('artifact', 'ls')
        self.assertEqual(rc, 0)
        self.assertIn('dev/amd64', out)
        self.assertIn('STAGING', out)
        self.assertIn('UNBAKED', out)
        rc, out, _ = self.command('artifact', 'show', 'v1', '--format', 'json')
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data['metadata']['bundle']['image_ids']['alias:v1'], 'sha256:abc')
        self.assertEqual(data['build_catalog']['reg/image:v1']['digest'], 'sha256:def')
        self.assertEqual(set(data['files']), set(locations))
        for verb in ('show', 'rm'):
            words = ['--root', str(self.root), 'artifact', verb, '']
            self.assertEqual(candidates(words, len(words) - 1), ['v1'])
        self.assertEqual(set(candidates(['image', 'build', '--scope', ''], 3)),
                         {'all', 'deployment', 'host', 'replay'})

    def test_remove_previews_confirms_and_preserves_editable_trees_and_run_data(self):
        locations = self.artifact()
        data = self.root / 'var/data/runs/recording'
        data.mkdir(parents=True)
        (data / 'data').write_text('recorded')
        with patch('subprocess.run', side_effect=AssertionError('cleanup must not invoke Docker')):
            rc, _, err = self.command('artifact', 'rm', 'v1', '--dry-run')
            self.assertEqual(rc, 0)
            self.assertIn('file data', err)
            self.assertTrue(all(p.exists() for p in locations.values()))
            with patch('sys.stdin.isatty', return_value=False):
                self.assertEqual(self.command('artifact', 'rm', 'v1')[0], 1)
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='n'):
                self.assertEqual(self.command('artifact', 'rm', 'v1')[0], 1)
            self.assertTrue(locations['archive'].exists())
            self.assertEqual(self.command('artifact', 'rm', 'v1', '--yes')[0], 0)
        self.assertFalse(locations['archive'].exists())
        self.assertFalse(locations['staging'].exists())
        self.assertEqual((locations['unbaked'] / 'editable').read_text(), 'field edits')
        self.assertEqual((data / 'data').read_text(), 'recorded')
        self.assertEqual(self.command('artifact', 'rm', 'v1', '--include-unbaked', '--yes')[0], 0)
        self.assertFalse(locations['unbaked'].exists())

    def test_prune_keeps_newest_archives_and_only_removes_matching_staging(self):
        locations = {i: self.artifact(f'v{i}', timestamp=i) for i in range(1, 6)}
        orphan = self.root / 'var/bake/in-progress'
        orphan.mkdir()
        rc, _, err = self.command('artifact', 'prune', '--keep', '2', '--dry-run')
        self.assertEqual(rc, 0)
        self.assertIn('3', err)
        self.assertTrue(all(p['archive'].exists() for p in locations.values()))
        self.assertEqual(self.command('artifact', 'prune', '--keep', '2', '--yes')[0], 0)
        for i, p in locations.items():
            self.assertEqual(p['archive'].exists(), i >= 4)
            self.assertEqual(p['staging'].exists(), i >= 4)
            self.assertTrue(p['unbaked'].exists())
        self.assertTrue(orphan.exists())
        self.assertEqual(self.command('artifact', 'prune', '--keep', '-1', '--yes')[0], 1)
        self.assertTrue(locations[4]['archive'].exists())

    def test_removal_validates_all_tags_and_never_follows_directory_symlinks(self):
        locations = self.artifact()
        self.assertEqual(self.command('artifact', 'rm', 'v1', 'missing', '--yes')[0], 1)
        self.assertEqual(self.command('artifact', 'rm', '../escape', '--yes')[0], 1)
        self.assertTrue(locations['archive'].exists())
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('outside')
        alias = self.root / 'var/bake/linked'
        alias.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.command('artifact', 'rm', 'linked', '--yes')[0], 0)
        self.assertFalse(alias.is_symlink())
        self.assertEqual(marker.read_text(), 'outside')
        moved = self.root / 'var/moved-bake'
        locations['staging'].parent.rename(moved)
        (self.root / 'var/bake').symlink_to(outside, target_is_directory=True)
        (outside / 'danger').mkdir()
        self.assertEqual(self.command('artifact', 'rm', 'danger', '--yes')[0], 1)
        self.assertTrue((outside / 'danger').is_dir())


if __name__ == '__main__':
    unittest.main()
