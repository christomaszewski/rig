"""Plan and publish declared runtime/architecture matrices, with one result catalog."""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from . import RigError, docker_registry
from .build_contract import variant_spec
from .build_progress import BuildProgress
from .common import eprint
from .image_catalog import canonical_ref


@dataclasses.dataclass
class Task:
    service: str
    desc: object
    cwd: Path
    variant: str | None
    architectures: list[str]
    images: list[str]
    tag: str
    command: str
    stage: int = 2
    base: str | None = None
    narrowed: bool = False


def plan(manifest, descriptors, *, registry, tag, platform=None, arch=None, root=None, base=None,
         scopes=None, selections=None, only=None):
    from .build import (_resolve_build_cwd, _script_identity, _service_tag,
                        resolve_base_image, resolve_msgs_image)
    if arch and arch not in ('arm64', 'amd64'):
        raise RigError('--arch must be arm64 or amd64')
    used = {s.service for s in manifest.sensors}
    descriptors = {s: d for s, d in descriptors.items() if s in used}
    from .targets import build_selections, parse_scopes
    scopes = parse_scopes(scopes)
    if scopes != ["all"] and selections is None:
        selections, notes = build_selections(manifest, descriptors, scopes, platform=platform, arch=arch)
        for note in notes:
            eprint(f"target {note}")
    version = tag or manifest.image_tag
    _, base_origin, error = resolve_base_image(manifest, descriptors, registry=registry,
                                                   tag=version, platform=platform, base=base)
    if error:
        raise RigError(error)
    if selections is None and platform and any(d.build_platforms for d in descriptors.values()):
        for d in descriptors.values():
            if d.build_platforms and platform not in d.build_platforms:
                raise RigError(f'{d.service}: runtime {platform!r} is not in {d.build_platforms}')
    required = set()
    for d in descriptors.values():
        if d.build_platforms:
            for variant in ([platform] if platform else d.build_platforms):
                required.update(variant_spec(d, variant)[0])
    tasks = []
    producers = {}
    base_outputs = {d.build_images[0] for d in descriptors.values() if d.build_provides == 'base'}
    for service, desc in descriptors.items():
        if selections is not None and service not in selections:
            continue
        if only is not None and service not in only:  # `rig build SERVICE...`: tasks, not inputs —
            continue                                   # base and msgs union still read everything
        if not desc.build_command:
            continue
        cwd, note = _resolve_build_cwd(service, desc, root)
        if cwd is None:
            raise RigError(f'{service}: {note}')
        if note:
            eprint(f'{service}: {note}')
        variants = (selections[service] if selections is not None else
                    [platform] if platform and desc.build_platforms else desc.build_platforms or [None])
        for variant in variants:
            supported, images = variant_spec(desc, variant)
            if not supported or not desc.build_multiarch:
                raise RigError(f'{service}: declare build.architectures and capabilities.platforms '
                               'before building a portable release (update the pinned service)')
            selected = (sorted(selections[service][variant]) if selections is not None else
                        [arch] if arch else list(supported))
            if selections is None and not variant and platform and required and not arch:
                selected = sorted(required)
            if set(selected) - set(supported):
                raise RigError(f'{service}/{variant or "generic"}: unsupported architecture(s) '
                               f'{sorted(set(selected) - set(supported))}')
            if not images:
                raise RigError(f'{service}: declare the image outputs before building')
            composed = _service_tag(desc, version, variant) or 'latest'
            task_base, _, error = resolve_base_image(manifest, descriptors, registry=registry,
                                                    tag=version, platform=variant, base=base)
            if error:
                raise RigError(error)
            provider = desc.build_provides == 'base'
            if provider and desc.build_platforms and not platform:
                # A generic consumer cannot select among differently parented images at one tag.
                if any(not d.build_platforms and d.build_provides != 'base' and d.build_command
                       for d in descriptors.values()) and base_origin != 'images.base':
                    raise RigError('matrix base with generic consumers needs an explicit images.base')
            task = Task(service, desc, cwd, variant, selected, list(images), composed,
                        desc.build_command, 0 if provider else 2,
                        None if provider else canonical_ref(task_base), set(selected) != set(supported))
            replaced_base = (base_origin == 'images.base' and len(images) == 1
                             and images[0] in base_outputs)
            candidates = [] if replaced_base else [task]
            if desc.msgs_overlay_command:
                msgs_ref, _, error = resolve_msgs_image(manifest, descriptors, registry=registry,
                                                       tag=version, platform=variant)
                if error:
                    raise RigError(error)
                if msgs_ref:
                    candidates.append(dataclasses.replace(task, images=[desc.msgs_overlay_image],
                                      command=desc.msgs_overlay_command, stage=1, base=canonical_ref(task_base)))
            for candidate in candidates:
                identity = (_script_identity((cwd / candidate.command).resolve()), tuple(sorted(selected)))
                keys = [(image, composed) for image in candidate.images]
                duplicates = [producers[k] for k in keys if k in producers]
                if duplicates:
                    if len(duplicates) == len(keys) and all(i == identity for i in duplicates):
                        for prior in tasks:
                            if prior.tag == composed and set(prior.images) == set(candidate.images) and candidate.stage < prior.stage:
                                prior.stage, prior.base = candidate.stage, candidate.base
                        continue
                    raise RigError(f'{service}: conflicting builds publish the same image tag {keys}')
                producers.update({k: identity for k in keys})
                tasks.append(candidate)
    parents = {f'{registry}/{image}:{task.tag}': task for task in tasks for image in task.images}
    for task in tasks:
        if task.base in parents:
            parent = parents[task.base]
            missing = set(task.architectures) - set(parent.architectures)
            if missing:
                raise RigError(f'{task.service}: shared base {task.base} lacks {sorted(missing)}; '
                               'align the build architectures or choose an external images.base')
    return sorted(tasks, key=lambda t: t.stage)


