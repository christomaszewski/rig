"""Isolated command capture for build-contract certification; never run a real build."""
from __future__ import annotations
import contextlib
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from .build_contract import variant_spec

STUB = '''#!/usr/bin/env python3
import hashlib, json, os, sys
a = sys.argv[1:]
with open(os.environ['RIG_PROBE_LOG'], 'a') as f: f.write(json.dumps(a)+'\\n')
if a[:2] == ['buildx', 'build'] and '--metadata-file' in a:
    tags = [a[i+1] for i, v in enumerate(a[:-1]) if v in ('-t', '--tag')]
    tags += [v.split('=', 1)[1] for v in a if v.startswith('--tag=')]
    ref = tags[0] if tags else ''
    digest = 'sha256:' + hashlib.sha256(ref.encode()).hexdigest()
    with open(a[a.index('--metadata-file')+1], 'w') as f:
        json.dump({'containerimage.digest': digest}, f)
if a[:1] in (['build'], ['push']) or a[:2] == ['buildx', 'build']: sys.exit(0)
sys.stderr.write('unsupported Docker operation in probe: '+repr(a))
sys.exit(96)
'''


def linux_socket_filter(tmp):
    """Network namespaces alone do NOT isolate pathname Unix sockets (including docker.sock)."""
    try:
        seccomp = ctypes.CDLL('libseccomp.so.2')
    except OSError:
        return None
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctx = seccomp.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW; deny socket creation/connections below
    if not ctx:
        return None
    output = tmp / 'no-sockets.bpf'
    try:
        # io_uring can submit socket operations without invoking socket/connect syscalls.
        for name in (b'socket', b'connect', b'io_uring_setup'):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if syscall < 0 or seccomp.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, syscall, 0):
                return None
        with output.open('wb') as stream:
            if seccomp.seccomp_export_bpf(ctx, stream.fileno()):
                return None
        return output
    finally:
        seccomp.seccomp_release(ctx)


def sandbox_command(tmp, cwd, command):
    if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists():
        profile = tmp / 'sandbox.sb'
        profile.write_text('(version 1)(deny default)(allow process*)(allow file-read*)'
                           '(allow sysctl-read)(allow mach-lookup)'
                           f'(allow file-write* (subpath {json.dumps(str(tmp.resolve()))})'
                           ' (literal "/dev/null"))')
        return ['/usr/bin/sandbox-exec', '-f', str(profile), *command]
    if sys.platform.startswith('linux') and shutil.which('bwrap'):
        filt = linux_socket_filter(tmp)
        if filt is None:
            return None
        return ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
                '--dev', '/dev', '--proc', '/proc', '--bind', str(tmp), str(tmp),
                '--chdir', str(cwd), '--seccomp', str(filt), '--', *command]
    return None


def run_probe(command, *, cwd, env):
    """Reap the entire probe process group if a build script hangs or leaves children behind."""
    with contextlib.ExitStack() as stack:
        command, fds = list(command), ()
        if command[0] == 'bwrap':
            # bwrap accepts a BPF descriptor, not a filename. Pass only this descriptor to the child.
            pos = command.index('--seccomp') + 1
            stream = stack.enter_context(open(command[pos], 'rb'))
            fds = (stream.fileno(),)
            command[pos] = str(stream.fileno())
        child = stack.enter_context(subprocess.Popen(command, cwd=cwd, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True, pass_fds=fds))
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


def check_commands(commands, expected, no_cache, platforms=None):
    builds = [c for c in commands if c[:1] == ['build'] or c[:2] == ['buildx', 'build']]
    produced, errors = set(), []
    for command in builds:
        tags = [command[i+1] for i, arg in enumerate(command[:-1]) if arg in ('-t', '--tag')]
        tags += [arg.split('=', 1)[1] for arg in command if arg.startswith('--tag=')]
        produced.update(tags)
        enabled = ('--no-cache' in command or '--no-cache=true' in command) and '--no-cache=false' not in command
        if enabled != no_cache:
            errors.append(f'{", ".join(tags) or "untagged build"}: '
                          f'{"missing" if no_cache else "unconditional"} --no-cache')
        if platforms:
            requested = [command[i+1] for i, arg in enumerate(command[:-1]) if arg == '--platform']
            requested += [a.split('=', 1)[1] for a in command if a.startswith('--platform=')]
            if requested != [platforms]:
                errors.append(f'{", ".join(tags) or "untagged build"}: '
                              f'expected --platform {platforms}, observed {requested}')
            if command[:2] != ['buildx', 'build'] or '--push' not in command or '--load' in command:
                errors.append(f'{", ".join(tags) or "untagged build"}: portable build must publish with buildx --push')
    if produced != set(expected):
        errors.append(f'expected image builds {sorted(expected)}, observed {sorted(produced)}')
    return errors


def check_results(path, expected):
    """Require one report per output, matching the metadata returned by the Docker stub."""
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return ['missing or invalid RIG_BUILD_RESULT JSONL']
    wanted = {ref: 'sha256:' + hashlib.sha256(ref.encode()).hexdigest() for ref in expected}
    if (len(rows) != len(wanted) or any(not isinstance(row, dict) for row in rows)
            or {row.get('ref') for row in rows if isinstance(row.get('ref'), str)} != set(wanted)
            or any(not isinstance(row.get('ref'), str)
                   or row.get('digest') != wanted.get(row['ref']) for row in rows)):
        return ['RIG_BUILD_RESULT must report each published ref and its builder metadata digest exactly once']
    return []


