"""A run whose attach failed is listed as failed, not as a sealed recording.

`rig sim up` opens the run before it starts the services, so a startup that fails leaves a
run behind, and `rig sim down --end-run` seals it. Its manifest then says the session never
got going, and `rig runs` and `rig catalog` show it as `failed` under the label that was
asked for. A session that started is listed as it always was.
"""
import contextlib
import io
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_external as external
from rig_cli import RigError, runcatalog, runs, session_runtime as runtime, sim_workflow as sim


PAUSED = {"version": 1, "ready": False,
          "issues": [dict(status="error", code="clock", message="paused")]}


class FailedAttach(external.ExternalSession):
    def fail_after(self, ready_probes, label="lap"):
        """`rig sim up` with the simulator answering `ready_probes` probes and then paused."""
        observations = iter([external.READY] * ready_probes + [PAUSED])
        with self.host(probe=lambda *args, **kwargs: next(observations)):
            manifest, descriptors, plan = self.f.build()
            with self.assertRaises(RigError):
                sim.execute(self.root, self.f.path, manifest, descriptors, plan, label=label)
        saved = runtime.read_state(self.root)
        return Path(saved[1]["output_run"]) if saved else None

    def end(self):
        with self.host() as err:
            self.assertEqual(self.rig("sim", "down", "--end-run"), 0, err.getvalue())
        return err.getvalue()

    def rows(self):
        return {row.label: row for row in runs.registry_rows(self.root / "data")}

    def listing(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.rig("runs"), 0)
        return out.getvalue(), err.getvalue()

    def test_the_manifest_says_the_session_never_started(self):
        run = self.fail_after(3)
        failed = yaml.safe_load((run / "manifest.yaml").read_text())["failed"]
        self.assertEqual(set(failed), {"stage", "at", "error"})
        self.assertEqual(failed["stage"], "startup")
        self.assertEqual(failed["error"], "worldsim graph failed verification after startup: paused")
        # The run is still open: the services were rolled back, the session is not yet ended.
        self.assertEqual(self.rows()["lap"].state, "OPEN")
        self.end()
        sealed = yaml.safe_load((run / "manifest.yaml").read_text())
        self.assertEqual(sealed["failed"], failed)
        self.assertIn("ended", sealed)

    def test_rig_runs_lists_it_as_failed_and_says_why(self):
        self.fail_after(3)
        said = self.end()
        row = self.rows()["lap"]
        self.assertEqual(row.state, "failed")
        self.assertNotEqual(row.ended, "—")
        out, err = self.listing()
        line = next(line for line in out.splitlines() if "_lap" in line)
        self.assertIn("failed", line.split())
        self.assertNotIn("sealed", line.split())
        self.assertRegex(err, r"rig runs: \S+_lap failed at startup, so no session ran: "
                              r"worldsim graph failed verification after startup: paused")
        self.assertIn("recorded as failed at startup", said)

    def test_a_launch_gate_that_fails_is_listed_the_same_way(self):
        self.fail_after(2)
        self.end()
        self.assertEqual(self.rows()["lap"].state, "failed")

    def test_a_failure_before_the_run_is_opened_leaves_no_run(self):
        self.assertIsNone(self.fail_after(1))
        self.assertEqual(self.rows(), {})

    def test_a_session_that_started_is_listed_as_sealed(self):
        with self.host():
            manifest, descriptors, plan = self.f.build()
            self.assertEqual(sim.execute(self.root, self.f.path, manifest, descriptors, plan,
                                         label="good"), 0)
            run = Path(runtime.read_state(self.root)[1]["output_run"])
        said = self.end()
        self.assertNotIn("failed", yaml.safe_load((run / "manifest.yaml").read_text()))
        self.assertEqual(self.rows()["good"].state, "sealed")
        out, err = self.listing()
        self.assertNotIn("failed", out + err + said)

    def test_a_later_attach_under_the_same_label_is_told_apart(self):
        self.fail_after(3)
        self.end()
        with self.host():
            manifest, descriptors, plan = self.f.build()
            self.assertEqual(sim.execute(self.root, self.f.path, manifest, descriptors, plan,
                                         label="lap"), 0)
        self.end()
        states = sorted(row.state for row in runs.registry_rows(self.root / "data"))
        self.assertEqual(states, ["failed", "sealed"])

    def test_the_catalog_lists_and_filters_it_as_failed(self):
        self.fail_after(3)
        self.end()
        with self.host():
            manifest, descriptors, plan = self.f.build()
            self.assertEqual(sim.execute(self.root, self.f.path, manifest, descriptors, plan,
                                         label="good"), 0)
        self.end()
        entries = [runcatalog._entry(d, self.root / "data", open_id=None)
                   for d in sorted((self.root / "data/runs").iterdir())]
        self.assertEqual({e.label: e.state for e in entries}, {"lap": "failed", "good": "sealed"})

        def select(state):
            return [e.label for e in runcatalog.select(entries, query=[], tags=[], label=None,
                                                       vehicle=None, since=None, until=None, state=state)]
        self.assertEqual(select("failed"), ["lap"])
        self.assertEqual(select("sealed"), ["good"])

    def test_a_failed_run_is_removed_like_any_sealed_run(self):
        run = self.fail_after(3)
        self.end()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.rig("run", "rm", run.name), 0)
        self.assertFalse(run.exists())


class FailedIsaacAttach(external.Session):
    """The mark is the run's, whichever simulator the session was to attach."""

    def test_a_failed_isaac_attach_is_listed_as_failed(self):
        observations = iter([external.READY] * 3 + [PAUSED])
        with self.host(probe=lambda *args, **kwargs: next(observations)):
            manifest, descriptors, plan = self.f.build()
            with self.assertRaises(RigError):
                sim.execute(self.root, self.f.path, manifest, descriptors, plan, label="dock")
            self.assertEqual(self.rig("sim", "down", "--end-run"), 0)
        row = next(iter(runs.registry_rows(self.root / "data")))
        self.assertEqual((row.label, row.state), ("dock", "failed"))


if __name__ == "__main__":
    unittest.main()
