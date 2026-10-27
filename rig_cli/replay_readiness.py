"""Derive explicit consumer endpoints for opt-in coordinated replay.

Recorded graph nodes are evidence, not liveness. The runtime probe must observe these
exact endpoints before release; missing evidence makes a consumer unsupported.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from . import RigError
from .common import load_yaml
from .descriptor import resolve_ros_namespace


def _type(types, kind):
    values = set(types)
    return next(iter(values)) if len(values) == 1 and re.fullmatch(
        rf"[A-Za-z_][A-Za-z_0-9]*/{kind}/[A-Za-z_][A-Za-z_0-9]*", next(iter(values))) else None


def _node(model, edge, row, desc):
    owner = edge.get("owner")
    if owner is None or edge.get("possible_owners") != [owner]:
        return None
    if model.get("owners", {}).get(owner, {}).get("selected_instances") != [row.name]:
        return None
    old = model.get("namespaces", {}).get(owner, "/" + owner).rstrip("/")
    node = edge["node"]
    if not old or not node.startswith(old + "/"):
        return None
    return resolve_ros_namespace(desc, row.name).rstrip("/") + node[len(old):]


def _transport(instance, config):
    """Only explicit TCP listeners; do not guess defaults in an arbitrary router image."""
    rc = config.get("router_config") or {}
    listen = rc.get("listen", {}) if isinstance(rc, dict) else {}
    endpoints = listen.get("endpoints") if isinstance(listen, dict) else None
    if not isinstance(endpoints, list) or not endpoints:
        raise ValueError("Set router_config.listen.endpoints explicitly for coordinated transport readiness")
    result = []
    for endpoint in endpoints:
        match = re.fullmatch(r"tcp/(?:\[([^\]]+)\]|([^:/#?]+)):(\d+)", str(endpoint))
        if not match:
            raise ValueError(f"Unsupported router readiness endpoint {endpoint!r}; an explicit TCP IP listener is required")
        host, port = match[1] or match[2], int(match[3])
        host = {"0.0.0.0": "127.0.0.1", "::": "::1", "localhost": "127.0.0.1"}.get(host, host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Router readiness requires a literal IP; DNS resolution has no bounded socket timeout") from exc
        if address.is_multicast or address.is_unspecified:
            raise ValueError("Router readiness requires a reachable unicast TCP listener")
        if not 1 <= port <= 65535:
            raise ValueError("Router readiness port must be between 1 and 65535")
        result.append(dict(instance=instance, host=host, port=port))
    return result


def requirements(plan, manifest, descriptors, rows):
    """Return the v1 probe document; callers surface `unsupported` in coordinated mode.

    Call again after request inspection filters playback.services. This function reads
    the already resolved configs and a validated call script, never starts a launcher.
    Producer-only rows need no consumer proof unless a relevant subscriber is observed.
    A declared interface alone does not identify its actual node, so cannot replace the
    recorded graph. The player and MAVLink router are never readiness evidence.
    """
    del manifest  # The plan already contains layered configuration and replay selection.
    model = plan.get("ownership") or {}
    selected = {row.name: row for row in rows if row.name in plan.get("selected", [])}
    result = dict(version=1, subscriptions=[], services=[], transports=[],
                  required_instances=[], unsupported=[])

    def unsupported(instance, reason):
        record = dict(instance=instance, reason=reason)
        if record not in result["unsupported"]:
            result["unsupported"].append(record)

    for name, row in sorted(selected.items()):
        if name == plan.get("player"):
            continue
        role = plan.get("roles", {}).get(name, {}).get("role", "consumer")
        if row.service == "mavlink-router":
            unsupported(name, "MAVLink has no coordinated readiness contract")
        elif row.service == "zenoh-router":
            result["required_instances"].append(name)
            try:
                result["transports"].extend(_transport(name, plan.get("configs", {}).get(name, {})))
            except ValueError as exc:
                unsupported(name, str(exc))
        elif role in ("consumer", "infra"):
            result["required_instances"].append(name)

    playback = plan.get("playback", {})
    bag_topics = ((plan.get("source") or {}).get("bag") or {}).get("topics", {})
    sources = {}
    for topic in playback.get("topics", []):
        destination = playback.get("remap", {}).get(topic, topic)
        sources.setdefault((topic, destination), set()).add(bag_topics.get(topic, {}).get("type"))
    # Recording/live producers can feed consumers even when their outputs are omitted
    # from the bag player. This also permits the camera's own bridge subscription proof.
    producer_names = {n for n in selected if plan.get("roles", {}).get(n, {}).get("role")
                      in ("recording-source", "live-device")}
    for topic, entry in model.get("topics", {}).items():
        for edge in entry.get("publishers", []):
            owner = model.get("owners", {}).get(edge.get("owner"), {})
            if producer_names.intersection(owner.get("selected_instances", [])):
                sources.setdefault((topic, topic), set()).update(edge["types"])

    for (source, destination), offered_types in sorted(sources.items()):
        edges = model.get("topics", {}).get(source, {}).get("subscribers", [])
        if destination != source:
            edges = edges + model.get("topics", {}).get(destination, {}).get("subscribers", [])
        for edge in edges:
            owner = model.get("owners", {}).get(edge.get("owner"), {})
            for name in owner.get("selected_instances", []):
                if name not in selected or name == plan.get("player"):
                    continue
                row = selected[name]
                node = _node(model, edge, row, descriptors.get(row.service))
                typ = _type(edge.get("types", []), "msg")
                if not node or not typ:
                    unsupported(name, f"Ambiguous node or message type for subscription {destination}")
                elif offered_types != {typ}:
                    unsupported(name, f"Cannot establish matching publisher/subscriber type for {destination}")
                else:
                    result["subscriptions"].append(dict(instance=name, topic=destination, type=typ, node=node))

    calls = {name: set() for name in playback.get("services", [])}
    inspection = ((plan.get("call_inspection") or {}).get("result") or {}).get("services", {})
    for name in calls:
        if inspection.get(name, {}).get("type"):
            calls[name].add(inspection[name]["type"])
    if playback.get("calls"):
        try:
            doc = load_yaml(Path(playback["calls"]))
            start, end = plan.get("window", {}).get("start", 0), plan.get("window", {}).get("end")
            for call in doc["calls"]:
                if call["t"] >= start and (end is None or call["t"] < end):
                    calls.setdefault(call["service"], set()).add(call["type"])
        except (RigError, OSError, KeyError, TypeError, ValueError) as exc:
            unsupported(plan.get("player") or "player", f"Cannot derive scripted service readiness: {exc}")
    for service, requested_types in sorted(calls.items()):
        edges = model.get("services", {}).get(service, {}).get("servers", [])
        owners = {edge.get("owner") for edge in edges}
        if not edges or None in owners or len(owners) != 1:
            unsupported(plan.get("player") or "player", f"No unique recorded server owner for {service}")
            continue
        owner = model.get("owners", {}).get(next(iter(owners)), {})
        instances = owner.get("selected_instances", [])
        if len(instances) != 1 or instances[0] not in selected:
            unsupported(plan.get("player") or "player", f"No unique selected service instance for {service}")
            continue
        name, row = instances[0], selected[instances[0]]
        for edge in edges:
            node = _node(model, edge, row, descriptors.get(row.service))
            typ = _type(edge.get("types", []), "srv")
            if not node or not typ or (requested_types and requested_types != {typ}):
                unsupported(name, f"Ambiguous or incompatible server node/type for {service}")
            else:
                result["services"].append(dict(instance=name, service=service, type=typ, node=node))

    covered = {entry["instance"] for key in ("subscriptions", "services", "transports") for entry in result[key]}
    for name in result["required_instances"]:
        if name not in covered:
            unsupported(name, "No meaningful recorded consumer endpoint or explicit transport probe; coordinated readiness is unsupported")
    for key in ("subscriptions", "services", "transports"):
        result[key] = [dict(items) for items in sorted({tuple(sorted(entry.items())) for entry in result[key]})]
    return result
