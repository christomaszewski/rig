"""A simulation probe that fails says what happened to it.

rig runs the probe as `<simulation.probe> -c <source>` and reads one JSON document from its
standard output. When there is none, the message gives the probe's exit code, how long it ran
and the end of its error stream, and a session that had opened a run keeps both streams with
the run. The advice to set simulation.probe is for a probe that could not be run at all.

The probes here are small stand-in executables, so the exit codes, the timing and the streams
are real ones. No ROS installation is needed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_external as external
from rig_cli import RigError, session_runtime as runtime, sim_workflow as sim


ADVICE = "Set simulation.probe to Python"
NOT_READY = {"version": 1, "ready": False,
             "issues": [{"status": "error", "code": "clock", "message": "clock paused"}]}


class ProbeFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def stand_in(self, body):
        """An executable that takes rig's `-c <source>` and request, and does `body` instead."""
        path = Path(self.tmp.name) / "probe"
        path.write_text("#!/bin/sh\ncat >/dev/null\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def simulation(self, probe, provider="external"):
        simulation = external.recipe()["simulation"] if provider == "external" else \
            external.isaac(external.recipe())["simulation"]
        return dict(simulation, probe=probe, timeout_s=1)

    def failure(self, body, **kwargs):
        with self.assertRaises(RigError) as raised:
            sim.probe(self.simulation([self.stand_in(body)], **kwargs), dict(os.environ))
        return raised.exception

    def test_a_probe_that_dies_gives_its_exit_code_its_duration_and_its_last_words(self):
        error = self.failure("echo 'rmw: session lost' >&2\necho 'rmw: giving up' >&2\nexit 1")
        message = str(error)
        self.assertRegex(message, r"^worldsim ROS probe failed: simulation\.probe \(\S*probe\) "
                                  r"exited 1 after \d+\.\d s and printed nothing")
        self.assertIn("Its error stream ended:", message)
        self.assertLess(message.index("rmw: session lost"), message.index("rmw: giving up"))
        self.assertNotIn(ADVICE, message)
        self.assertNotIn("Expecting value", message)

    def test_a_probe_that_dies_silently_says_that_too(self):
        message = str(self.failure("exit 3"))
        self.assertRegex(message, r"exited 3 after \d+\.\d s and printed nothing")
        self.assertIn("Its error stream was empty", message)
        self.assertNotIn(ADVICE, message)

    def test_output_that_is_not_a_result_is_quoted(self):
        message = str(self.failure("echo 'Segmentation fault'\necho 'second line'\nexit 0"))
        self.assertRegex(message, r"exited 0 after \d+\.\d s and printed something other than a "
                                  r"probe result")
        self.assertIn("'Segmentation fault'", message)
        self.assertNotIn("second line", message)
        self.assertNotIn(ADVICE, message)

    def test_only_the_end_of_a_long_error_stream_is_quoted(self):
        message = str(self.failure(
            "i=0; while [ $i -lt 400 ]; do echo \"diagnostic line $i\" >&2; i=$((i+1)); done\nexit 1"))
        self.assertIn("diagnostic line 399", message)
        self.assertNotIn("diagnostic line 0\n", message)
        self.assertLess(len(message), 2000)

    def test_a_probe_killed_by_a_signal_names_the_signal(self):
        message = str(self.failure("kill -9 $$"))
        self.assertRegex(message, r"was killed by signal 9 \(SIGKILL\) after \d+\.\d s")
        self.assertNotIn(ADVICE, message)

    def test_a_probe_that_never_answers_is_stopped_and_says_how_long_it_had(self):
        with patch.object(sim, "PROBE_GRACE_S", 0.5):
            message = str(self.failure("echo 'still discovering' >&2\nexec sleep 30"))
        self.assertRegex(message, r"printed no result in 1\.5 s and was stopped")
        self.assertIn("still discovering", message)
        self.assertNotIn(ADVICE, message)

    def test_the_interpreter_advice_is_for_a_probe_that_could_not_be_run(self):
        missing = str(Path(self.tmp.name) / "no-such-python")
        with self.assertRaises(RigError) as raised:
            sim.probe(self.simulation([missing]), dict(os.environ))
        self.assertIn(ADVICE, str(raised.exception))
        self.assertIn("no-such-python", str(raised.exception))
        # A wrapper reports a command it could not find or start with the shell's 127 and 126.
        for code in (126, 127):
            with self.subTest(code=code):
                message = str(self.failure(f"exit {code}"))
                self.assertRegex(message, rf"exited {code} after")
                self.assertIn(ADVICE, message)

    def test_a_report_of_trouble_is_still_the_probes_own(self):
        """Exit 1 with a result is the probe saying what it saw, not a failure to report."""
        body = "cat <<'EOF'\n" + json.dumps(NOT_READY) + "\nEOF\necho 'a log line' >&2\nexit 1"
        result = sim.probe(self.simulation([self.stand_in(body)]), dict(os.environ))
        self.assertEqual(result, NOT_READY)
        self.assertIn("a log line", result.log)

    def test_an_isaac_probe_is_named_as_isaacs(self):
        message = str(self.failure("exit 1", provider="isaac"))
        self.assertRegex(message, r"^Isaac ROS probe failed: simulation\.probe \(\S*probe\) exited 1")
        self.assertNotIn("worldsim", message)

    def test_the_failure_carries_both_streams_for_the_record(self):
        error = self.failure("echo 'partial' \necho 'why' >&2\nexit 4")
        self.assertIsInstance(error, sim.ProbeFailure)
        self.assertEqual(error.evidence["returncode"], 4)
        self.assertEqual((error.evidence["stdout"], error.evidence["stderr"]), ("partial\n", "why\n"))
        self.assertGreaterEqual(error.evidence["duration_s"], 0)
        self.assertEqual(error.evidence["argv"][-1], "-c")


# What rmw_zenoh's Zenoh puts on the process's standard output when it logs, colour included.
ZENOH_LINE = (b"\x1b[2m2026-11-12T15:51:40.123456Z\x1b[0m \x1b[31mERROR\x1b[0m ThreadId(04) "
              b"\x1b[2mzenoh::net::runtime\x1b[0m\x1b[2m:\x1b[0m session with the router was lost\n")


class NativeOutput(unittest.TestCase):
    """The probe's standard output carries its result and nothing else.

    A native library writes to file descriptor 1 without going through Python: Zenoh's own log
    does, and a line of it ahead of the result made rig report a JSON error at character 0 and
    drop both the line and the result. These probes run rig's real probe source against a
    stand-in `rclpy` that writes the way such a library does and is then unavailable.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def probe(self, stand_in):
        package = Path(self.tmp.name) / "rclpy"
        package.mkdir()
        (package / "__init__.py").write_text(stand_in)
        simulation = dict(external.recipe()["simulation"], probe=[sys.executable], timeout_s=1)
        return sim.probe(simulation, dict(os.environ, PYTHONPATH=self.tmp.name))

    def test_a_zenoh_log_line_does_not_hide_the_probes_result(self):
        result = self.probe(f"import os\nos.write(1, {ZENOH_LINE!r})\n"
                            "raise ImportError('stand-in rclpy stops here')\n")
        # v0.2.85: "worldsim ROS probe failed: Expecting value: line 1 column 1 (char 0)"
        self.assertFalse(result["ready"])
        self.assertEqual([i["code"] for i in result["issues"] if i["status"] == "error"],
                         ["ros_unavailable"])

    def test_what_the_middleware_logged_comes_back_beside_the_result(self):
        result = self.probe(f"import os\nos.write(1, {ZENOH_LINE!r})\n"
                            "raise ImportError('stand-in rclpy stops here')\n")
        self.assertIn("The probe's error stream ended:", result.log)
        self.assertIn("ERROR", result.log)
        self.assertIn("session with the router was lost", result.log)
        self.assertNotIn("\x1b", result.log)  # the colour codes are not for a report
        # The result itself is what the probe printed: the log is not mixed into the evidence.
        self.assertNotIn("session with the router", json.dumps(result))

    def test_output_a_library_buffers_until_exit_does_not_follow_the_result(self):
        """C stdio holds a pipe's output until the process ends, after the result is written."""
        result = self.probe("import ctypes\n"
                            "ctypes.CDLL(None).printf(b'a late line from a native library\\n')\n"
                            "raise ImportError('stand-in rclpy stops here')\n")
        self.assertEqual([i["code"] for i in result["issues"] if i["status"] == "error"],
                         ["ros_unavailable"])

    def test_check_shows_the_log_under_a_probe_that_is_not_ready(self):
        plan = dict(recipe=external.recipe(), issues=[])
        seen = sim.ProbeResult({"version": 1, "ready": False, "issues": [
            dict(status="error", code="clock_stopped", message="No advancing /clock received")]},
            log="The probe's error stream ended:\n    ERROR zenoh: session lost")
        sim._apply_probe(plan, seen)
        self.assertEqual([(i["status"], i["code"]) for i in plan["issues"]],
                         [("error", "probe_clock_stopped"), ("warning", "probe_log")])
        self.assertIn("ERROR zenoh: session lost", plan["issues"][1]["message"])
        self.assertIs(type(plan["probe"]), dict)  # the plan is written as YAML
        # A probe that is ready has nothing to explain.
        plan = dict(recipe=external.recipe(), issues=[])
        sim._apply_probe(plan, sim.ProbeResult(external.READY, log="The probe's error stream ended:\n    x"))
        self.assertEqual(plan["issues"], [])

    def test_a_ready_probe_reports_no_log(self):
        """What a healthy probe returns is what the probe printed, whatever its error stream held."""
        ready = {"version": 1, "ready": True, "issues": []}
        script = Path(self.tmp.name) / "probe"
        script.write_text("#!/bin/sh\ncat >/dev/null\necho 'a harmless middleware line' >&2\n"
                          f"cat <<'EOF'\n{json.dumps(ready)}\nEOF\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        simulation = dict(external.recipe()["simulation"], probe=[str(script)], timeout_s=1)
        self.assertEqual(sim.probe(simulation, dict(os.environ)), ready)


class FailedProbeInASession(external.ExternalSession):
    """A probe that dies once the run is open leaves its streams with the run's evidence."""

    def died(self):
        return sim.ProbeFailure("worldsim ROS probe failed: simulation.probe (ros-python) exited 1 "
                                "after 11.2 s and printed nothing. Its error stream was empty.",
                                dict(argv=["ros-python", "-c"], returncode=1, duration_s=11.2,
                                     stdout="", stderr="", timed_out=False))

    def start_with(self, ready_probes):
        outcomes = iter([external.READY] * ready_probes + [self.died()])

        def probe(*args, **kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        with self.host(probe=probe):
            manifest, descriptors, plan = self.f.build()
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            with self.assertRaisesRegex(RigError, "exited 1 after 11.2 s"):
                sim.execute(self.root, self.f.path, manifest, descriptors, plan)

    def test_the_probe_after_startup_leaves_its_streams_with_the_run(self):
        self.start_with(3)
        state = runtime.read_state(self.root)[1]
        self.assertEqual(state["status"], "failed")
        self.assertIn("exited 1 after 11.2 s", state["error"])
        kept = json.loads((Path(state["output_run"]) / ".rig/simulation/probe-failed.json").read_text())
        self.assertEqual(kept, dict(stage="after startup", argv=["ros-python", "-c"], returncode=1,
                                    duration_s=11.2, stdout="", stderr="", timed_out=False))
        self.assertFalse((Path(state["output_run"]) / ".rig/simulation/probe-after.json").exists())

    def test_the_launch_gate_does_the_same_and_starts_nothing(self):
        self.start_with(2)
        state = runtime.read_state(self.root)[1]
        kept = json.loads((Path(state["output_run"]) / ".rig/simulation/probe-failed.json").read_text())
        self.assertEqual(kept["stage"], "launch gate")
        self.assertEqual(self.f.calls, [])

    def test_the_reason_for_a_failed_verification_includes_what_the_middleware_logged(self):
        unseen = sim.ProbeResult({"version": 1, "ready": False, "issues": [
            dict(status="error", code="clock_stopped", message="No advancing /clock received")]},
            log="The probe's error stream ended:\n    ERROR zenoh: session lost")
        observations = iter([external.READY] * 3 + [unseen])
        with self.host(probe=lambda *args, **kwargs: next(observations)):
            manifest, descriptors, plan = self.f.build()
            with self.assertRaises(RigError) as raised:
                sim.execute(self.root, self.f.path, manifest, descriptors, plan)
        message = str(raised.exception)
        self.assertIn("worldsim graph failed verification after startup: No advancing /clock received",
                      message)
        self.assertIn("ERROR zenoh: session lost", message)

    def test_a_session_that_starts_keeps_no_such_file(self):
        with self.host():
            _, state = self.start()
        self.assertFalse((Path(state["output_run"]) / ".rig/simulation/probe-failed.json").exists())


if __name__ == "__main__":
    unittest.main()
