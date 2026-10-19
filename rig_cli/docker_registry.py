"""Carry the active Docker daemon's registry policy into Buildx registry clients.

Buildx imagetools does not read dockerd's insecure-registries. A config-only, Rig-owned
builder supplies that missing policy without changing the user's selected builder. Builds
using the docker driver keep its cache when its image store supports portable builds; classic
stores and unconfigured container builders can use the managed builder instead. Explicit
builder overrides remain authoritative.
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
             '"driver_status":{{json .DriverStatus}}}'],
            capture_output=True, text=True, timeout=10)
        info = json.loads(proc.stdout) if proc.returncode == 0 else None
        if isinstance(info, dict) and isinstance(info.get('registries'), dict) and info.get('id'):
            return info
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    # No evidence permitting insecure access. The normal Docker operation will supply its
    # connection error; never guess transport policy from the local daemon.json.
    return None


def _managed_builder(info, settings):
    config = ''.join(f'[registry.{json.dumps(host)}]\n  {transport} = true\n'
                     for host, transport in sorted(settings.items()))
    identity = json.dumps([info['id'], os.environ.get('DOCKER_CONTEXT'), os.environ.get('DOCKER_HOST'),
                           os.environ.get('DOCKER_TLS_VERIFY'), config], sort_keys=True)
    name = 'rig-registry-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    inspect = ['docker', 'buildx', 'inspect', name]
    try:
        existing = subprocess.run(inspect, capture_output=True, text=True, timeout=30)
        if existing.returncode == 0:
            if not re.search(r'^Driver:\s+docker-container\s*$', existing.stdout, re.M):
                raise RigError(f'{name}: expected a Rig-managed docker-container builder')
            return name
        with tempfile.TemporaryDirectory(prefix='rig-registry-') as tmp:
            path = Path(tmp) / 'buildkitd.toml'
            path.write_text(config)
            proc = subprocess.run(
                ['docker', 'buildx', 'create', '--name', name, '--driver', 'docker-container',
                 '--driver-opt', 'network=host', '--buildkitd-config', str(path)],
                capture_output=True, text=True, timeout=30)
        if proc.returncode:
            # Another Rig invocation may have created the same policy concurrently.
            other = subprocess.run(inspect, capture_output=True, text=True, timeout=30)
            if other.returncode or not re.search(r'^Driver:\s+docker-container\s*$', other.stdout, re.M):
                raise RigError(f'cannot prepare Docker registry settings for Buildx: {proc.stderr.strip()}')
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
