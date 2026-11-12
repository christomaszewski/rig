"""An external simulator attaches through rig sim and is recorded as what it is.

The recipe names its provider `external` and the simulator's own name and version. The
plan, the run metadata, the saved session and every message then say that, and never
Isaac. The Isaac provider keeps each record and message it had. The deployment is
test_sim_workflow's fixture: no GPU, ROS installation or Docker daemon is needed.
"""
import contextlib
import copy
import importlib.abc
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_workflow as fixtures
from rig_cli import RigError, cli, dispatch, session_recipe as sessions, session_runtime as runtime
from rig_cli import sim_probe, sim_workflow as sim


RECIPE = """
version: 2
session:
  instances: [bag_logger, planner]
  clock: source
  overrides:
    bag_logger:
      control: {use_sim_time: true}
simulation:
  provider: external
  simulator: {name: worldsim, version: "abc1234"}
  mode: attach
  ros: {distro: lyrical, rmw: rmw_zenoh_cpp, domain_id: 1}
  scene: scenarios/typical.yaml
  seed: 7
  publishes:
    - {topic: /world/view, type: std_msgs/msg/String}
    - {topic: /world/report, type: std_msgs/msg/String}
  subscribes:
    - {topic: /agent/plan, type: std_msgs/msg/String}
  probe: [python3]
  timeout_s: 10
replay:
  source: run-a
  playback:
    exclude: [/agent/plan]
"""
SIMULATOR = {"name": "worldsim", "version": "abc1234"}
READY = {"version": 1, "ready": True, "issues": []}
# What a saved Isaac session holds at v0.2.84; an Isaac session must not grow a field.
ISAAC_STATE_KEYS = {"version", "session", "provider", "status", "run_id", "output_run", "data_dir",
                    "vehicle_id", "vehicle", "ros", "engine_id", "docker_environment", "selected",
                    "attempted", "environment", "rows", "repos", "launch_surfaces", "configs"}


def recipe():
    return yaml.safe_load(RECIPE)


def isaac(doc):
    """The same session attached to Isaac instead."""
    doc = copy.deepcopy(doc)
    doc["simulation"]["provider"] = "isaac"
    del doc["simulation"]["simulator"]
    return doc


