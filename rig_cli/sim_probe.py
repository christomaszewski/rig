"""Read-only ROS graph/clock probe, also executable as ``python -c <this file>``.

The configured interpreter must have rclpy and rosgraph_msgs. Rig itself does not
import ROS. One JSON request arrives on stdin and one JSON result leaves stdout.
Endpoint discovery establishes connectivity, not the remote process's identity or
sensor fidelity. ROS_DISTRO describes this probe's sourced environment.

Endpoints on Iron and later carry a REP-2016 type hash of their message definition. Endpoints
of one topic that disagree never match, whatever their type names say, so any disagreement is
an error. When the simulator and the deployment run different ROS distros the request sets
``require_type_hashes``: the hash is then the only evidence of compatibility, so an endpoint
without one is an error too. ``await_counterparts`` (the check after the deployment starts) keeps
observing until every bound topic has endpoints on both sides, so the comparison is not passed
vacuously; a topic still one-sided at the timeout is reported as a warning.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sys
import time
import uuid


SHARED_TOPICS = frozenset(("/tf", "/tf_static", "/diagnostics"))
_TYPE_HASH = re.compile(r"RIHS\d{2}_[0-9a-f]{64}")


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {
            "ros", "publishes", "subscribes", "timeout_s", "require_type_hashes", "await_counterparts"}:
        raise ValueError("request must contain only ros, publishes, subscribes, timeout_s, "
                         "require_type_hashes and await_counterparts")
    for flag in ("require_type_hashes", "await_counterparts"):
        if not isinstance(request.get(flag, False), bool):
            raise ValueError(f"{flag} must be true or false")
    ros = request.get("ros")
    if not isinstance(ros, dict) or set(ros) != {"distro", "rmw", "domain_id"}:
        raise ValueError("ros requires distro, rmw and domain_id")
    if any(not isinstance(ros[k], str) or not re.fullmatch(r"[A-Za-z0-9_]+", ros[k])
           for k in ("distro", "rmw")):
        raise ValueError("ros.distro and ros.rmw must be nonempty identifiers")
    if type(ros["domain_id"]) is not int or not 0 <= ros["domain_id"] <= 232:
        raise ValueError("ros.domain_id must be an integer from 0 to 232")
    timeout = request.get("timeout_s", 10.0)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or not 0 < timeout <= 120):
        raise ValueError("timeout_s must be a finite number greater than 0 and at most 120")
    for direction in ("publishes", "subscribes"):
        entries = request.get(direction, [])
        if not isinstance(entries, list):
            raise ValueError(f"{direction} must be a list")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"topic", "type", "instance"}:
                raise ValueError(f"{direction} entries require topic and type")
            topic, typ = entry.get("topic"), entry.get("type")
            if not isinstance(topic, str) or not re.fullmatch(
                    r"(?:/[A-Za-z_][A-Za-z0-9_]*)+", topic):
                raise ValueError(f"{direction}: topic must be an exact absolute ROS topic name")
            if not isinstance(typ, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*/msg/[A-Za-z_][A-Za-z0-9_]*", typ):
                raise ValueError(f"{direction}: type must be a package/msg/Message name")
            if topic in seen:
                raise ValueError(f"{direction}: duplicate topic {topic}")
            if topic == "/clock" and typ != "rosgraph_msgs/msg/Clock":
                raise ValueError("/clock requires rosgraph_msgs/msg/Clock")
            if direction == "subscribes" and topic == "/clock":
                raise ValueError("simulation owns /clock; it cannot be a command subscription")
            if "instance" in entry and (not isinstance(entry["instance"], str) or
                    not re.fullmatch(r"[A-Za-z0-9_-]+", entry["instance"])):
                raise ValueError(f"{direction}: invalid instance name")
            seen.add(topic)
    return request


def evaluate(request, observation):
    """Evaluate observed graph evidence without importing ROS or changing anything."""
    validate_request(request)
    issues = []

    def error(code, message):
        issues.append(dict(status="error", code=code, message=message))

    ros = observation.get("ros", {})
    for key, expected in request["ros"].items():
        actual = ros.get(key)
        if actual != expected or (key == "domain_id" and type(actual) is not int):
            error("ros_" + key, f"Probe ROS {key} is {actual!r}; expected {expected!r}")
    clock = observation.get("clock", {})
    if clock.get("publishers") != 1 or clock.get("max_publishers", 0) > 1:
        error("clock_authority", "Expected exactly one /clock publisher; observed "
              f"{clock.get('publishers', 0)} (peak {clock.get('max_publishers', 0)})")
    if clock.get("reset", False):
        error("clock_reset", "/clock moved backwards during the probe; restart a coherent session")
    if (clock.get("advancing") is not True or clock.get("samples", 0) < 2 or
            not isinstance(clock.get("first_ns"), int) or
            not isinstance(clock.get("last_ns"), int) or clock["last_ns"] <= clock["first_ns"]):
        error("clock_stopped", "No advancing /clock received; enable Isaac's ROS bridge and press Play")
    age = clock.get("advance_age_s")
    if (not isinstance(age, (int, float)) or isinstance(age, bool) or
            not math.isfinite(age) or age < 0 or age > min(2.0, request.get("timeout_s", 10.0))):
        error("clock_stale", "No recent /clock advance; the simulator may be paused")
    topics = observation.get("topics", {})
    for direction, endpoint in (("publishes", "publishers"), ("subscribes", "subscribers")):
        for entry in request.get(direction, []):
            found = topics.get(entry["topic"], {}).get(endpoint, [])
            if not found:
                error("missing_endpoint", f"No {endpoint} discovered on {entry['topic']}")
            elif {item.get("type") for item in found} != {entry["type"]}:
                error("topic_type", f"{entry['topic']} {endpoint} have types "
                      f"{sorted(str(item.get('type')) for item in found)}; expected {entry['type']}")
    bindings = {e["topic"]: e["type"] for direction in ("publishes", "subscribes")
                for e in request.get(direction, [])}
    bindings["/clock"] = "rosgraph_msgs/msg/Clock"
    for topic, typ in bindings.items():
        endpoints = topics.get(topic, {})
        publishers, subscribers = endpoints.get("publishers", []), endpoints.get("subscribers", [])
        if topic not in SHARED_TOPICS and topic != "/clock" and len(publishers) > 1:
            error("topic_authority", f"Multiple publishers compete on {topic}: " +
                  ", ".join(p.get("node", "unknown node") for p in publishers))
        # The original binding checks its source direction. The opposite side
        # also needs the right type when it appears after rig starts consumers.
        wrong = [e for e in publishers + subscribers if e.get("type") != typ]
        if wrong:
            error("endpoint_type", f"{topic} has endpoints with incompatible message types; expected {typ}")
        by_hash = {}
        for e in publishers + subscribers:
            by_hash.setdefault(e.get("type_hash"), []).append(e.get("node", "unknown node"))
        known = sorted(h for h in by_hash if h)
        if len(known) > 1:
            error("type_hash", f"{topic}: endpoints disagree on the {typ} definition (REP-2016 hash " +
                  "; ".join(f"{h[:15]}… at {', '.join(by_hash[h])}" for h in known) +
                  "); the simulator and the deployment need the same message definitions")
        elif request.get("require_type_hashes") and None in by_hash:
            error("type_hash_unknown", f"{topic}: {', '.join(by_hash[None])} report no REP-2016 type "
                  "hash, so a cross-distro binding cannot be verified (Humble and older have none)")
        pairs = endpoints.get("qos_checks", [])
        if len(pairs) != len(publishers) * len(subscribers):
            error("qos_evidence", f"{topic}: missing QoS compatibility evidence for discovered endpoint pairs")
        for pair in pairs:
            status = pair.get("status")
            message = (f"{topic}: {pair.get('publisher', 'unknown publisher')} -> "
                       f"{pair.get('subscriber', 'unknown subscriber')}: {pair.get('reason') or status}")
            if status == "error":
                error("qos_incompatible", message)
            elif status == "warn":
                issues.append(dict(status="warn", code="qos_unknown", message=message))
            elif status != "ok":
                error("qos_evidence", message)
    if request.get("await_counterparts"):
        for direction, other in (("publishes", "subscribers"), ("subscribes", "publishers")):
            for entry in request.get(direction, []):
                if not topics.get(entry["topic"], {}).get(other):
                    issues.append(dict(status="warn", code="type_hash_unverified", message=(
                        f"{entry['topic']}: no deployment {other[:-1]} discovered, so its type hash "
                        "was not compared")))
    if not issues:
        issues.append(dict(status="ok", code="connected",
                           message="Expected ROS endpoints and one advancing simulation clock observed"))
    return dict(version=1, ready=not any(i["status"] == "error" for i in issues),
                issues=issues, ros=ros, clock=clock, topics=topics)


def _type_hash(info):
    """The endpoint's REP-2016 type hash (``RIHS01_<hex>``), or None: rclpy before Iron has
    none, and version 0 means the middleware left it unset."""
    value = getattr(info, "topic_type_hash", None)
    if value is None or getattr(value, "version", 1) == 0:
        return None
    text = str(value)
    return text if _TYPE_HASH.fullmatch(text) else None


def _endpoint(info):
    qos = info.qos_profile
    values = {}
    for name in ("reliability", "durability", "history", "liveliness"):
        policy = getattr(qos, name, None)
        values[name] = getattr(policy, "name", str(policy))
    values["depth"] = qos.depth
    for name in ("deadline", "lifespan", "liveliness_lease_duration"):
        duration = getattr(qos, name, None)
        values[name + "_ns"] = getattr(duration, "nanoseconds", None)
    return dict(node=(info.node_namespace.rstrip("/") + "/" + info.node_name),
                type=info.topic_type, type_hash=_type_hash(info), gid=list(info.endpoint_gid), qos=values)


def _qos_check(publisher, subscriber, checker):
    """Use the active RMW's own request/offered check, including duration policies."""
    try:
        compatibility, reason = checker(publisher.qos_profile, subscriber.qos_profile)
        status = {"OK": "ok", "WARNING": "warn", "ERROR": "error"}.get(
            getattr(compatibility, "name", None), "error")
    except Exception as exc:
        status, reason = "error", f"QoS compatibility inspection failed: {exc}"
    return dict(publisher=publisher.node_namespace.rstrip("/") + "/" + publisher.node_name,
                subscriber=subscriber.node_namespace.rstrip("/") + "/" + subscriber.node_name,
                publisher_gid=list(publisher.endpoint_gid), subscriber_gid=list(subscriber.endpoint_gid),
                status=status, reason=reason)


