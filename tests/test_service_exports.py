"""Sparse service export contract: real subprocess helpers, portability and fault handling."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

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
    recordings=result,producer={'implementation':'fixture-1',
        'image_id':None if os.environ.get('STUB_NO_RUNTIME') else 'fixture-image'},
    missing_dependencies=['image unavailable'] if os.environ.get('STUB_NO_RUNTIME') else [],
    validation={'ok':True})))
'''


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
