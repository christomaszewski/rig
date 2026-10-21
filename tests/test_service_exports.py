"""Sparse service export contract: real subprocess helpers, portability and fault handling."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rig_cli import RigError, service_exports as se
from rig_cli.catalog import load_catalog
from rig_cli.descriptor import load_descriptor
from rig_cli.manifest import load_manifest
from rig_cli.replay_sources import inventory_fingerprint
from test_export import tree, run_dir, _cli, _expect


HELPER = '''#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--request'); p.add_argument('--result'); p.add_argument('--preflight',action='store_true')
a=p.parse_args(); req=json.loads(Path(a.request).read_text())
root=Path(req['source_root']); result=[]
runtime_missing=bool(os.environ.get('STUB_NO_RUNTIME'))
producer={'implementation':'fixture-1','image_id':None if runtime_missing else 'fixture-image'}
missing_images=[]
if os.environ.get('STUB_IMAGE_STATE'):
    runtime_missing=not Path(os.environ['STUB_IMAGE_STATE']).exists() or bool(os.environ.get('STUB_IMAGE_STILL_MISSING'))
    producer.update(image=os.environ['STUB_IMAGE_REF'],architecture=os.environ['RIG_TARGET_ARCH'],
                    image_id=None if runtime_missing else 'sha256:fixture-pulled-image')
    if runtime_missing:
        missing_images=[dict(image=producer['image'],platform='linux/'+producer['architecture'])]
        if 'STUB_IMAGES_REPORT' in os.environ:
            missing_images=json.loads(os.environ['STUB_IMAGES_REPORT'])
    with (Path(__file__).parent/'helper-calls').open('a') as log:
        log.write(json.dumps(dict(preflight=a.preflight,producer=producer))+'\\n')
if not a.preflight:
    log=Path(__file__).parent/'conversions'
    log.write_text(log.read_text()+'x' if log.exists() else 'x')
for row in req['recordings']:
    files=sorted(str(x.relative_to(root)) for x in (root/row['relative']).iterdir() if x.is_file())
    if not a.preflight:
        for f in files:
            target=Path(req['destination'])/f
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(b'proxy-'+os.environ.get('STUB_PROXY_VARIANT','a').encode())
        if os.environ.get('STUB_PROXY_FAIL'): raise SystemExit(8)
        if os.environ.get('STUB_PROXY_EXTRA'):
            (Path(req['destination'])/'unexpected').write_text('extra')
    result.append(dict(name=row['name'],relative=row['relative'],sessions=[dict(id='s1',files=files,
                       frame_count=2,start_ns=10,end_ns=20,width=2,height=2,pixel_format='GRAY8')]))
Path(a.result).write_text(json.dumps(dict(version=1,profile_sha256=req['profile']['sha256'],
    recordings=result,producer=producer,missing_images=missing_images,
    missing_dependencies=['image unavailable'] if runtime_missing else [],
    validation={'ok':True})))
'''


DOCKER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with Path(os.environ['STUB_DOCKER_LOG']).open('a') as log:
    log.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('STUB_PULL_FAIL'):
    print('denied: registry authentication required',file=sys.stderr)
    raise SystemExit(42)
if sys.argv[1:3] != ['pull','--platform'] or len(sys.argv)!=5:
    raise SystemExit('unexpected Docker command: '+repr(sys.argv))
Path(os.environ['STUB_IMAGE_STATE']).write_text('pulled')
print('Downloaded native fixture image')
'''


@contextmanager
def docker_fixture(repo, **extra):
    directory=repo/'bin'
    directory.mkdir()
    docker=directory/'docker'
    docker.write_text(DOCKER)
    docker.chmod(0o755)
    env=dict(PATH=str(directory)+os.pathsep+os.environ['PATH'],
             STUB_IMAGE_STATE=str(repo/'image-present'), STUB_DOCKER_LOG=str(repo/'docker-calls'),
             STUB_IMAGE_REF='registry:5000/cam-dev@sha256:'+'a'*64, **extra)
    with patch.dict(os.environ, env):
        yield env


def logged_calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fixture():
    root, data = tree()
    run = run_dir(data)
    repo = Path(tempfile.mkdtemp())
    (repo / 'export-recordings').write_text(HELPER)
    (repo / 'export-recordings').chmod(0o755)
    definition = dict(version=1,id='h264',revision=1,format='camera-recording-proxy/v1',lossy=True)
    (repo / 'h264.yaml').write_text(json.dumps(definition))
    (repo / 'rigging.yaml').write_text('''service: proxycam
launcher: unused
export:
  data: recordings/{name}
  modes: [service]
  command: [export-recordings]
  default_profile: h264
  profiles: {h264: h264.yaml}
replay:
  source:
    data: recordings/{name}
    binding: direct
    overrides: {replay: {path: '{{replay_input_path}}'}}
''')
    with (root / 'services.yaml').open('a') as handle:
        handle.write(f'  proxycam: {{path: {repo}}}\n')
    return root, run, repo


def generate(root, run, *extra):
    rc, err = _cli(root, 'run', 'export', str(run), '--service', 'proxycam', *extra)
    assert rc == 0, err
    return se.committed_artifacts(run, 'proxycam', 'h264')


def binding(run):
    path = run / 'recordings/cam_front'
    return dict(path=str(path), relative='recordings/renamed', recorded_as='cam_front',
                sessions=['s1'], files=[str(p) for p in sorted(path.iterdir())],
                fingerprint=inventory_fingerprint(path), adapted=True)


def test_sparse_export_default_without_deployment_row_and_reuse():
    root, run, repo = fixture()
    original = {p.name:p.read_bytes() for p in (run/'recordings/cam_front').iterdir()}
    artifacts = generate(root, run)
    assert len(artifacts) == 1
    folder, doc = artifacts[0]
    assert not (folder/'manifest.yaml').exists() and not (folder/'bags').exists()
    assert doc['profile']['id'] == 'h264'
    se.load_artifact(folder, source_root=run)
    assert original == {p.name:p.read_bytes() for p in (run/'recordings/cam_front').iterdir()}
    assert generate(root,run)[0][0] == folder
    assert (repo/'conversions').read_text() == 'x'
    assert len(generate(root,run,'--force')) == 1  # identical output is deduplicated
    assert (repo/'conversions').read_text() == 'xx'


def test_force_preserves_generations_and_ambiguity_requires_pin():
    root,run,repo=fixture()
    first=generate(root,run)[0]
    os.environ['STUB_PROXY_VARIANT']='b'
    try:
        artifacts=generate(root,run,'--force')
    finally:
        os.environ.pop('STUB_PROXY_VARIANT')
    assert len(artifacts)==2 and first[0].is_dir()
    desc=load_descriptor('proxycam',repo)
    _expect(lambda: se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc),
            'multiple compatible')
    selected=se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc,
                                      export_id=first[1]['export_id'])
    assert selected['proxy']['export_id']==first[1]['export_id']
    assert selected['original']['path']==binding(run)['path']
    assert 'exports/services' in selected['path']


def test_portable_proxy_and_changed_source_rejection():
    root,run,repo=fixture()
    generate(root,run)
    moved=Path(tempfile.mkdtemp())/'copied-run'
    shutil.copytree(run,moved)
    desc=load_descriptor('proxycam',repo)
    selected=se.resolve_proxy_binding(moved,binding(moved),service='proxycam',descriptor=desc)
    assert selected['path'].startswith(str(moved.resolve()))
    (moved/'recordings/cam_front/s1.csv').write_text('changed')
    _expect(lambda: se.resolve_proxy_binding(moved,binding(moved),service='proxycam',descriptor=desc),
            'no compatible proxy')


def test_failed_conversion_and_extra_outputs_never_commit():
    for key in ('STUB_PROXY_FAIL','STUB_PROXY_EXTRA'):
        root,run,repo=fixture()
        os.environ[key]='1'
        try:
            rc,err=_cli(root,'run','export',str(run),'--service','proxycam')
        finally:
            os.environ.pop(key)
        assert rc==1,err
        assert se.committed_artifacts(run)==[]
        assert not list((run/'exports/services').rglob('.stage-*'))
        assert (run/'recordings/cam_front/s1.mkv').stat().st_size==8192


def test_dry_run_and_filters_and_mode_rejections():
    root,run,repo=fixture()
    rc,err=_cli(root,'run','export',str(run),'--service','proxycam','--dry-run')
    assert rc==0 and 'would create' in err
    assert not (run/'exports/services').exists() and not (repo/'conversions').exists()
    for arguments,needle in ((['--in-place'],'cannot use'), (['--lossy'],'cannot use'),
                             (['--instance','absent'],'no recordings'),
                             (['--profile','unknown'],'unknown export profile')):
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam',*arguments)
        assert rc==1 and needle in err,err


def test_manifest_identity_corruption_and_escaping_paths():
    root,run,repo=fixture()
    folder,doc=generate(root,run)[0]
    bad=copy.deepcopy(doc)
    bad['profile']['id']='changed'
    (folder/'export.yaml').write_text(json.dumps(bad))
    _expect(lambda:se.load_artifact(folder),'identity mismatch')
    bad=copy.deepcopy(doc)
    bad['recordings'][0]['sessions'][0]['files'][0]['path']='../outside'
    bad['export_id']=se.digest({k:v for k,v in bad.items() if k!='export_id'})
    (folder/'export.yaml').write_text(json.dumps(bad))
    _expect(lambda:se.load_artifact(folder),'relative path')


def test_profile_changes_require_regeneration_or_explicit_compatible_pin():
    root,run,repo=fixture()
    folder,doc=generate(root,run)[0]
    profile=json.loads((repo/'h264.yaml').read_text())
    profile.update(revision=2,quality=24)
    (repo/'h264.yaml').write_text(json.dumps(profile))
    desc=load_descriptor('proxycam',repo)
    _expect(lambda:se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc),
            'no compatible proxy')
    assert se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc,
                                    export_id=doc['export_id'])['proxy']['export_id']==doc['export_id']
    assert len(generate(root,run))==2


def test_descriptor_modes_and_missing_bundle_files():
    from rig_cli.export import discover_exporters
    root,run,repo=fixture()
    desc=load_descriptor('proxycam',repo)
    assert desc.export_source.modes==('service',)
    manifest=load_manifest(root)
    from dataclasses import replace
    manifest.sensors[-1]=replace(manifest.sensors[-1],service='proxycam')
    for mode in ('whole-run','in-place'):
        found,_=discover_exporters(manifest,{'proxycam':desc},run,mode=mode)
        assert not found
    (repo/'h264.yaml').unlink()
    _expect(lambda:load_descriptor('proxycam',repo),'inside the service bundle')


def test_selected_pin_ignores_broken_sibling_but_rejects_extra_media():
    root,run,repo=fixture()
    folder,doc=generate(root,run)[0]
    bad=folder.parent/('a'*64)
    bad.mkdir()
    (bad/'export.yaml').write_text('broken: true\n')
    desc=load_descriptor('proxycam',repo)
    selected=se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc,
                                     export_id=doc['export_id'])
    assert selected['proxy']['export_id']==doc['export_id']
    (folder/'recordings/cam_front/s1-99999.mkv').write_bytes(b'extra')
    _expect(lambda:se.resolve_proxy_binding(run,binding(run),service='proxycam',descriptor=desc,
                                            export_id=doc['export_id']),'no compatible proxy')


def test_destination_symlink_escape_and_concurrent_request_refused():
    root,run,repo=fixture()
    namespace=run/'exports/services'
    namespace.mkdir(parents=True)
    outside=Path(tempfile.mkdtemp())
    (namespace/'proxycam').symlink_to(outside,target_is_directory=True)
    rc,err=_cli(root,'run','export',str(run),'--service','proxycam')
    assert rc==1 and 'escapes' in err,err
    assert not list(outside.iterdir())
    (namespace/'proxycam').unlink()
    folder,doc=generate(root,run)[0]
    import fcntl
    with (folder.parent/f".lock-{doc['request_sha256']}").open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam','--force')
        assert rc==1 and 'already running' in err,err
    assert (repo/'conversions').read_text()=='x'


def test_service_profile_completion_and_replay_alias_flags():
    from rig_cli.completions import candidates
    root,run,repo=fixture()
    words=['--root',str(root),'run','export',str(run),'--service','proxycam','--profile','']
    assert candidates(words,len(words)-1)==['h264']
    words[-1]='--profile=h'
    words.pop(-2)
    assert candidates(words,len(words)-1)==['--profile=h264']
    for prefix in (['replay'],['replay','check'],['replay','prepare'],['run','replay']):
        words=[*prefix,'--']
        assert '--use-proxies' in candidates(words,len(words)-1)


def test_reuse_does_not_require_producers_local_image():
    root,run,repo=fixture()
    folder,doc=generate(root,run)[0]
    os.environ['STUB_NO_RUNTIME']='1'
    try:
        assert generate(root,run)[0][0]==folder
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam','--force')
        assert rc==1 and 'missing dependencies' in err,err
    finally:
        os.environ.pop('STUB_NO_RUNTIME')
    assert (repo/'conversions').read_text()=='x'


def test_missing_native_image_is_pulled_once_and_preflight_refreshes_provenance():
    root,run,repo=fixture()
    with docker_fixture(repo) as env:
        folder,doc=generate(root,run)[0]
    calls=logged_calls(repo/'helper-calls')
    assert [call['preflight'] for call in calls]==[True,True,False]
    assert calls[0]['producer']['image_id'] is None
    assert calls[1]['producer']==calls[2]['producer']==doc['producer']
    assert doc['producer']['image_id']=='sha256:fixture-pulled-image'
    assert doc['producer']['image']==env['STUB_IMAGE_REF']
    import platform
    native={'aarch64':'arm64','x86_64':'amd64'}.get(platform.machine().lower(),platform.machine().lower())
    assert logged_calls(repo/'docker-calls')==[
        ['pull','--platform','linux/'+native,env['STUB_IMAGE_REF']]]
    inputs=sorted([entry for recording in doc['recordings'] for session in recording['sessions']
                   for entry in session['source_files']],key=lambda entry:entry['path'])
    identity=dict(service='proxycam',profile=doc['profile'],inputs=inputs,
                  producer=doc['producer'],helper=se.file_sha256(repo/'export-recordings'))
    assert doc['request_sha256']==se.digest(identity)
    identity['producer']=calls[0]['producer']
    assert doc['request_sha256']!=se.digest(identity)
    assert (repo/'conversions').read_text()=='x'
    se.load_artifact(folder,source_root=run)


def test_missing_image_dry_run_reports_pull_without_docker_or_export_writes():
    root,run,repo=fixture()
    with docker_fixture(repo) as env:
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam','--dry-run')
    assert rc==0 and 'would pull' in err and env['STUB_IMAGE_REF'] in err,err
    assert logged_calls(repo/'docker-calls')==[]
    assert len(logged_calls(repo/'helper-calls'))==1
    assert not (repo/'image-present').exists()
    assert not (repo/'conversions').exists()
    assert not (run/'exports/services').exists()


def test_reuse_skips_missing_image_pull_but_force_prepares_it():
    root,run,repo=fixture()
    with docker_fixture(repo):
        (repo/'image-present').write_text('already cached')
        folder,_=generate(root,run)[0]
        assert logged_calls(repo/'docker-calls')==[]
        (repo/'image-present').unlink()
        assert generate(root,run)[0][0]==folder
        assert logged_calls(repo/'docker-calls')==[]
        assert not (repo/'image-present').exists()
        assert (repo/'conversions').read_text()=='x'
        assert generate(root,run,'--force')[0][0]==folder
    assert len(logged_calls(repo/'docker-calls'))==1
    assert (repo/'conversions').read_text()=='xx'


def test_failed_image_pull_preserves_registry_diagnostic_and_never_commits():
    root,run,repo=fixture()
    with docker_fixture(repo,STUB_PULL_FAIL='1') as env:
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam')
    assert rc==1 and 'pull failed' in err and 'exit 42' in err,err
    assert 'denied: registry authentication required' in err and env['STUB_IMAGE_REF'] in err,err
    assert len(logged_calls(repo/'docker-calls'))==1
    assert len(logged_calls(repo/'helper-calls'))==1
    assert se.committed_artifacts(run)==[]
    assert not (run/'exports/services').exists()
    assert not (repo/'image-present').exists()
    assert not (repo/'conversions').exists()


def test_successful_pull_with_unavailable_image_stops_without_retry_loop():
    root,run,repo=fixture()
    with docker_fixture(repo,STUB_IMAGE_STILL_MISSING='1'):
        rc,err=_cli(root,'run','export',str(run),'--service','proxycam')
    assert rc==1 and 'still unavailable after pull' in err,err
    assert len(logged_calls(repo/'docker-calls'))==1
    assert [call['preflight'] for call in logged_calls(repo/'helper-calls')]==[True,True]
    assert se.committed_artifacts(run)==[]
    assert not (run/'exports/services').exists()
    assert not (repo/'conversions').exists()


def test_malformed_missing_image_reports_are_rejected_before_docker():
    invalid=(None, {}, 'cam-dev:dev', [None], [{}],
             [{'image':'--all-tags','platform':'linux/arm64'}],
             [{'image':'cam-dev:dev extra','platform':'linux/arm64'}],
             [{'image':'cam-dev:dev','platform':'linux/unsupported'}],
             [{'image':'cam-dev:dev','platform':['linux/arm64']}],
             [{'image':'cam-dev:dev','platform':'linux/arm64','unexpected':True}])
    for report in invalid:
        root,run,repo=fixture()
        with docker_fixture(repo,STUB_IMAGES_REPORT=json.dumps(report)):
            rc,err=_cli(root,'run','export',str(run),'--service','proxycam')
        assert rc==1 and ('missing_images must be a list' in err or 'invalid native image dependency' in err),err
        assert logged_calls(repo/'docker-calls')==[]
        assert se.committed_artifacts(run)==[]
        assert not (run/'exports/services').exists()
        assert not (repo/'conversions').exists()


def test_direct_binding_image_label_requirements_are_strict():
    root,run,repo=fixture()
    path=repo/'rigging.yaml'
    text=path.read_text()+'\nbuild: {images: [cam-dev]}\n'
    valid=text.replace('    binding: direct\n',
        '    binding: direct\n    image_labels: {cam-dev: {io.rig.replay: "1"}}\n')
    path.write_text(valid)
    assert load_descriptor('proxycam',repo).replay_source.image_labels=={'cam-dev':{'io.rig.replay':'1'}}
    path.write_text(valid.replace('io.rig.replay: "1"','io.rig.replay: 1'))
    _expect(lambda:load_descriptor('proxycam',repo),'string label requirements')
    path.write_text(valid.replace('image_labels: {cam-dev:', 'image_labels: {unknown:'))
    _expect(lambda:load_descriptor('proxycam',repo),'build image names')
    path.write_text(valid.replace('    binding: direct\n',''))
    _expect(lambda:load_descriptor('proxycam',repo),'requires binding: direct')


if __name__=='__main__':
    failures=0
    for name,fn in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn(); print('ok  ',name)
            except Exception:
                import traceback
                failures+=1; print('FAIL',name); traceback.print_exc()
    sys.exit(bool(failures))
