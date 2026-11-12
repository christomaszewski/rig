"""A simulator's statement is opaque evidence, copied only when its run is sealed."""
import contextlib
import copy
import hashlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_workflow as fixtures
from rig_cli import RigError, cli, runs, session_recipe, sim_workflow as sim, rundiff


class SimulationStatement(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.doc['simulation'].update(provider='external',
            simulator={'name': 'world', 'version': 'abc123'}, statement_file='../output/meta.json')
        self.f.path = self.f.write('sessions/dockyard.yaml', self.f.doc)
        self.source = self.f.root / 'output/meta.json'
        self.source.parent.mkdir()

    @contextlib.contextmanager
    def host(self):
        with self.f.host(), patch('rig_cli.sim_workflow._verify_running'), \
                contextlib.redirect_stderr(io.StringIO()):
            yield

    def start(self):
        manifest, descriptors, plan = self.f.build()
        self.assertTrue(plan['ready_to_run'], plan['issues'])
        self.assertEqual(sim.execute(self.f.root, self.f.path, manifest, descriptors, plan), 0)
        return Path(sim._read_state(self.f.root)[1]['output_run'])

    def doc(self, run):
        return yaml.safe_load((run / 'manifest.yaml').read_text())

    def seal(self, run):
        self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        doc = self.doc(run)
        self.assertIn('ended', doc)
        self.assertNotIn('failed', doc)
        self.assertFalse(sim.active_session(self.f.root))
        return doc['simulation']['statement']

    def test_recipe_accepts_a_path_for_either_provider_and_rejects_nonpaths(self):
        for provider in ('external', 'isaac'):
            doc = copy.deepcopy(self.f.doc)
            doc['simulation']['provider'] = provider
            if provider == 'isaac':
                del doc['simulation']['simulator']
            self.assertIs(session_recipe.validate(doc), doc)
        for value in ('', ' ', None, 1, [], {}, 'bad\x00name'):
            doc = copy.deepcopy(self.f.doc)
            doc['simulation']['statement_file'] = value
            with self.subTest(value=value), self.assertRaisesRegex(RigError, 'statement_file'):
                session_recipe.validate(doc)

    def test_capture_reads_final_bytes_at_seal_and_uses_frozen_recipe_path(self):
        self.source.write_bytes(b'early')
        with self.host():
            run = self.start()
            self.assertFalse((run / '.rig/simulation/statement').exists())
            final = b'not JSON: \x00\xff\n'
            self.source.write_bytes(final)
            changed = copy.deepcopy(self.f.doc)
            changed['simulation']['statement_file'] = '/unrelated/missing'
            self.f.write('sessions/dockyard.yaml', changed)
            record = self.seal(run)
        self.assertEqual(record['source'], str(self.source))
        self.assertEqual(record['path'], '.rig/simulation/statement')
        self.assertEqual((run / record['path']).read_bytes(), final)
        self.assertEqual(record['sha256'], hashlib.sha256(final).hexdigest())
        self.assertEqual(record['reported_by'], 'simulator')
        self.assertIs(record['verified'], False)
        self.assertNotIn('warning', record)
        self.source.write_bytes(b'later run')
        runs._seal(run, None)
        self.assertEqual((run / record['path']).read_bytes(), final)

    def test_missing_statement_never_blocks_check_start_or_seal(self):
        with self.host():
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--root', str(self.f.root), 'sim', 'check',
                                          '--recipe', 'dockyard', '--json']), 0)
            run = self.start()
            record = self.seal(run)
        self.assertIn('warning', record)
        self.assertNotIn('sha256', record)
        self.assertNotIn('path', record)
        self.assertEqual(record['reported_by'], 'simulator')
        self.assertIs(record['verified'], False)

    def test_unreadable_statement_is_a_recorded_warning(self):
        self.source.write_bytes(b'private')
        with self.host():
            run = self.start()
            self.source.chmod(0)
            try:
                if os.geteuid() == 0:
                    self.skipTest('root can read mode 000 files')
                record = self.seal(run)
            finally:
                self.source.chmod(0o600)
        self.assertIn('warning', record)
        self.assertNotIn('sha256', record)

    def test_nonregular_statement_is_a_warning_without_waiting_for_a_writer(self):
        os.mkfifo(self.source)
        with self.host():
            run = self.start()
            record = self.seal(run)
        self.assertIn('warning', record)
        self.assertNotIn('path', record)

    def test_bare_down_then_run_end_still_captures_the_statement(self):
        with self.host():
            run = self.start()
            self.assertEqual(sim.down(self.f.root), 0)
            self.assertNotIn('ended', self.doc(run))
            self.source.write_bytes(b'world finished after consumers stopped')
            self.assertEqual(cli.main(['--root', str(self.f.root), 'run', 'end']), 0)
        record = self.doc(run)['simulation']['statement']
        self.assertEqual((run / record['path']).read_bytes(), self.source.read_bytes())

    def test_recipe_without_statement_keeps_existing_run_shape(self):
        del self.f.doc['simulation']['statement_file']
        self.f.write('sessions/dockyard.yaml', self.f.doc)
        with self.host():
            run = self.start()
            self.assertEqual(sim.down(self.f.root, end_run=True), 0)
        self.assertNotIn('statement', self.doc(run)['simulation'])


class StatementDiff(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_dir(self, name, body=None, warning=None):
        run = self.root / name
        (run / '.rig/simulation').mkdir(parents=True)
        record = dict(source='/world/' + name + '/meta.json', reported_by='simulator', verified=False)
        if body is not None:
            path = '.rig/simulation/statement'
            (run / path).write_bytes(body)
            record.update(path=path, sha256=hashlib.sha256(body).hexdigest())
        if warning:
            record['warning'] = warning
        (run / 'manifest.yaml').write_text(yaml.safe_dump(dict(
            run=name, vehicle='rover', simulation=dict(provider='external', statement=record,
                recipe={'simulation': {'statement_file': '/world/' + name + '/meta.json'}}))))
        return run

    def test_changed_bytes_get_their_own_section_without_parsing(self):
        a, b = self.run_dir('a', b'\x00\xff'), self.run_dir('b', b'\x00\xfe')
        report = rundiff.compare(a, b)
        self.assertTrue(report['substantive'])
        self.assertEqual({d['section'] for d in report['differs']}, {'simulator statement'})
        self.assertTrue(any(d['key'] == 'sha256' for d in report['differs']))
        self.assertIn('reported by the simulator', rundiff.render(report))

    def test_same_bytes_at_different_source_paths_are_equal(self):
        a, b = self.run_dir('a', b'same'), self.run_dir('b', b'same')
        report = rundiff.compare(a, b)
        self.assertFalse(report['substantive'], report)
        self.assertIn('simulator statement', report['same'])
        self.assertTrue(any(d['key'] == 'simulation.statement.source' for d in report['per_run']))

    def test_warning_and_absent_capture_are_compared_in_the_statement_section(self):
        a, b = self.run_dir('a', b'world'), self.run_dir('b', warning='not readable')
        report = rundiff.compare(a, b)
        self.assertTrue(report['substantive'])
        self.assertEqual({d['section'] for d in report['differs']}, {'simulator statement'})

    def test_missing_files_at_different_source_paths_have_the_same_outcome(self):
        a, b = self.run_dir('a'), self.run_dir('b')
        runs._seal(a, None)
        runs._seal(b, None)
        report = rundiff.compare(a, b)
        self.assertFalse(report['substantive'], report['differs'])
        self.assertIn('simulator statement', report['same'])


if __name__ == '__main__':
    unittest.main()