def inspect_raw(ref, *, access=None):
    try:
        result = docker_registry.imagetools(['inspect', '--raw', ref], [ref], access=access,
                                           capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f'cannot inspect {ref}: {exc}') from exc
    if result.returncode:
        if re.search(r'not found|manifest unknown|name unknown', result.stderr, re.I):
            return None
        raise RigError(f'cannot inspect {ref}: {result.stderr.strip()}')
    try:
        raw = json.loads(result.stdout)
        if not isinstance(raw, dict):
            raise ValueError('expected an image manifest')
        return raw
    except ValueError as exc:
        raise RigError(f'invalid manifest for {ref}') from exc


def manifest_architectures(raw):
    return {m.get('platform', {}).get('architecture') for m in raw.get('manifests', [])
            if m.get('platform', {}).get('os') == 'linux'}


def _builder_platforms(access):
    import os
    env = dict(os.environ, **access.build_env) if access else dict(os.environ)
    name = env.get('BUILDX_BUILDER')
    command = ['docker', 'buildx', 'inspect', *([name] if name else []), '--bootstrap']
    for attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RigError(f'Buildx builder {name or "current"!r} unavailable: {exc}') from exc
        if result.returncode:
            raise RigError(f'Buildx builder {name or "current"!r} unavailable: {result.stderr.strip()}')
        actual = re.search(r'^Name:[ \t]*(\S+)', result.stdout, re.M)
        if actual:
            if name and name != actual[1]:
                raise RigError(f'Buildx inspected builder {actual[1]!r}, expected {name!r}')
            name = actual[1]
        errors = re.findall(r'^[ \t]*Error:[ \t]*(.*)$', result.stdout, re.M)
        platforms = sorted({p.strip().rstrip('*')
                            for line in re.findall(r'^[ \t]*Platforms:[ \t]*(.*)$', result.stdout, re.M)
                            for p in line.split(',') if p.strip()})
        if platforms and not errors:
            return name or 'current', platforms
        # Buildx can exit zero after node inspection fails. Older versions also reuse an
        # expired status deadline after a slow bootstrap. Reinspect the same builder once,
        # without rebooting it, before treating missing output as a capability limitation.
        if attempt == 0:
            eprint(f'rig: rechecking builder {name or "current"!r} after incomplete inspection')
            command = ['docker', 'buildx', 'inspect', *([name] if name else [])]
        else:
            detail = '; '.join(errors) if errors else 'no platforms reported after bootstrap'
            raise RigError(f'Buildx builder {name or "current"!r} unavailable: {detail}')


