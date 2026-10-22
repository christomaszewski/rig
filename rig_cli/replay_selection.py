"""Pure, evidence-based replay topic and recorded-call selection.

All historical owners delimit node namespaces, including instances omitted from replay.
Namespaces attribute observed nodes; they never prove an unobserved topic or service owner.
Callers apply explicit topic keeps/excludes/remaps and validate recorded request contents.
"""
from __future__ import annotations

from . import graph


SHARED_TOPICS = frozenset(("/tf", "/tf_static", "/diagnostics"))


def ownership(epochs, known_names, *, namespaces=None, aliases=None, selected=()):
    """Build JSON-safe ownership evidence from valid epochs (or an existing GraphUnion).

    `aliases` maps current instance names to recorded identities. `namespaces` maps
    recorded identities to historical ROS namespaces. `selected` is the entire started
    instance set, without the replay harness/player. The default scope is /<identity>.
    Missing/malformed epochs are handled by graph.load_epochs before calling this helper;
    its retained union is useful evidence, not a claim that every publisher was observed.
    """
    observed = epochs if isinstance(epochs, graph.GraphUnion) else graph.union(list(epochs))
    selected = sorted(set(selected))
    aliases = dict(aliases or {})
    for name in selected:
        aliases.setdefault(name, name)
    namespaces = dict(namespaces or {})
    identities = sorted(set(known_names) | set(aliases.values()) | set(namespaces))
    scopes = {identity: namespaces.get(identity, "/" + identity).rstrip("/")
              for identity in identities}
    started = {identity: sorted(name for name in selected if aliases[name] == identity)
               for identity in identities}
    nodes, topics, services = {}, {}, {}
    owner_nodes = {identity: [] for identity in identities}
    for node in sorted(observed.nodes):
        matches = [(len(scope), identity) for identity, scope in scopes.items()
                   if node == scope or node.startswith(scope + "/")]
        longest = max((length for length, _ in matches), default=-1)
        possible = sorted(identity for length, identity in matches if length == longest)
        owner = possible[0] if len(possible) == 1 else None
        nodes[node] = dict(owner=owner, possible_owners=possible,
                           reason="observed" if owner else "ambiguous_namespace" if possible else "unassigned")
        if owner:
            owner_nodes[owner].append(node)
        edges = {}
        for edge in observed.nodes[node]:
            if not graph.is_plumbing(edge):
                edges.setdefault((edge.kind, edge.name), set()).add(edge.type)
        for (kind, name), types in sorted(edges.items()):
            target = topics if kind in ("pubs", "subs") else services
            groups = ("publishers", "subscribers") if target is topics else ("servers", "clients")
            entry = target.setdefault(name, {key: [] for key in groups})
            group = {"pubs": "publishers", "subs": "subscribers",
                     "provides": "servers", "requires": "clients"}[kind]
            entry[group].append(dict(node=node, types=sorted(types), **nodes[node]))
    return dict(version=1, epochs=observed.epochs, graph_available=bool(observed.epochs),
                selected=selected, aliases=aliases,
                namespaces={name: scopes[name] or "/" for name in identities},
                owners={name: dict(nodes=owner_nodes[name], selected_instances=started[name])
                        for name in identities}, nodes=nodes, topics=topics, services=services,
                selected_without_nodes=[name for name in selected if not owner_nodes[aliases[name]]],
                limitations=["Ownership describes retained graph epochs; unobserved publishers, "
                             "servers, or clients remain unknown."] if observed.epochs else
                            ["No valid recorded graph; namespace-only ownership inference is disabled."])


def _evidence(model, nodes):
    owners = sorted({item["owner"] for item in nodes if item["owner"] is not None})
    return dict(owners=owners,
                owner_nodes={owner: [item["node"] for item in nodes if item["owner"] == owner]
                             for owner in owners},
                selected_instances=sorted({name for owner in owners
                    for name in model["owners"][owner]["selected_instances"]}),
                unselected_owners=[owner for owner in owners
                                   if not model["owners"][owner]["selected_instances"]],
                unassigned_nodes=[item["node"] for item in nodes if not item["possible_owners"]],
                ambiguous_nodes={item["node"]: item["possible_owners"] for item in nodes
                                 if len(item["possible_owners"]) > 1},
                types=sorted({kind for item in nodes for kind in item["types"]}))


