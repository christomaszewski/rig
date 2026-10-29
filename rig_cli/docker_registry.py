"""Carry the active Docker daemon's registry policy into Buildx registry clients.

Buildx imagetools does not read dockerd's insecure-registries. A config-only, Rig-owned
builder supplies that missing policy without changing the user's selected builder. Builds
using the docker driver keep its cache when its image store supports portable builds; classic
stores and unconfigured container builders can use the managed builder instead. Explicit
builder overrides remain authoritative.

The managed builder also gets this host's real DNS servers when the host resolves through a
loopback stub (systemd-resolved), which BuildKit cannot use from a RUN step. Those settings do
not name the builder — a new network must never cost the build cache an offline build needs —
so a change re-creates it under the same name with its state kept.
"""
from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import RigError
from .common import eprint


@dataclasses.dataclass
class Access:
    build_env: dict = dataclasses.field(default_factory=dict)
    registry_env: dict = dataclasses.field(default_factory=dict)


def registry_host(ref):
    first, sep, _ = ref.partition('/')
    if sep and ('.' in first or ':' in first or first.lower() == 'localhost' or first.lower() != first):
        # Buildx looks up registry config by the reference's exact authority, even though
        # DNS and Docker's policy treat hostnames case-insensitively.
        return 'docker.io' if first == 'index.docker.io' else first
    return 'docker.io'


def _addresses(host):
    """Bound DNS too: socket's connect timeout does not bound getaddrinfo."""
    result = queue.Queue()

    def lookup():
        try:
            result.put([r[4][0] for r in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)])
        except OSError:
            result.put([])

    threading.Thread(target=lookup, daemon=True).start()
    try:
        return result.get(timeout=3)
    except queue.Empty:
        return []


def insecure_registry(host, policy):
    """Match dockerd's exact registry entries first, then its IP/CIDR policy."""
    entries = policy.get('IndexConfigs') or {}
    entry = next((value for key, value in entries.items() if key.lower() == host.lower()), None)
    if isinstance(entry, dict) and isinstance(entry.get('Secure'), bool):
        return not entry['Secure']
    networks = []
    for cidr in policy.get('InsecureRegistryCIDRs') or []:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except (ValueError, TypeError):
            continue
    if not networks:
        return False
    name = urllib.parse.urlsplit('//' + host).hostname
    if not name:
        return False
    try:
        addresses = [ipaddress.ip_address(name)]
    except ValueError:
        addresses = []
        for address in _addresses(name):
            try:
                addresses.append(ipaddress.ip_address(address))
            except ValueError:
                pass
    return any(address in network for address in addresses for network in networks)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(host, scheme):
    # No credentials are sent; a 401/403 still proves that this transport speaks HTTP.
    # Certificate verification is relaxed ONLY after dockerd explicitly authorizes the host.
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    try:
        with opener.open(f'{scheme}://{host}/v2/', timeout=3):
            pass
    except urllib.error.HTTPError as exc:
        exc.close()


def registry_transport(host):
    # Docker's "insecure" allows both self-signed TLS and HTTP. Prefer TLS when it works;
    # do not turn a self-signed HTTPS registry into an HTTP registry.
    try:
        _probe(host, 'https')
        return 'insecure'
    except (OSError, urllib.error.URLError):
        try:
            _probe(host, 'http')
            return 'http'
        except (OSError, urllib.error.URLError) as exc:
            raise RigError(f'cannot reach Docker-approved insecure registry {host}: {exc}') from exc


def _docker_info():
    try:
        proc = subprocess.run(
            ['docker', 'info', '--format', '{"id":{{json .ID}},"registries":{{json .RegistryConfig}},'
             '"driver_status":{{json .DriverStatus}},"name":{{json .Name}}}'],
            capture_output=True, text=True, timeout=10)
        info = json.loads(proc.stdout) if proc.returncode == 0 else None
        if isinstance(info, dict) and isinstance(info.get('registries'), dict) and info.get('id'):
            return info
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    # No evidence permitting insecure access. The normal Docker operation will supply its
    # connection error; never guess transport policy from the local daemon.json.
    return None


RESOLV_CONF = '/etc/resolv.conf'
RESOLVED_UPSTREAM = '/run/systemd/resolve/resolv.conf'  # systemd-resolved's real servers