def observe(request):
    """Inspect the sourced ROS environment for a bounded period; never publish commands."""
    validate_request(request)
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import (QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy,
                          qos_check_compatible)
    from rclpy.utilities import get_rmw_implementation_identifier
    from rosgraph_msgs.msg import Clock

    context, node, executor = Context(), None, None
    try:
        # Do not force the expected domain/RMW here: inspect what the environment
        # actually selected, so a misconfigured probe cannot silently pass.
        rclpy.init(args=[], context=context)
        observed = dict(ros=dict(distro=os.environ.get("ROS_DISTRO"),
                                 rmw=get_rmw_implementation_identifier(),
                                 domain_id=context.get_domain_id()),
                        clock=dict(publishers=0, max_publishers=0, samples=0,
                                   first_ns=None, last_ns=None, advancing=False, reset=False,
                                   advance_age_s=None),
                        topics={})
        if observed["ros"] != request["ros"]:
            return observed
        node = rclpy.create_node("_rig_sim_probe_" + uuid.uuid4().hex[:12],
                                 namespace="/rig/probe", context=context,
                                 enable_rosout=False, start_parameter_services=False,
                                 use_global_arguments=False)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        clock = observed["clock"]
        last_advance = [None]

        def on_clock(message):
            stamp = message.clock.sec * 1_000_000_000 + message.clock.nanosec
            if clock["last_ns"] is not None:
                clock["reset"] |= stamp < clock["last_ns"]
                clock["advancing"] |= stamp > clock["last_ns"]
                if stamp > clock["last_ns"]:
                    last_advance[0] = time.monotonic()
            else:
                clock["first_ns"] = stamp
            clock["last_ns"] = stamp
            clock["samples"] += 1

        # A best-effort/volatile reader can connect to reliable or best-effort
        # publishers, and requires fresh data rather than an old retained clock.
        node.create_subscription(Clock, "/clock", on_clock, QoSProfile(
            depth=10, history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE))
        names = sorted({"/clock"} | {e["topic"] for direction in ("publishes", "subscribes")
                                    for e in request.get(direction, [])})
        started = time.monotonic()
        timeout = request.get("timeout_s", 10.0)
        deadline = started + timeout
        next_snapshot = started
        while context.ok():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            executor.spin_once(timeout_sec=min(0.1, remaining))
            now = time.monotonic()
            if now < next_snapshot and now < deadline:
                continue
            next_snapshot = now + 0.1
            for topic in names:
                pubs = node.get_publishers_info_by_topic(topic)
                subs = node.get_subscriptions_info_by_topic(topic)
                observed["topics"][topic] = dict(
                    publishers=[_endpoint(e) for e in pubs], subscribers=[_endpoint(e) for e in subs],
                    qos_checks=[_qos_check(pub, sub, qos_check_compatible) for pub in pubs for sub in subs])
            clock["publishers"] = len(observed["topics"]["/clock"]["publishers"])
            clock["max_publishers"] = max(clock["publishers"], clock["max_publishers"])
            clock["advance_age_s"] = None if last_advance[0] is None else now - last_advance[0]
            # Allow graph discovery to settle before accepting the first match. A one-sided
            # binding keeps the observation going (await_counterparts) until the timeout.
            if now - started >= min(1.0, timeout):
                result = evaluate(request, observed)
                if result["ready"] and not any(i["code"] == "type_hash_unverified"
                                               for i in result["issues"]):
                    break
        return observed
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        context.try_shutdown()


def main():
    try:
        request = validate_request(json.load(sys.stdin))
    except (ValueError, TypeError) as exc:
        result = dict(version=1, ready=False,
                      issues=[dict(status="error", code="request", message=str(exc))])
    else:
        old_log_stream = os.environ.get("RCUTILS_LOGGING_USE_STDOUT")
        try:
            # Keep stdout a machine-readable protocol even if a Python ROS import
            # prints a diagnostic or the caller routed native ROS logs to stdout.
            os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"
            with contextlib.redirect_stdout(sys.stderr):
                result = evaluate(request, observe(request))
        except ImportError as exc:
            result = dict(version=1, ready=False, issues=[dict(status="error", code="ros_unavailable",
                          message=f"Probe interpreter needs sourced ROS with rclpy and rosgraph_msgs: {exc}")])
        except Exception as exc:
            result = dict(version=1, ready=False, issues=[dict(status="error", code="probe",
                          message=f"ROS probe failed: {exc}")])
        finally:
            if old_log_stream is None:
                os.environ.pop("RCUTILS_LOGGING_USE_STDOUT", None)
            else:
                os.environ["RCUTILS_LOGGING_USE_STDOUT"] = old_log_stream
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