def topic_selection(model, topics):
    """Return suppressible regenerated outputs and a reason for every supplied topic.

    Multiple known publishers are safe to suppress only when all owners are selected.
    Shared transform/diagnostic topics stay eligible even then; explicit exclusion belongs
    to the caller and can override that policy. No prefix heuristic is used without nodes.
    """
    records, suppressed = [], []
    for topic in sorted(set(topics)):
        publishers = model["topics"].get(topic, {}).get("publishers", [])
        evidence = _evidence(model, publishers)
        if topic in SHARED_TOPICS:
            reason = "shared_topic"
        elif not model["graph_available"]:
            reason = "no_graph"
        elif not publishers:
            reason = "unknown_publishers"
        elif evidence["ambiguous_nodes"]:
            reason = "ambiguous_publishers"
        elif evidence["unassigned_nodes"]:
            reason = "unassigned_publishers"
        elif evidence["unselected_owners"]:
            reason = "shared_publishers" if evidence["selected_instances"] else "producers_not_selected"
        else:
            reason = "regenerated"
            suppressed.append(topic)
        records.append(dict(topic=topic, decision="suppress" if reason == "regenerated" else "keep",
                            reason=reason, selected_producers=evidence.pop("selected_instances"),
                            publisher_nodes=[item["node"] for item in publishers], **evidence))
    return dict(suppressed=suppressed, records=records)


def service_selection(model, services, *, mode="auto", manual=None):
    """Select candidate recorded ROS calls; request-content validation is a later step.

    Automatic calls require one unambiguous historical server owner mapped to exactly one
    started instance. Any positively identified started client excludes automatic injection
    to avoid regenerating and replaying the same request. Unassigned client evidence is
    reported as uncertainty, not proof that a selected instance will regenerate calls.
    Manual names override ownership inference, with warnings retaining the auto-mode reason.
    `mode='manual', manual=[]` explicitly disables recorded calls.
    """
    if mode not in ("auto", "manual"):
        raise ValueError("service selection mode must be auto or manual")
    requested = set(manual or ()) if mode == "manual" else set()
    candidates, records = [], []
    for service in sorted(set(services) | requested):
        entry = model["services"].get(service, {})
        servers = entry.get("servers", [])
        clients = entry.get("clients", [])
        server = _evidence(model, servers)
        client = _evidence(model, clients)
        if not model["graph_available"]:
            automatic = "no_graph"
        elif not servers:
            automatic = "unknown_servers"
        elif server["ambiguous_nodes"]:
            automatic = "ambiguous_server_namespace"
        elif server["unassigned_nodes"]:
            automatic = "unassigned_servers"
        elif len(server["owners"]) != 1:
            automatic = "multiple_server_owners"
        elif not server["selected_instances"]:
            automatic = "server_not_selected"
        elif len(server["selected_instances"]) != 1:
            automatic = "ambiguous_started_servers"
        elif client["selected_instances"] or any(
                model["owners"][owner]["selected_instances"]
                for owners in client["ambiguous_nodes"].values() for owner in owners):
            # A namespace tie involving a started client cannot establish absence of a
            # live caller. Avoid automatic injection until the binding is disambiguated.
            automatic = "selected_live_client" if client["selected_instances"] else "ambiguous_started_clients"
        else:
            automatic = "selected_server"
        include = automatic == "selected_server" if mode == "auto" else service in requested
        warnings = []
        if mode == "manual" and include and automatic != "selected_server":
            warnings.append("Manual selection overrides " + automatic)
        if client["unassigned_nodes"] or client["ambiguous_nodes"]:
            warnings.append("Some recorded clients have uncertain ownership; duplicate-call risk is unknown")
        if len({kind for kind in server["types"] if kind != "?"}) > 1:
            warnings.append("Multiple recorded service types require request-content compatibility checks")
        if include:
            candidates.append(service)
        records.append(dict(service=service, decision="candidate" if include else "exclude",
                            reason=automatic if mode == "auto" else "manual" if include else "not_requested",
                            automatic_reason=automatic, warnings=warnings,
                            owners=server["owners"], owner_nodes=server["owner_nodes"],
                            server_nodes=[item["node"] for item in servers],
                            selected_servers=server["selected_instances"],
                            unselected_owners=server["unselected_owners"],
                            unassigned_servers=server["unassigned_nodes"],
                            ambiguous_servers=server["ambiguous_nodes"], types=server["types"],
                            clients=client["owners"], client_nodes=[item["node"] for item in clients],
                            selected_clients=client["selected_instances"],
                            unassigned_clients=client["unassigned_nodes"],
                            ambiguous_clients=client["ambiguous_nodes"]))
    return dict(mode=mode, candidates=candidates, records=records)