def _resolv(path):
    """(nameservers, search domains) a resolv.conf lists; empty when unreadable."""
    servers, search = [], []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return servers, search
    for line in lines:
        fields = line.split('#', 1)[0].split()
        if len(fields) >= 2 and fields[0] == 'nameserver':
            servers.append(fields[1])
        elif fields and fields[0] in ('search', 'domain'):
            search = [d for d in fields[1:] if d != '.']
    return servers, search


def _loopback(address):
    try:
        return ipaddress.ip_address(address.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


def builder_dns():
    """``[dns]`` for the managed builder when this host resolves only through a loopback stub
    (systemd-resolved's 127.0.0.53). BuildKit drops loopback servers from a RUN step's
    resolv.conf and falls back to 8.8.8.8/8.8.4.4, so on a network that blocks public DNS
    every fetch stalls and fails — while `docker build` works, because dockerd reads resolved's
    upstream list. That same list here; '' when no stub is in play or no upstream is known."""
    local, _ = _resolv(RESOLV_CONF)
    if not local or not all(_loopback(server) for server in local):
        return ''
    upstream, search = _resolv(RESOLVED_UPSTREAM)
    upstream = [server for server in upstream if not _loopback(server)]
    if not upstream:
        return ''
    text = f'[dns]\n  nameservers = {json.dumps(upstream)}\n'
    return text + (f'  searchDomains = {json.dumps(search)}\n' if search else '')


def _builder_tuning(info):
    """Builder settings that do not name it: the operator's RIG_BUILDKITD_CONFIG snippet (a
    buildkitd.toml fragment), and — unless that snippet has its own ``[dns]`` — this host's DNS
    when the Docker daemon is this host's (a remote daemon's resolvers are not ours to read)."""
    extra = ''
    if os.environ.get('RIG_BUILDKITD_CONFIG'):
        path = Path(os.environ['RIG_BUILDKITD_CONFIG']).expanduser()
        try:
            extra = path.read_text()
        except OSError as exc:
            raise RigError(f'RIG_BUILDKITD_CONFIG: cannot read {path}: {exc}') from exc
        extra += '' if extra.endswith('\n') else '\n'
    local = bool(info.get('name')) and info['name'] == socket.gethostname()
    dns = builder_dns() if local and not re.search(r'^\s*\[dns\]', extra, re.M) else ''
    return dns + extra


def _applied_record(name):
    from .registries import rig_home
    return rig_home() / 'cache' / 'buildx' / f'{name}.toml'


def _managed_builder(info, settings):
    config = ''.join(f'[registry.{json.dumps(host)}]\n  {transport} = true\n'
                     for host, transport in sorted(settings.items()))
    identity = json.dumps([info['id'], os.environ.get('DOCKER_CONTEXT'), os.environ.get('DOCKER_HOST'),
                           os.environ.get('DOCKER_TLS_VERIFY'), config], sort_keys=True)
    name = 'rig-registry-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    tuning = _builder_tuning(info)
    record = _applied_record(name)
    try:
        applied = record.read_text()
    except OSError:
        applied = config  # created before rig recorded settings: from the registry tables alone
    inspect = ['docker', 'buildx', 'inspect', name]
    recreated = False
    try:
        existing = subprocess.run(inspect, capture_output=True, text=True, timeout=30)
        if existing.returncode == 0:
            if not re.search(r'^Driver:\s+docker-container\s*$', existing.stdout, re.M):
                raise RigError(f'{name}: expected a Rig-managed docker-container builder')
            if applied == config + tuning:
                return name
            # Same name + kept state volume = same build cache, new settings (a new network's DNS).
            removed = subprocess.run(['docker', 'buildx', 'rm', '--keep-state', name],
                                     capture_output=True, text=True, timeout=60)
            if removed.returncode:
                eprint(f'rig: warning: cannot re-create {name} with new settings '
                       f'({removed.stderr.strip()}); building with its old ones')
                return name
            recreated = True
        with tempfile.TemporaryDirectory(prefix='rig-registry-') as tmp:
            path = Path(tmp) / 'buildkitd.toml'
            path.write_text(config + tuning)
            proc = subprocess.run(
                ['docker', 'buildx', 'create', '--name', name, '--driver', 'docker-container',
                 '--driver-opt', 'network=host', '--buildkitd-config', str(path)],
                capture_output=True, text=True, timeout=30)
        if proc.returncode:
            # Another Rig invocation may have created the same policy concurrently.
            other = subprocess.run(inspect, capture_output=True, text=True, timeout=30)
            if other.returncode or not re.search(r'^Driver:\s+docker-container\s*$', other.stdout, re.M):
                raise RigError(f'cannot prepare Docker registry settings for Buildx: {proc.stderr.strip()}')
        else:
            if tuning:
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(config + tuning)
            else:
                record.unlink(missing_ok=True)
            if recreated:
                eprint(f'rig: re-created managed Buildx builder {name} with new settings; '
                       'its build cache is kept')
        # No --use or --bootstrap: imagetools needs only the saved config, not a container.
        return name
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f'cannot prepare Docker registry settings for Buildx: {exc}') from exc