def preflight(tasks, registry, *, access=None):
    name, platforms = _builder_platforms(access)
    available = {match[1] for p in platforms
                 if (match := re.fullmatch(r'linux/(arm64|amd64)(?:/[^/]+)?', p))}
    needed = {a for task in tasks for a in task.architectures}
    if needed - available:
        raise RigError(f'builder {name!r} lacks {sorted(needed - available)}; '
                       f'advertised platforms: {", ".join(platforms)}; '
                       'configure native nodes/emulation or narrow --platform/--arch')
    eprint(f'rig: builder {name!r} supports {", ".join(sorted(available))}')
    for task in tasks:
        if not task.narrowed:
            continue
        for image in task.images:
            ref = f'{registry}/{image}:{task.tag}'
            raw = inspect_raw(ref, access=access)
            if raw is not None:
                existing = manifest_architectures(raw)
                if not existing or existing - set(task.architectures):
                    raise RigError(f'targeted build would replace architecture members of {ref}; '
                                   'use a new iteration tag or build the complete matrix')


def run(manifest, descriptors, *, registry, tag, dry_run, jobs=1, root=None,
        platform=None, arch=None, no_cache=False, base_image=None, scopes=None, verbose=False,
        offline=False, only=None, retry_flags=None):
    from . import build_sources
    from .build import OFFLINE_ENV, _build_cmd, _build_env, msgs_union, retry_hint
    from .lock import load_lock, save_lock
    reg = registry or manifest.image_registry
    if not reg:
        raise RigError('matrix build needs --registry or images.registry')
    from .targets import build_selections, parse_scopes
    scopes = parse_scopes(scopes)
    selections = None
    if scopes != ["all"]:
        used = {s.service for s in manifest.sensors}
        selections, notes = build_selections(manifest, {s: d for s, d in descriptors.items() if s in used},
                                            scopes, platform=platform, arch=arch)
        for note in notes:
            eprint(f"target {note}")
    complete = scopes == ["all"] and platform is None and arch is None and only is None
    tasks = plan(manifest, descriptors, registry=reg, tag=tag, platform=platform,
                 arch=arch, root=root, base=base_image, scopes=scopes, selections=selections,
                 only=only)
    if only is not None and not tasks and not any(descriptors[s].mirror for s in only):
        replaced = ' (an external images.base replaces a base provider\'s own image)' if (
            base_image or manifest.image_base) else ''
        raise RigError(f'nothing to build for {", ".join(sorted(only))} in the selected scope'
                       f'{replaced}')
    for task in tasks:
        if no_cache and task.desc.build_no_cache is not True:
            raise RigError(f'{task.service} ({task.cwd}): no declared no-cache capability; update its build contract '
                           'and run rig certify --repo <source>')
        if not (dry_run or verbose):
            continue
        eprint(f'build {task.service}/{task.variant or "generic"}: '
               f'{",".join(task.architectures)} -> {", ".join(task.images)}:{task.tag}'
               f'{" [no-cache]" if no_cache else ""} (cwd={task.cwd})')
        revision = subprocess.run(['git', '-C', str(task.cwd), 'rev-parse', '--short', 'HEAD'],
                                  capture_output=True, text=True).stdout.strip() or 'unversioned'
        eprint(f'  source={revision}; cache={"disabled" if no_cache else "enabled"}; '
               f'parent={task.base or "script default"}')
    union, error = msgs_union(descriptors)
    if error:
        raise RigError(error)
    # The git sources each task gets as RIG_BUILD_SOURCES: the overlay its msgs repos, any other
    # task its rigging's build.sources — one export per (service, stage), shared across variants.
    sources = {(task.service, task.stage): (build_sources.msgs_requirements(union)
                                            if task.stage == 1 else
                                            build_sources.build_requirements(task.service, task.desc))
               for task in tasks}
    sources = {key: reqs for key, reqs in sources.items() if reqs}
    if dry_run:
        for (service, stage), reqs in sorted(sources.items()):
            eprint(f'  {service}{" [msgs]" if stage == 1 else ""}: sources '
                   f'{", ".join(f"{r.name} <- {r.repo} @ {r.ref}" for r in reqs)}')
        return 0
    mirrors = {image for name, d in descriptors.items()
               if (selections is None or name in selections) and (only is None or name in only)
               for image in d.mirror}
    mirror_owners = sorted({name for name, d in descriptors.items() if set(d.mirror) & mirrors})
    if offline and mirrors:  # the preflight verified the copies are already in the registry
        eprint(f'rig build: offline — using the copies of {", ".join(sorted(mirrors))} already in {reg}')
        mirrors = set()
    labels = [f'build {task.service}/{task.variant or "generic"} -> '
              f'{", ".join(image + ":" + task.tag for image in task.images)} '
              f'[{", ".join(task.architectures)}]' for task in tasks]
    labels += [f'mirror {image}' for image in sorted(mirrors)]
    if not labels:
        eprint('rig build: nothing to build or mirror')
        return 0
    eprint(f'rig build: preparing {len(tasks)} builds and {len(mirrors)} mirrors')
    refs = [f'{reg}/{image}:{task.tag}' for task in tasks for image in task.images]
    refs += [task.base for task in tasks if task.base]
    refs += [ref for image in mirrors for ref in (image, f'{reg}/{image}')]
    access = docker_registry.prepare(refs, for_build=True)
    preflight(tasks, reg, access=access)
    results = []
    completed_refs = {}
    with contextlib.ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rig-build-')))
        exported = {key: build_sources.export(reqs, temp / f'sources-{key[0]}-{key[1]}')
                    for key, reqs in sources.items()}  # before the progress display: may fail
        progress = stack.enter_context(BuildProgress(labels, root=root, verbose=verbose))
        msgs = temp / 'msgs.yaml'
        msgs.write_text(yaml.safe_dump(union or {}))

        done = set()  # task indices that published and verified, for the retry hint

        def execute(index_task):
            index, task = index_task
            with progress.step(index) as step:
                rows = execute_task(index, task, step)
            results.extend(rows)  # here, not via pool.map: a failure must not hide the others
            done.add(index)
            return rows

        def record(*, last_build):
            if root is None or not (results or last_build):
                return
            lock = load_lock(root)
            catalog = lock.setdefault('build_catalog', {})
            for row in results:
                # One logical image/version, even after publishing into a different registry.
                for old in list(catalog):
                    if (catalog[old].get('image') == row['image']
                            and old.rsplit(':', 1)[-1] == row['ref'].rsplit(':', 1)[-1]):
                        del catalog[old]
                catalog[row['ref']] = row
            if last_build:
                lock['last_build'] = dict(tag=tag or manifest.image_tag or 'latest',
                                          complete=complete, scopes=scopes,
                                          platform=platform, architecture=arch,
                                          outputs=[r['ref'] for r in results])
            save_lock(root, lock)

        def execute_task(index, task, step):
            report = temp / f'{index}.jsonl'
            env = _build_env(manifest.ros.distro, task.desc, task.variant,
                             completed_refs.get(task.base, task.base), no_cache, manifest.ros.rmw,
                             str(msgs) if task.stage == 1 else None)
            env.update(access.build_env)
            if offline:
                env.update(OFFLINE_ENV)
            if (task.service, task.stage) in exported:
                env['RIG_BUILD_SOURCES'] = str(exported[(task.service, task.stage)])
            env['RIG_BUILD_PLATFORMS'] = ','.join(f'linux/{a}' for a in task.architectures)
            env['RIG_BUILD_RESULT'] = str(report)
            env['PUSH'] = '1'  # matrix builds publish indexes; never inherit a standalone --load path
            cmd, _ = _build_cmd(task.desc, task.cwd, reg, task.tag, command=task.command)
            step.run(cmd, cwd=task.cwd, env=env)
            step.verifying()
            if not report.exists():
                raise RigError(f'{task.service}: builder did not write RIG_BUILD_RESULT')
            try:
                rows = [json.loads(line) for line in report.read_text().splitlines() if line.strip()]
                if any(not isinstance(row, dict) for row in rows):
                    raise ValueError('expected objects')
            except ValueError as exc:
                raise RigError(f'{task.service}: invalid build result report: {exc}') from exc
            expected = {f'{reg}/{image}:{task.tag}' for image in task.images}
            if {r.get('ref') for r in rows} != expected or len(rows) != len(expected):
                raise RigError(f'{task.service}: build output report does not match declared images')
            revision = subprocess.run(['git', '-C', str(task.cwd), 'rev-parse', 'HEAD'],
                                      capture_output=True, text=True).stdout.strip() or None
            dirty = subprocess.run(['git', '-C', str(task.cwd), 'status', '--porcelain'],
                                   capture_output=True, text=True).stdout.strip()
            for row in rows:
                digest = row.get('digest', '')
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                    raise RigError(f'{task.service}: missing image digest in build report')
                raw = inspect_raw(f'{row["ref"].rsplit(":", 1)[0]}@{digest}', access=access)
                if raw is None:
                    raise RigError(f'{task.service}: published digest is unavailable')
                actual = manifest_architectures(raw)
                if set(task.architectures) - actual:
                    raise RigError(f'{row["ref"]}: published index lacks {set(task.architectures) - actual}')
                row.update(service=task.service, variant=task.variant, architectures=task.architectures,
                           image=row['ref'][len(reg) + 1:].rsplit(':', 1)[0],
                           source_revision=revision, source_dirty=bool(dirty),
                           children={m['platform']['architecture']: m['digest']
                                     for m in raw.get('manifests', [])
                                     if m.get('platform', {}).get('os') == 'linux'},
                           no_cache=no_cache)
            return rows

        for stage in (0, 1, 2):
            group = [(i, task) for i, task in enumerate(tasks) if task.stage == stage]
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
                    list(pool.map(execute, group))
            except RigError as exc:
                # The images that did publish are real: catalog them (their tags now point at
                # them), but never as a finished build. Retry everything not done — tasks that
                # failed, were cancelled, or belong to later stages, and the mirrors after them.
                record(last_build=False)
                undone = [task.service for i, task in enumerate(tasks) if i not in done]
                raise RigError(f'{exc}\n{retry_hint(undone + mirror_owners, retry_flags or [])}'
                               ) from exc
            for row in results:
                completed_refs[row['ref']] = row['ref'].rsplit(':', 1)[0] + '@' + row['digest']
        try:
            for index, image in enumerate(sorted(mirrors), len(tasks)):
                # Preserve all architectures, unlike pull/tag/push through a host-local store.
                with progress.step(index) as step:
                    docker_registry.imagetools(['create', '-t', f'{reg}/{image}', image],
                                               [image, f'{reg}/{image}'], access=access,
                                               runner=step.run)
        except RigError as exc:  # every build published: keep them, retry the mirrors
            record(last_build=False)
            raise RigError(f'{exc}\n{retry_hint(mirror_owners, retry_flags or [])}') from exc
        record(last_build=True)
        eprint(f'rig build: published and verified {len(results)} images; '
               f'{"complete matrix" if complete else "targeted build"}')
    return 0