def says_isaac(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return "isaac" in text.lower()


class ExternalRecipe(unittest.TestCase):
    def refused(self, doc, where):
        with self.assertRaisesRegex(RigError, where):
            sessions.validate(doc)

    def test_the_recipe_validates_for_simulation_and_for_replay(self):
        doc = recipe()
        before = copy.deepcopy(doc)
        self.assertIs(sessions.validate(doc), doc)
        self.assertEqual(sessions.provider_recipe(doc, "simulation"), before["simulation"])
        self.assertEqual(sessions.provider_recipe(doc, "simulation")["simulator"], SIMULATOR)
        self.assertEqual(sessions.provider_recipe(doc, "replay"),
                         {"version": 1, "source": "run-a", "playback": {"exclude": ["/agent/plan"]}})
        self.assertEqual(doc, before)

    def test_an_unknown_provider_is_refused(self):
        for shape in (recipe(), isaac(recipe())):
            for provider in ("gazebo", "Isaac", "EXTERNAL", "external ", "", None, 7, ["external"]):
                doc = copy.deepcopy(shape)
                doc["simulation"]["provider"] = provider
                with self.subTest(provider=provider, simulator="simulator" in shape["simulation"]):
                    self.refused(doc, "supported provider")
            doc = copy.deepcopy(shape)
            del doc["simulation"]["provider"]
            with self.subTest(provider="absent", simulator="simulator" in shape["simulation"]):
                self.refused(doc, "supported provider")
            # attach stays the only mode, whichever the provider
            doc = copy.deepcopy(shape)
            doc["simulation"]["mode"] = "launch"
            with self.subTest(mode="launch", simulator="simulator" in shape["simulation"]):
                self.refused(doc, "supported provider/mode")

    def test_simulator_is_refused_with_isaac(self):
        doc = isaac(recipe())
        self.assertIs(sessions.validate(doc), doc)
        doc["simulation"]["simulator"] = dict(SIMULATOR)
        self.refused(doc, r"simulation\.simulator")

    def test_isaac_version_is_refused_with_external(self):
        doc = isaac(recipe())
        doc["simulation"]["isaac_version"] = "6.0.0"
        self.assertIs(sessions.validate(doc), doc)
        doc = recipe()
        doc["simulation"]["isaac_version"] = "6.0.0"
        self.refused(doc, r"simulation\.isaac_version")

    def test_external_requires_a_simulator_name_and_version(self):
        doc = recipe()
        del doc["simulation"]["simulator"]
        self.refused(doc, r"simulation\.simulator")
        for simulator in (None, "worldsim", ["worldsim", "abc1234"], {}, {"name": "worldsim"},
                          {"version": "abc1234"}, {"name": "", "version": "abc1234"},
                          {"name": "worldsim", "version": " "}, {"name": 7, "version": "abc1234"},
                          {"name": None, "version": "abc1234"}, {"name": ["worldsim"], "version": "abc1234"},
                          {"name": "worldsim", "version": 1.2}, {"name": "worldsim", "version": 7},
                          {"name": "worldsim", "version": True}, {"name": "worldsim", "version": None},
                          {**SIMULATOR, "vendor": "acme"}):
            doc = recipe()
            doc["simulation"]["simulator"] = simulator
            with self.subTest(simulator=simulator):
                self.refused(doc, r"simulation\.simulator")


class ExternalClock(unittest.TestCase):
    def test_with_clock_sets_sim_time_for_external(self):
        original = {"RIG_SIM_TIME": "poison", "OTHER": "kept"}
        env = dispatch.with_clock(original, provider="external")
        self.assertEqual(env["RIG_SIM_TIME"], "1")
        self.assertEqual(env["OTHER"], "kept")
        self.assertEqual(original["RIG_SIM_TIME"], "poison")
        for provider in ("isaac", "replay"):
            self.assertEqual(dispatch.with_clock(original, provider=provider)["RIG_SIM_TIME"], "1")
        self.assertNotIn("RIG_SIM_TIME", dispatch.with_clock(original, provider=None))
        with self.assertRaisesRegex(RigError, "Unknown session clock provider"):
            dispatch.with_clock(original, provider="gazebo")


def probe_request(**extra):
    simulation = recipe()["simulation"]
    return dict(ros=simulation["ros"], publishes=simulation["publishes"],
                subscribes=simulation["subscribes"], timeout_s=0.05, **extra)


def observation():
    return dict(ros=probe_request()["ros"], clock=dict(publishers=1, max_publishers=1, samples=2,
                first_ns=0, last_ns=100, advancing=True, reset=False, advance_age_s=0),
                topics={"/world/view": {"publishers": [dict(type="std_msgs/msg/String")]},
                        "/world/report": {"publishers": [dict(type="std_msgs/msg/String")]},
                        "/agent/plan": {"subscribers": [dict(type="std_msgs/msg/String")]}})


class ExternalProbe(unittest.TestCase):
    """The probe is told the simulator in its request, as the recipe's own name/version mapping."""

    def stopped(self, request):
        observed = observation()
        observed["clock"].update(samples=1)
        result = sim_probe.evaluate(request, observed)
        self.assertFalse(result["ready"])
        return next(i["message"] for i in result["issues"] if i["code"] == "clock_stopped")

    def test_a_stopped_clock_names_the_external_simulator(self):
        message = self.stopped(probe_request(simulator=dict(SIMULATOR)))
        self.assertIn("worldsim", message)
        self.assertFalse(says_isaac(message), message)
        # A request that names no simulator is Isaac's, and so is the advice.
        self.assertEqual(self.stopped(probe_request()),
                         "No advancing /clock received; enable Isaac's ROS bridge and press Play")

    def test_no_external_probe_result_mentions_isaac(self):
        request = probe_request(simulator=dict(SIMULATOR))
        result = sim_probe.evaluate(request, observation())
        self.assertTrue(result["ready"], result["issues"])
        self.assertFalse(says_isaac(result))
        for changes in ({"publishers": 0}, {"samples": 1}, {"advancing": False},
                        {"advance_age_s": 3}, {"reset": True}):
            observed = observation()
            observed["clock"].update(changes)
            result = sim_probe.evaluate(request, observed)
            with self.subTest(changes=changes):
                self.assertFalse(result["ready"])
                self.assertFalse(says_isaac(result), result["issues"])

    def test_the_request_takes_only_a_name_and_version_for_the_simulator(self):
        good = probe_request(simulator=dict(SIMULATOR))
        self.assertIs(sim_probe.validate_request(good), good)
        for simulator in (None, "worldsim", [], {}, {"name": "worldsim"}, {"version": "abc1234"},
                          {"name": "", "version": "abc1234"}, {"name": 7, "version": "abc1234"},
                          {"name": "worldsim", "version": 7}, {**SIMULATOR, "vendor": "acme"}):
            with self.subTest(simulator=simulator), self.assertRaises(ValueError):
                sim_probe.validate_request(probe_request(simulator=simulator))

    def test_the_stdin_protocol_accepts_a_named_simulator(self):
        class MissingRos(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "rclpy":
                    raise ImportError("deliberately unavailable")
        out = io.StringIO()
        request = json.dumps(probe_request(simulator=dict(SIMULATOR)))
        with patch.object(sys, "stdin", io.StringIO(request)), contextlib.redirect_stdout(out), \
                patch.object(sys, "meta_path", [MissingRos(), *sys.meta_path]):
            rc = sim_probe.main()
        self.assertEqual(rc, 1)
        # The request was understood: only the ROS installation this test withholds is missing.
        self.assertEqual([i["code"] for i in json.loads(out.getvalue())["issues"]], ["ros_unavailable"])


class ExternalProbeRequest(unittest.TestCase):
    def sent(self, simulation):
        with patch("rig_cli.sim_workflow.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(READY), "")) as run:
            sim.probe(simulation, {})
        return json.loads(run.call_args.kwargs["input"])

    def test_the_workflow_tells_the_probe_which_simulator_it_attaches(self):
        request = self.sent(recipe()["simulation"])
        self.assertEqual(request["simulator"], SIMULATOR)
        self.assertIs(sim_probe.validate_request(request), request)
        # An Isaac request is what it always was.
        request = self.sent(isaac(recipe())["simulation"])
        self.assertEqual(set(request), {"ros", "publishes", "subscribes", "timeout_s"})
        self.assertIs(sim_probe.validate_request(request), request)

    def test_a_probe_that_cannot_run_names_the_simulator(self):
        for simulation, name in ((recipe()["simulation"], "worldsim"),
                                 (isaac(recipe())["simulation"], "Isaac")):
            with self.subTest(simulator=name), \
                    patch("rig_cli.sim_workflow.subprocess.run", side_effect=FileNotFoundError("no Python")):
                with self.assertRaisesRegex(RigError, name + " ROS probe failed.*simulation.probe") as raised:
                    sim.probe(simulation, {})
                self.assertEqual(says_isaac(str(raised.exception)), name == "Isaac")


class Session(unittest.TestCase):
    """test_sim_workflow's Isaac deployment and recipe, with a captured stderr."""

    def setUp(self):
        self.f = fixtures.Simulation()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root

    @contextlib.contextmanager
    def host(self, **kwargs):
        with self.f.host(**kwargs), patch("rig_cli.sim_workflow._verify_running"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            yield err

    def plan(self, doc=None, **kwargs):
        return self.f.build(doc, **kwargs)[2]

    def start(self):
        manifest, descriptors, plan = self.f.build()
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(sim.execute(self.root, self.f.path, manifest, descriptors, plan), 0)
        return plan, runtime.read_state(self.root)[1]

    def rig(self, *argv):
        return cli.main(["--root", str(self.root), *argv])

    def state_path(self, state):
        return self.root / "var/sessions" / state["session"] / "state.json"

    def assert_no_isaac(self, folder):
        files = [p for p in Path(folder).rglob("*") if p.is_file()]
        self.assertTrue(files, f"nothing was written under {folder}")
        for path in files:
            self.assertNotIn("isaac", str(path.relative_to(folder)).lower())
            self.assertNotIn(b"isaac", path.read_bytes().lower(), str(path))
        return {str(p.relative_to(folder)) for p in files}


class ExternalSession(Session):
    """The deployment the recipe names: Lyrical on Zenoh in domain 1, a logger and a planner."""

    def setUp(self):
        super().setUp()
        vehicle = yaml.safe_load((self.root / "vehicle.yaml").read_text())
        vehicle["ros"] = {"distro": "lyrical", "rmw": "rmw_zenoh_cpp", "domain_id": 1}
        vehicle["infra"] = [{"name": "bag_logger", "service": "ros2-bag-logger", "config": "bag_logger.yaml"}]
        self.f.write("vehicle.yaml", vehicle)
        self.f.write("bag_logger.yaml", {"name": "bag_logger", "service": "ros2-bag-logger", "gain": 1})
        self.f.doc = recipe()
        self.f.path = self.f.write("sessions/world.yaml", self.f.doc)


class ExternalPlan(ExternalSession):
    def test_check_reports_external_and_the_simulator(self):
        with self.host(), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self.rig("sim", "check", "--recipe", "world", "--json")
        self.assertEqual(rc, 0, out.getvalue())
        plan = json.loads(out.getvalue())
        self.assertTrue(plan["ready_to_run"], plan["issues"])
        self.assertEqual(plan["provider"], "external")
        self.assertEqual(plan["simulator"], SIMULATOR)
        self.assertEqual(plan["clock"], "external")
        self.assertEqual(plan["mode"], "attach")
        self.assertEqual(plan["selected"], ["bag_logger", "planner"])
        self.assertIs(plan["configs"]["bag_logger"]["control"]["use_sim_time"], True)
        self.assertEqual(plan["recipe"], self.f.doc)
        self.assertFalse(says_isaac(out.getvalue()), plan["issues"])
        self.assertFalse(sim.active_session(self.root))
        self.assertEqual(self.f.calls, [])

    def test_the_text_report_names_the_simulator(self):
        with self.host() as err:
            self.assertEqual(self.rig("sim", "check", "--recipe", "world"), 0, err.getvalue())
        self.assertIn("rig sim: attach worldsim; selected bag_logger, planner", err.getvalue())
        self.assertFalse(says_isaac(err.getvalue()), err.getvalue())

    def test_the_humble_jazzy_warning_is_isaacs_alone(self):
        plan = self.plan(host=False)
        self.assertNotIn("experimental_ros", [i["code"] for i in plan["issues"]])
        # The same session attached to Isaac on Lyrical still draws it.
        plan = self.plan(isaac(self.f.doc), host=False)
        warnings = [i for i in plan["issues"] if i["code"] == "experimental_ros"]
        self.assertEqual([i["status"] for i in warnings], ["warning"])
        self.assertIn("Isaac documents Humble/Jazzy as tested", warnings[0]["message"])

    def test_preflight_messages_name_the_simulator(self):
        def changed(mutate):
            doc = copy.deepcopy(self.f.doc)
            mutate(doc)
            return doc
        cases = (
            ("clock", "error", changed(lambda d: d["session"].update(clock="wall"))),
            ("cross_distro", "warning", changed(lambda d: d["simulation"]["ros"].update(distro="jazzy"))),
            ("selection", "error", changed(lambda d: d["session"]["instances"].append("camera"))),
            ("authority", "error", changed(lambda d: d["simulation"]["publishes"].append(
                {"topic": "/planner/cmd", "type": "geometry_msgs/msg/Twist"}))),
        )
        for code, status, doc in cases:
            with self.subTest(code=code):
                plan = self.plan(doc, host=False)
                messages = [i["message"] for i in plan["issues"]
                            if i["code"] == code and i["status"] == status]
                self.assertEqual(len(messages), 1, plan["issues"])
                self.assertIn("worldsim", messages[0])
                self.assertFalse(says_isaac(plan["issues"]), plan["issues"])

    def test_an_unverified_or_malformed_probe_names_the_simulator(self):
        for response, code in (({"version": 1, "ready": False, "issues": []}, "probe"),
                               ({"version": 1, "ready": True, "issues": ["oops"]}, "host")):
            with self.subTest(code=code), self.host(probe=response):
                plan = self.plan()
                self.assertFalse(plan["ready_to_run"])
                messages = [i["message"] for i in plan["issues"]
                            if i["code"] == code and i["status"] == "error"]
                self.assertEqual(len(messages), 1, plan["issues"])
                self.assertIn("worldsim", messages[0])
                self.assertFalse(says_isaac(plan["issues"]), plan["issues"])


class ExternalStartFailures(ExternalSession):
    def lost_after(self, ready_probes):
        """Start with the simulator answering `ready_probes` probes and then gone."""
        lost = {"version": 1, "ready": False,
                "issues": [dict(status="error", code="clock", message="paused")]}
        observations = iter([READY] * ready_probes + [lost])
        with self.host(probe=lambda *args, **kwargs: next(observations)):
            manifest, descriptors, plan = self.f.build()
            self.assertTrue(plan["ready_to_run"], plan["issues"])
            with self.assertRaises(RigError) as raised:
                sim.execute(self.root, self.f.path, manifest, descriptors, plan)
        self.assertFalse(says_isaac(str(raised.exception)), str(raised.exception))
        return str(raised.exception)

    def failed_evidence(self):
        state = runtime.read_state(self.root)[1]
        self.assertEqual((state["provider"], state["status"]), ("external", "failed"))
        self.assert_no_isaac(Path(state["output_run"]) / ".rig/simulation")

    def test_a_connection_lost_before_launch_names_the_simulator(self):
        self.assertRegex(self.lost_after(1), "worldsim connection changed before launch: paused")
        self.assertEqual(self.f.calls, [])
        self.assertFalse(sim.active_session(self.root))

    def test_a_connection_lost_during_run_capture_names_the_simulator(self):
        self.assertRegex(self.lost_after(2), "worldsim connection changed during run capture")
        self.assertEqual(self.f.calls, [])
        self.failed_evidence()

    def test_a_graph_that_fails_after_startup_names_the_simulator(self):
        self.assertRegex(self.lost_after(3), "worldsim graph failed verification after startup: paused")
        self.assertEqual([(c["name"], c["verb"]) for c in self.f.calls],
                         [("bag_logger", "up"), ("planner", "up"), ("planner", "down"), ("bag_logger", "down")])
        self.failed_evidence()


class ExternalRun(ExternalSession):
    def test_the_run_and_the_session_are_recorded_as_external(self):
        with self.host():
            plan, state = self.start()
        run = Path(state["output_run"])
        self.assertEqual(state["provider"], "external")
        self.assertEqual(state["simulator"], SIMULATOR)
        self.assertEqual(state["status"], "running")
        recorded = yaml.safe_load((run / "manifest.yaml").read_text())
        self.assertNotIn("replay", recorded)
        self.assertEqual(recorded["simulation"], dict(
            provider="external", simulator=SIMULATOR, mode="attach", scene="scenarios/typical.yaml",
            recipe=self.f.doc, fingerprint=plan["fingerprint"], clock="external"))
        self.assertEqual(recorded["simulation"]["recipe"]["simulation"]["seed"], 7)
        evidence = run / ".rig/simulation"
        self.assertEqual(yaml.safe_load((evidence / "recipe.yaml").read_text()), self.f.doc)
        resolved = yaml.safe_load((evidence / "resolved.yaml").read_text())
        self.assertEqual((resolved["provider"], resolved["clock"], resolved["simulator"]),
                         ("external", "external", SIMULATOR))
        # The session's lifecycle record sits with the simulation evidence, not replay's.
        self.assertEqual(json.loads((evidence / "lifecycle.json").read_text()), state)
        self.assertFalse((run / ".rig/replay").exists())
        ups = [c for c in self.f.calls if c["verb"] == "up"]
        self.assertEqual([c["name"] for c in ups], ["bag_logger", "planner"])
        self.assertTrue(all(c["env"]["RIG_SIM_TIME"] == "1" for c in ups))
        self.assertIs(ups[0]["config"]["control"]["use_sim_time"], True)

    def test_nothing_in_the_runs_simulation_evidence_says_isaac(self):
        with self.host():
            _, state = self.start()
            run = Path(state["output_run"])
            written = self.assert_no_isaac(run / ".rig/simulation")
            self.assertLessEqual({"recipe.yaml", "resolved.yaml", "probe.json", "probe-launch.json",
                                  "probe-after.json", "lifecycle.json"}, written)
            self.assertFalse(says_isaac(self.state_path(state).read_text()))
            self.assertFalse(says_isaac((run / "manifest.yaml").read_text()))
            self.assertEqual(self.rig("sim", "down", "--end-run"), 0)
        # Teardown and sealing rewrite the lifecycle record and the run metadata.
        self.assert_no_isaac(run / ".rig/simulation")
        self.assertFalse(says_isaac(self.state_path(state).read_text()))
        self.assertFalse(says_isaac((run / "manifest.yaml").read_text()))

    def test_sim_down_and_bare_down_both_stop_an_external_session(self):
        for command in (["sim", "down", "--end-run"], ["down", "--end-run"]):
            with self.subTest(command=command), self.host() as err:
                _, state = self.start()
                self.f.calls.clear()
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(self.rig("status", "--format", "json"), 0, err.getvalue())
                self.assertEqual([r["sensor"] for r in json.loads(out.getvalue())["stacks"]],
                                 ["bag_logger", "planner"])
                self.assertEqual(self.rig(*command), 0, err.getvalue())
                self.assertEqual([c["name"] for c in self.f.calls if c["verb"] == "down"],
                                 ["planner", "bag_logger"])
                self.assertFalse(sim.active_session(self.root))
                self.assertIn("ended", yaml.safe_load((Path(state["output_run"]) / "manifest.yaml").read_text()))
                self.assertIn("external worldsim remains running", err.getvalue())
                self.assertFalse(says_isaac(err.getvalue()), err.getvalue())

    def test_another_providers_teardown_never_stops_an_external_session(self):
        with self.host():
            self.start()
            self.f.calls.clear()
            for provider in ("replay", "isaac"):
                with self.subTest(provider=provider), \
                        self.assertRaisesRegex(RigError, "Active session uses external"):
                    runtime.down(self.root, provider=provider)
        self.assertEqual(self.f.calls, [])
        self.assertTrue(sim.active_session(self.root))

    def test_an_external_session_that_lost_its_simulator_fails_closed(self):
        with self.host():
            _, state = self.start()
            self.f.calls.clear()
            for simulator in (None, "worldsim", {}, {"name": "worldsim"}, {"name": 7, "version": "abc1234"}):
                damaged = {k: v for k, v in state.items() if k != "simulator"}
                if simulator is not None:
                    damaged["simulator"] = simulator
                with self.subTest(simulator=simulator):
                    runtime.json_write(self.state_path(state), damaged)
                    with self.assertRaisesRegex(RigError, "Cannot read saved session"):
                        runtime.down(self.root)
            self.assertEqual(self.f.calls, [])
            runtime.json_write(self.state_path(state), state)
            self.assertEqual(runtime.down(self.root), 0)
        self.assertFalse(sim.active_session(self.root))


class IsaacUnchanged(Session):
    """The provider that came first keeps its records, its names and its wording."""

    def test_a_saved_isaac_session_still_loads_and_records_under_simulation(self):
        with self.host() as err:
            plan, state = self.start()
            run = Path(state["output_run"])
            self.assertEqual((plan["provider"], plan["clock"]), ("isaac", "isaac"))
            self.assertNotIn("simulator", plan)
            self.assertEqual(state["provider"], "isaac")
            self.assertEqual(set(state), ISAAC_STATE_KEYS)
            self.assertEqual(yaml.safe_load((run / "manifest.yaml").read_text())["simulation"], dict(
                provider="isaac", mode="attach", scene="dockyard.usd", recipe=self.f.doc,
                fingerprint=plan["fingerprint"], clock="isaac"))
            self.assertEqual(json.loads((run / ".rig/simulation/lifecycle.json").read_text()), state)
            self.assertFalse((run / ".rig/replay").exists())
            self.assertEqual(self.rig("sim", "down", "--end-run"), 0, err.getvalue())
        self.assertFalse(sim.active_session(self.root))
        self.assertIn("ended", yaml.safe_load((run / "manifest.yaml").read_text()))
        self.assertIn("rig: selected session services stopped; external Isaac remains running", err.getvalue())

    def test_isaac_messages_keep_their_wording(self):
        with self.host() as err:
            self.assertEqual(self.rig("sim", "check", "--recipe", "dockyard"), 0, err.getvalue())
        self.assertIn("rig sim: attach Isaac; selected logger, planner", err.getvalue())
        doc = copy.deepcopy(self.f.doc)
        doc["session"]["clock"] = "wall"
        self.assertIn("Isaac attach requires session.clock: source",
                      [i["message"] for i in self.plan(doc, host=False)["issues"]])

    def test_sim_down_still_refuses_a_replay_session(self):
        with self.host():
            _, state = self.start()
            self.f.calls.clear()
            runtime.json_write(self.state_path(state), {**state, "provider": "replay"})
            with self.assertRaisesRegex(RigError, "Active session uses replay; use rig down --end-run"):
                sim.down(self.root, end_run=True)
        self.assertEqual(self.f.calls, [])
        self.assertTrue(sim.active_session(self.root))

    def test_an_unknown_session_provider_is_still_unsupported(self):
        with self.assertRaisesRegex(RigError, "Unsupported session provider: gazebo"):
            runtime.register(self.root, None, [], {}, provider="gazebo", engine_id="fixture-engine",
                             launch_surfaces={})
        self.assertFalse(sim.active_session(self.root))


if __name__ == "__main__":
    unittest.main()