def _copy(source, dest):
    result = subprocess.run(['git', '-C', str(source), 'ls-files', '-z', '--cached',
                             '--others', '--exclude-standard', '.'], capture_output=True)
    files = ([Path(os.fsdecode(f)) for f in result.stdout.split(b'\0') if f] if not result.returncode
             else [f.relative_to(source) for f in source.rglob('*') if f.is_file()
                   and not {'.git', '.venv', '__pycache__', 'node_modules', 'var'}.intersection(
                       f.relative_to(source).parts)])
    for rel in files:
        if (source / rel).is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, target)


def certify_build(desc):
    from .certify import Check, ERROR, WARN, OK
    from .build import _service_tag
    if not desc.build_command:
        return []
    def unchecked(reason):
        return [Check(WARN, 'build-no-cache', 'NOT CHECKED: ' + reason)]
    commands = [(desc.build_command, False)]
    if desc.msgs_overlay_command:
        commands.append((desc.msgs_overlay_command, True))
    if any(len(shlex.split(c)) != 1 for c, _ in commands):
        return unchecked('requires a script entrypoint')
    paths = [(desc.repo / c).resolve() for c, _ in commands]
    if any(not p.is_file() for p in paths):
        return unchecked('build context absent; certify the pinned source checkout')
    source = Path(os.path.commonpath([str(desc.repo.resolve()), *[str(p.parent) for p in paths]]))
    if source == Path(source.anchor):
        return unchecked('build script is outside a bounded source context')
    errors = []
    revision = subprocess.run(['git', '-C', str(desc.repo), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip() or 'unversioned'
    try:
        with tempfile.TemporaryDirectory(prefix='rig-certify-build-') as folder:
            tmp = Path(folder).resolve()
            cwd = tmp / 'source' / desc.repo.resolve().relative_to(source)
            _copy(source, tmp / 'source')
            cwd.mkdir(parents=True, exist_ok=True)
            bin_dir = tmp / 'bin'
            bin_dir.mkdir()
            docker = bin_dir / 'docker'
            docker.write_text(STUB)
            docker.chmod(0o755)
            log, msgs, report = tmp / 'calls.jsonl', tmp / 'msgs.yaml', tmp / 'results.jsonl'
            msgs.write_text('apt: [diagnostic_msgs]\n')
            # Allow only tool/runtime basics; operator image/subset overrides cannot weaken a check.
            base_env = {k: v for k, v in os.environ.items() if k in ('LANG', 'LC_ALL', 'SYSTEMROOT')}
            base_env.update(PATH=f'{bin_dir}:{Path(sys.executable).parent}:{os.defpath}',
                            TMPDIR=str(tmp), HOME=str(tmp), RIG_PROBE_LOG=str(log))
            for variant in desc.build_platforms or [None]:
                arches, images = variant_spec(desc, variant)
                combinations = [[a] for a in arches] or [[]]
                if len(arches) > 1:
                    combinations.append(arches)
                for selected in combinations:
                    arch = ','.join(selected) or 'native'
                    platforms = ','.join('linux/' + a for a in selected) if desc.build_multiarch else None
                    for command, overlay in commands:
                        outputs = [desc.msgs_overlay_image] if overlay else images
                        if not outputs:
                            return unchecked('no declared image outputs')
                        tag = _service_tag(desc, 'certify', variant)
                        for no_cache in (False, True):
                            env = dict(base_env, RIG_TARGET_PLATFORM=variant or '',
                                       RIG_MSGS_MANIFEST=str(msgs), RIG_BASE_IMAGE='certify.invalid/base:certify')
                            if desc.platform_override_env and variant:
                                env[desc.platform_override_env] = variant
                            if platforms:
                                env['RIG_BUILD_PLATFORMS'] = platforms
                                env['RIG_BUILD_RESULT'] = str(report)
                            if no_cache:
                                env['RIG_BUILD_NO_CACHE'] = '1'
                            log.write_text('')
                            report.unlink(missing_ok=True)
                            cmd = sandbox_command(tmp, cwd, [str(cwd / command), 'certify.invalid', tag])
                            if cmd is None:
                                return unchecked('install bubblewrap and libseccomp2 (Linux) '
                                                 'or use sandbox-exec (macOS)')
                            result = run_probe(cmd, cwd=cwd, env=env)
                            if result.returncode:
                                detail = result.stderr.strip()[-500:]
                                if 'sandbox' in detail.lower() or 'bwrap:' in detail:
                                    return unchecked(f'sandbox unavailable: {detail}')
                                errors.append(f'{variant or "generic"}/{arch or "native"}: probe failed: {detail}')
                                continue
                            calls = [json.loads(line) for line in log.read_text().splitlines()]
                            expected = {f'certify.invalid/{image}:{tag}' for image in outputs}
                            errors.extend(f'{variant or "generic"}/{arch or "native"}: {e}'
                                          for e in check_commands(calls, expected, no_cache, platforms))
                            if platforms:
                                errors.extend(f'{variant or "generic"}/{arch}: {e}'
                                              for e in check_results(report, expected))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return [Check(ERROR, 'build-no-cache', f'probe failed: {exc}')]
    return [Check(ERROR, 'build-no-cache', f'{desc.repo}@{revision}: ' + '; '.join(errors))] \
        if errors else [Check(OK, 'build-no-cache', f'{desc.repo}@{revision}')]