def prepare(refs, *, for_build=False):
    info = _docker_info()
    if info is None:
        return Access()
    hosts = sorted({registry_host(ref) for ref in refs})
    settings = {host: registry_transport(host) for host in hosts
                if insecure_registry(host, info['registries'])}
    access = Access()
    builder = None
    if settings:
        builder = _managed_builder(info, settings)
        access.registry_env = {'BUILDX_BUILDER': builder}
    if for_build and not (os.environ.get('BUILDX_BUILDER') or os.environ.get('BUILDKIT_HOST')):
        try:
            selected = subprocess.run(['docker', 'buildx', 'inspect'], capture_output=True,
                                      text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RigError(f'cannot inspect selected Buildx builder: {exc}') from exc
        if selected.returncode:
            raise RigError(f'cannot inspect selected Buildx builder: {selected.stderr.strip()}')
        driver = re.search(r'^Driver:\s+(\S+)', selected.stdout, re.M)
        endpoints = re.findall(r'^\s*Endpoint:\s+(\S+)', selected.stdout, re.M)
        if driver and driver[1] in ('docker', 'docker-container') and len(endpoints) == 1:
            try:
                context = subprocess.run(['docker', 'context', 'show'], capture_output=True,
                                         text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RigError(f'cannot determine current Docker context: {exc}') from exc
            # A named context or explicit DOCKER_HOST identifies this same daemon. A selected
            # remote endpoint is not permission to move its build to the local engine.
            current = (context.stdout.strip() if os.environ.get('DOCKER_CONTEXT') else
                       os.environ.get('DOCKER_HOST') or context.stdout.strip())
            if context.returncode == 0 and endpoints[0] == current:
                containerd = ['driver-type', 'io.containerd.snapshotter.v1'] in (
                    info.get('driver_status') or [])
                classic = driver[1] == 'docker' and not containerd
                if classic or (driver[1] == 'docker-container' and settings):
                    builder = builder or _managed_builder(info, settings)
                    access.build_env = {'BUILDX_BUILDER': builder}
                    if classic:
                        eprint('rig: Docker image store lacks multiarch/attestation support; '
                               'using a managed container builder')
        # docker with containerd already supports attestations and dockerd policy; remote/cloud/
        # multinode builders remain under the operator's control rather than moving their work.
    if settings:
        eprint('rig: using Docker insecure-registry settings for ' + ', '.join(settings))
    if access.build_env:
        eprint(f'rig: using managed Buildx builder {builder} for this build')
    return access


def imagetools(args, refs, *, access=None, runner=None, **kwargs):
    """All registry reads/copies share policy; standalone bake/replay retry only TLS failures."""
    env = access.registry_env if access is not None else {}
    command = ['docker', 'buildx', 'imagetools', *args]
    run = runner or subprocess.run
    if env:
        kwargs['env'] = dict(os.environ, **env)
    result = run(command, **kwargs)
    if access is None and result.returncode and re.search(
            r'HTTP response to HTTPS|server gave HTTP|x509:|certificate verify failed',
            result.stderr or '', re.I):
        inherited = prepare(refs)
        if inherited.registry_env:
            result = run(command, **dict(kwargs, env=dict(os.environ, **inherited.registry_env)))
    return result
