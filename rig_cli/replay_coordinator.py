"""Session-scoped replay startup, without ROS dependencies.

Participants own their readiness evidence; Rig owns the single immutable release.
The shared directory is private to one invocation and never reused on retry.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from . import RigError
from .common import eprint
from .manifest import project_name

VERSION = 1
FRESHNESS_S = 2.0
RELEASE_MARGIN_S = 2.0
COMMIT_MARGIN_S = 0.3
TERMINAL = {"completed", "failed", "aborted"}


def configure_plan(plan, manifest, descriptors, rows, options, capability_probe):
    """Require the complete producer contract before acquiring or starting anything."""
    startup = plan["startup"] = dict(mode=options.get("startup", "legacy"),
        timeout_s=options.get("startup_timeout", 300.0),
        ready_timeout_s=options.get("ready_timeout", 120.0), participants=[], image_labels={})
    if startup["mode"] == "legacy":
        return
    def issue(message):
        plan["issues"].append(dict(status="error", code="coordination", phase="run", message=message))
    if startup["mode"] != "coordinated":
        issue("Unknown replay startup mode")
        return
    for key in ("timeout_s", "ready_timeout_s"):
        value = startup[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not 0 < value <= 3600):
            issue("Coordinated startup timeouts must be finite and between 0 and 3600 seconds")
            return
    if not plan.get("player"):
        issue("Coordinated startup requires a selected bag player for ROS endpoint readiness")
    for row in rows:
        components = []
        if row.name == plan.get("player"):
            caps = plan.get("capabilities", {})
            components = ["player", "readiness"]
            if plan["playback"].get("calls"):
                components.append("calls")
        elif row.name in plan.get("recordings", {}):
            try:
                caps = capability_probe(row, descriptors[row.service])
            except RigError as exc:
                issue(f"{row.name}: {exc}")
                continue
            components = ["camera"]
        else:
            continue
        contract = caps.get("coordination", {})
        available = contract.get("components") if isinstance(contract, dict) else None
        if (not isinstance(contract, dict) or type(contract.get("version")) is not int or
                contract.get("version") != VERSION or
                not isinstance(available, list) or any(not isinstance(v, str) for v in available) or
                not set(components) <= set(available)):
            issue(f"{row.name}: coordinated startup v1 is unsupported; update its package and images "
                  "or explicitly use --startup legacy")
            continue
        labels = contract.get("image_labels")
        if (not isinstance(labels, dict) or not labels or any(
                not isinstance(role, str) or not role or not isinstance(values, dict) or
                values.get("io.rig.replay.coordination") != "1" or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())
                for role, values in labels.items())):
            issue(f"{row.name}: coordination requires labels on the actual running image")
            continue
        startup["image_labels"][row.name] = labels
        startup["participants"].extend(row.name + "." + component for component in components)


def refresh_requirements(plan, manifest, descriptors, rows):
    if plan.get("startup", {}).get("mode") != "coordinated":
        return
    from .replay_readiness import requirements
    plan["issues"][:] = [i for i in plan["issues"] if i["code"] != "coordination_readiness"]
    request = plan["startup"]["requirements"] = requirements(plan, manifest, descriptors, rows)
    # Pending event inspection can still remove candidate services. Only final call
    # selection may turn their missing readiness evidence into a launch-blocking error.
    pending = any(i["code"] == "recorded_calls" and i["status"] == "not_checked" for i in plan["issues"])
    for entry in request.get("unsupported", []):
        plan["issues"].append(dict(status="not_checked" if pending else "error",
            code="coordination_readiness", phase="run",
            message=f"{entry['instance']}: {entry['reason']}; use --startup legacy until readiness is supported"))


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)  # Container USER may differ from the host coordinator UID.
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def project_locks(manifest, names, env):
    """Serialize cooperating Rig invocations even across deployment directories."""
    del env  # Different Docker context/host aliases can address the same daemon.
    import fcntl
    directory = Path(tempfile.gettempdir()) / f"rig-replay-locks-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RigError(f"Unsafe replay lock directory: {directory}")
    handles = []
    try:
        for name in sorted(names):
            project = project_name(name, manifest.vehicle_id)
            key = hashlib.sha256(project.encode()).hexdigest()
            fd = os.open(directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = os.fdopen(fd, "a")
            handles.append(stream)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RigError(f"Another replay is starting project {project}") from exc
        yield
    finally:
        for stream in reversed(handles):
            stream.close()


class Coordinator:
    def __init__(self, directory, *, session_id, plan_id, participants, requirements,
                 timeout_s=300, ready_timeout_s=120, now=time.time, sleep=time.sleep,
                 log=eprint, health=None):
        self.directory = Path(directory)
        self.identity = dict(version=VERSION, session_id=session_id, plan_id=plan_id)
        if (not re.fullmatch(r"[0-9a-f]{32}", session_id) or
                not re.fullmatch(r"[0-9a-f]{64}", plan_id)):
            raise RigError("Invalid replay session or resolved plan identity")
        if (not participants or len(participants) != len(set(participants)) or
                any(not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z_]+", p) for p in participants)):
            raise RigError("Invalid replay participant set")
        self.participants = list(participants)
        self.now, self.sleep, self.log = now, sleep, log
        self.deadline = now() + timeout_s
        self.ready_timeout_s = ready_timeout_s
        self.launched = {}
        self.health = health or (lambda: None)
        self.seq = 0
        self.release_at = None
        self.phase = "prepare"
        self.phase_started_at = now()
        self.statuses = {}
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "statuses").mkdir()
        # Status writers may run as a different image USER. Sticky bit prevents
        # another UID replacing an existing participant's file; commands stay host-owned.
        (self.directory / "statuses").chmod(0o1777)
        atomic_json(self.directory / "session.json", dict(self.identity,
            participants=self.participants, startup_deadline_unix_s=self.deadline,
            ready_timeout_s=ready_timeout_s))
        atomic_json(self.directory / "requirements.json", dict(requirements, **self.identity))
        self.command("hold")

    def launched_instance(self, instance):
        for participant in self.participants:
            if participant.rsplit(".", 1)[0] == instance:
                self.launched[participant] = self.now()

    def remaining(self):
        remaining = self.deadline - self.now()
        if remaining <= 0:
            raise RigError(f"Replay startup timed out during {self.phase}")
        return remaining

    def command(self, op):
        self.seq += 1
        command = dict(self.identity, seq=self.seq, op=op)
        if self.release_at is not None:
            command["release_at_unix_s"] = self.release_at
        self.phase_started_at = self.now()
        atomic_json(self.directory / "command.json", command)
        self.phase = op

    def read_statuses(self):
        result = {}
        for participant in self.participants:
            path = self.directory / "statuses" / (participant + ".json")
            try:
                value = json.loads(path.read_text())
            except FileNotFoundError:
                # Some shared Docker filesystems briefly hide an atomic replacement.
                # A previously observed status gets a bounded retry; never synthesize
                # readiness from the old document while the current file is absent.
                value = None
                if participant in self.statuses:
                    for _ in range(10):
                        self.sleep(0.02)
                        try:
                            value = json.loads(path.read_text())
                            break
                        except FileNotFoundError:
                            pass
                        except (ValueError, OSError) as exc:
                            raise RigError(f"{participant}: unreadable replay status: {exc}") from exc
                if value is None:
                    continue
            except (ValueError, OSError) as exc:
                raise RigError(f"{participant}: unreadable replay status: {exc}") from exc
            if not isinstance(value, dict) or any(value.get(k) != v for k, v in self.identity.items()):
                raise RigError(f"{participant}: stale or foreign replay status")
            stamp = value.get("updated_at_unix_s")
            if (value.get("participant") != participant or isinstance(stamp, bool) or
                    not isinstance(stamp, (int, float)) or not math.isfinite(stamp)):
                raise RigError(f"{participant}: malformed replay status")
            state = value.get("state")
            if state not in {"preparing", "held", "ready", "release_ack", "running", *TERMINAL}:
                raise RigError(f"{participant}: unknown replay state {state!r}")
            if state in {"failed", "aborted"}:
                raise RigError(f"{participant}: {state} during {self.phase}: {value.get('error', 'no detail')}")
            if (state not in TERMINAL and self.now() - stamp > FRESHNESS_S) or stamp > self.now() + FRESHNESS_S:
                raise RigError(f"{participant}: readiness heartbeat lost during {self.phase}")
            result[participant] = value
        self.statuses = result
        return result

    def wait(self, accepted, *, deadline=None):
        deadline = min(self.deadline, deadline or self.deadline)
        last_pending = None
        while True:
            self.health()
            statuses = self.read_statuses()
            if self.now() >= deadline:
                raise RigError(f"Replay {self.phase} timed out at its startup deadline")
            pending = [p for p in self.participants if statuses.get(p, {}).get("state") not in accepted]
            if self.phase in ("hold", "arm"):
                for participant in self.participants:
                    status = statuses.get(participant, {})
                    origin = self.launched.get(participant, self.phase_started_at) if self.phase == "hold" else self.phase_started_at
                    field = "held_at_unix_s" if self.phase == "hold" else "ready_at_unix_s"
                    stamp = status.get(field)
                    if participant not in pending:
                        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or
                                not math.isfinite(stamp) or stamp < origin or stamp > self.now() + FRESHNESS_S):
                            raise RigError(f"{participant}: missing or invalid {field} evidence")
                        elapsed = stamp - origin
                    else:
                        elapsed = self.now() - origin
                    if elapsed >= self.ready_timeout_s:
                        raise RigError(f"{participant}: timed out during {self.phase} ({status.get('state', 'no status')})")
            for participant in pending:
                state = statuses.get(participant, {}).get("state")
                if state in {"running", "completed"}:
                    raise RigError(f"{participant}: released before coordinated commit")
            if not pending:
                return statuses
            if pending != last_pending:
                self.log(f"rig replay: {self.phase}; waiting for " + ", ".join(pending))
                last_pending = pending
            if self.now() >= deadline:
                raise RigError(f"Replay {self.phase} timed out waiting for " + ", ".join(pending))
            self.sleep(min(0.1, max(0, deadline - self.now())))

    def release(self, settling_s=0):
        self.wait({"held", "ready"})
        self.command("arm")
        self.wait({"ready"})
        ready = self.now()
        settling_end = ready + settling_s
        self.log(f"rig replay: all participants ready; settling for {settling_s:g}s")
        while self.now() < settling_end:
            self.remaining()
            self.health()
            statuses = self.read_statuses()
            if any(statuses.get(p, {}).get("state") != "ready" for p in self.participants):
                raise RigError("Replay lost readiness during the settling interval")
            self.sleep(min(0.1, settling_end - self.now()))
        self.release_at = self.now() + RELEASE_MARGIN_S
        if self.release_at >= self.deadline:
            raise RigError("Insufficient startup time remains to commit a replay release")
        self.command("prepare_release")
        statuses = self.wait({"release_ack"}, deadline=self.release_at - COMMIT_MARGIN_S)
        if any(s.get("release_at_unix_s") != self.release_at for s in statuses.values()):
            raise RigError("Replay participant acknowledged a different release instant")
        self.health()
        # Acknowledgements can be withdrawn while another participant is preparing.
        statuses = self.read_statuses()
        if (any(statuses.get(p, {}).get("state") != "release_ack" for p in self.participants)
                or self.now() >= self.release_at - COMMIT_MARGIN_S):
            raise RigError("Replay lost readiness or release margin before commit")
        self.command("commit")
        self.log(f"rig replay: committed common release at {self.release_at:.6f}")
        self.wait({"running", "completed"})
        for participant, status in self.statuses.items():
            actual = status.get("actual_release_at_unix_s")
            if (not isinstance(actual, (int, float)) or isinstance(actual, bool) or
                    not math.isfinite(actual) or actual < self.release_at):
                raise RigError(f"{participant}: missing or premature release evidence")
        return dict(ready_at_unix_s=ready, start_at_unix_s=self.release_at,
                    participants=self.statuses, settling_s=settling_s)

    def abort(self):
        self.command("abort")
