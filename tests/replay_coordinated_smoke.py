#!/usr/bin/env python3
"""Real coordinated replay acceptance, isolated to fresh temporary resources.

Usage: python tests/replay_coordinated_smoke.py --infra /path/to/rig-infra
       --image ALREADY_LOCAL_COORDINATION_IMAGE [--scripted-calls]
Only the uniquely named synthetic containers created here are removed. No image pulls/builds.
Each invocation keeps its fixture, logs and summary under /private/tmp for release evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import yaml

GENERATE = r'''from pathlib import Path
import rosbag2_py
from rclpy.serialization import serialize_message
from rosbag2_py._storage import QoS
from std_msgs.msg import String
from std_srvs.srv import SetBool
path = Path('/fixture/source/bags/logger/exact')
path.parent.mkdir(parents=True, exist_ok=True)
writer = rosbag2_py.SequentialWriter()
writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id='mcap'), rosbag2_py.ConverterOptions('cdr', 'cdr'))
qos = QoS(10)
qos.reliable()
for ident, (topic, typ) in enumerate([
    ('/rig_smoke/input', 'std_msgs/msg/String'),
    ('/rig_smoke/output', 'std_msgs/msg/String'),
    ('/rig_smoke/set_bool/_service_event', 'std_srvs/srv/SetBool_Event')]):
    writer.create_topic(rosbag2_py.TopicMetadata(id=ident, name=topic, type=typ,
                        serialization_format='cdr', offered_qos_profiles=[qos]))
events = []
for stamp, text in [(0,'start'), (1,'one'), (4,'four'), (5,'excluded-at-end'), (6,'end')]:
    events.append((stamp, '/rig_smoke/input', String(data=text)))
events.append((2, '/rig_smoke/output', String(data='RECORDED_OUTPUT')))
for stamp, origin, value, seq in [(1.5,1,True,1), (1.6,0,True,1), (3,1,False,2)]:
    event = SetBool.Event()
    event.info.event_type = origin
    event.info.stamp.sec = 999
    event.info.client_gid = [1] * 16
    event.info.sequence_number = seq
    event.request = [SetBool.Request(data=value)]
    events.append((stamp, '/rig_smoke/set_bool/_service_event', event))
for stamp, topic, message in sorted(events, key=lambda x:x[0]):
    writer.write(topic, serialize_message(message), 100_000_000_000 + int(stamp*1e9))
writer.close()
'''

SERVER = r'''import json
import os
import signal
import sys
import time
from pathlib import Path
import rclpy
from std_msgs.msg import String
from std_srvs.srv import SetBool
time.sleep(float(os.environ.get('CONSUMER_START_DELAY_S', '0')))
rclpy.init()
node = rclpy.create_node('server', namespace='/rig_smoke/controller')
def record(kind, value):
    with Path('/evidence/received.jsonl').open('a') as out:
        out.write(json.dumps({'kind':kind, 'value':value, 'at':time.time()})+'\n')
def serve(request, response):
    record('call', request.data)
    response.success = True
    return response
node.create_service(SetBool, '/rig_smoke/set_bool', serve)
node.create_subscription(String, '/rig_smoke/input', lambda msg:record('input',msg.data), 10)
node.create_subscription(String, '/rig_smoke/output', lambda msg:record('output',msg.data), 10)
if os.environ.get('CAMERA_TOPIC'):
    from sensor_msgs.msg import Image
    import hashlib
    def frame(msg):
        record('frame', dict(stamp_ns=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec,
                            sha256=hashlib.sha256(bytes(msg.data)).hexdigest(),
                            width=msg.width, height=msg.height, encoding=msg.encoding))
    node.create_subscription(Image, os.environ['CAMERA_TOPIC'], frame, 1000)
publisher = node.create_publisher(String, '/rig_smoke/output', 10)
node.create_timer(.5, lambda:publisher.publish(String(data='LIVE_OUTPUT')))
record('ready', True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    rclpy.spin(node)
finally:
    Path('/evidence/stopped').write_text('consumer finalized\n')
    node.destroy_node()
    rclpy.try_shutdown()
'''

LAUNCHER = r'''#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
base = Path(__file__).resolve().parent
args = sys.argv[2:] or ['up']
opts = ['-f', str(base/'compose.yaml')]
if args[0] in ('runtime-images','images'):
    command = [sys.executable,str(base/'runtime_images.py'),args[0],*opts]
elif args[0]=='up' and 'RIG_RUNTIME_IMAGE_PINS' in os.environ:
    command = [sys.executable,str(base/'runtime_images.py'),'pinned-up',*opts,'--',*args[1:]]
else:
    command = ['docker','compose',*opts,*args]
raise SystemExit(subprocess.run(command).returncode)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--infra', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--consumer-delay', type=float, default=2)
    parser.add_argument('--scripted-calls', action='store_true')
    parser.add_argument('--window-from', type=float, default=0)
    parser.add_argument('--rate', type=float, default=1)
    parser.add_argument('--wall-clock', action='store_true')
    parser.add_argument('--camera-service', type=Path)
    parser.add_argument('--camera-image')
    parser.add_argument('--bridge-image')
    parser.add_argument('--bake', choices=('registry', 'offline'))
    parser.add_argument('--auto-player', action='store_true')
    parser.add_argument('--fault', choices=('timeout','kill-held','cancel','concurrent'))
    parser.add_argument('--legacy-default', action='store_true', help='exercise the unchanged default with an older reader image')
    args = parser.parse_args()
    if args.camera_service and not (args.camera_image and args.bridge_image):
        parser.error('--camera-service requires --camera-image and --bridge-image')
    if args.auto_player and (not args.bake or args.rate != 1):
        parser.error('--auto-player requires --bake and the default rate')
    if args.fault:
        args.consumer_delay=60
    if args.legacy_default and (args.fault or args.camera_service):
        parser.error('--legacy-default covers bag/call compatibility; omit camera and fault flags')
    package, infra = args.package.resolve(), args.infra.resolve()
    if (infra/"rig-infra").is_dir():
        infra = infra/"rig-infra"
    assert (package/'rig_cli/cli.py').is_file(), package
    token = uuid.uuid4().hex[:10]
    root = Path('/tmp').resolve()/('rig-replay-coordinated-'+token)
    root.mkdir()
    print('EVIDENCE_ROOT='+str(root), flush=True)
    fleet = root/'deployment'
    fleet.mkdir()
    data, evidence = root/'data', root/'evidence'
    data.mkdir(); evidence.mkdir()
    image = args.image
    domain = 100 + int(token[:3],16)%100
    port = 23000 + int(token[3:6],16)%12000
    names = {kind:'smoke-'+kind+'-'+token for kind in ('router','consumer','player')}
    if args.camera_service:
        names['camera'] = 'smoke_camera_'+token
    if args.auto_player:
        names['player'] = 'bag_player'
    vehicle_id = 'e2e'+token
    projects = [name+'-vehicle-'+vehicle_id for name in names.values()]
    generator = 'rig-replay-e2e-generate-'+token
    env = {k:v for k,v in os.environ.items() if not k.startswith(('RIG_', 'ROS_', 'RMW_', 'ZENOH_', 'BAG_PLAYER_', 'COMPOSE_', 'CAM_'))}
    env.update(PYTHONPATH=str(package), PYTHONDONTWRITEBYTECODE='1',
               PATH=str(Path(sys.executable).parent)+os.pathsep+env['PATH'],
               RIG_HOME=str(root/'rig-home'), RIG_VEHICLE_LOCAL=str(root/'absent-machine-local.yaml'),
               BAG_PLAYER_PYTHON=sys.executable, ZENOH_ROUTER_PYTHON=sys.executable)
    lognumber = 0
    baked = False
    injected = []
    owned_aliases = []
    sentinel = 'rig-replay-e2e-sentinel-'+token
    def inject(process):
        try:
            deadline=time.monotonic()+45
            while process.poll() is None and time.monotonic()<deadline:
                states=[]
                for path in (fleet/'var/replay/sessions').glob('*/control/statuses/*.player.json'):
                    try:
                        states.append(json.loads(path.read_text()).get('state'))
                    except (OSError,ValueError):
                        pass
                if 'held' in states or 'ready' in states:
                    break
                time.sleep(.05)
            else:
                raise AssertionError('player never reached hold for fault injection')
            if args.fault=='kill-held':
                project=names['player']+'-vehicle-'+vehicle_id
                ids=subprocess.check_output(['docker','ps','-q','--filter',
                    'label=com.docker.compose.project='+project],text=True).split()
                assert len(ids)==1,ids
                subprocess.run(['docker','kill',ids[0]],check=True,capture_output=True)
            elif args.fault=='cancel':
                process.send_signal(signal.SIGINT)
            elif args.fault=='concurrent':
                other=root/'conflicting-deployment'
                shutil.copytree(fleet,other,ignore=shutil.ignore_patterns('var'))
                command=([str(other/'rig')] if baked else
                    [sys.executable,'-c','from rig_cli.cli import main; raise SystemExit(main())'])
                result=subprocess.run([*command,'--root',str(other),'replay',str(root/'source'),
                    '--startup','coordinated','--pull','never','--to','5','--force'],cwd=other,env=env,
                    capture_output=True,text=True,timeout=60)
                (root/'concurrent.stderr').write_text(result.stderr)
                assert result.returncode!=0,result.stdout
                assert ('Another replay is starting project' in result.stderr or
                        'requires an empty project' in result.stderr),result.stderr
            injected.append(args.fault)
        except Exception as exc:
            injected.append(repr(exc))
    def call(command, *, label, timeout=90, required=True, **kwargs):
        nonlocal lognumber
        lognumber += 1
        started = time.monotonic()
        if label=='replay-auto-end' and args.fault and args.fault!='timeout':
            process=subprocess.Popen(command,cwd=fleet,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            worker=threading.Thread(target=inject,args=(process,),daemon=True)
            worker.start()
            try:
                out,err=process.communicate(timeout=timeout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            worker.join(timeout=65)
            result=subprocess.CompletedProcess(command,process.returncode,out,err)
        else:
            result = subprocess.run(command, cwd=fleet, env=env, text=True, capture_output=True, timeout=timeout, **kwargs)
        prefix = root/f'{lognumber:02d}-{label}'
        prefix.with_suffix('.stdout').write_text(result.stdout)
        prefix.with_suffix('.stderr').write_text(result.stderr)
        prefix.with_suffix('.json').write_text(json.dumps(dict(command=command,returncode=result.returncode,
                                                    elapsed_s=time.monotonic()-started),indent=2))
        print(f'{label}: exit={result.returncode} seconds={time.monotonic()-started:.2f}', flush=True)
        if required and result.returncode:
            raise AssertionError(label+' failed\n'+result.stderr[-8000:]+'\n'+result.stdout[-4000:])
        return result
    def docker(*argv, **kwargs):
        return call(['docker',*argv],**kwargs)
    def rig(*argv, **kwargs):
        if baked:
            return call(['./rig',*argv],**kwargs)
        return call([sys.executable,'-c','from rig_cli.cli import main; raise SystemExit(main())',
                     '--root',str(fleet),*argv],**kwargs)
    def write(path, value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(yaml.safe_dump(value,sort_keys=False))
    def fingerprints():
        return {str(p.relative_to(root/'source')):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root/'source').rglob('*') if p.is_file()}
    result_summary = dict(token=token, package=str(package), infra=str(infra), image=image,
                          ros_domain=domain, zenoh_port=port, projects=projects, success=False)
    try:
        info=json.loads(docker('image','inspect',image,label='image').stdout)[0]
        arch=info['Architecture']
        assert arch in ('arm64','amd64') and info['Config']['Labels'].get('io.rig.rosbag2.replay')=='1'
        result_summary['image_id']=info['Id']
        shutil.copytree(infra,fleet/'services/rig-infra',
                        ignore=shutil.ignore_patterns('.git','var','__pycache__'))
        consumer=fleet/'services/smoke-consumer'
        consumer.mkdir()
        (consumer/'server.py').write_text(SERVER)
        (consumer/'up').write_text(LAUNCHER)
        (consumer/'up').chmod(0o755)
        shutil.copy2(infra/'ros2-bag-player/tools/runtime_images.py',consumer/'runtime_images.py')
        write(consumer/'rigging.yaml',dict(service='smoke-consumer',launcher='up',tier='autonomy',
                ros_namespace='/rig_smoke/controller',ros_distro='lyrical',
                verbs={'up':'up -d','down':'down','status':'ps','logs':'logs','config':'config',
                       'runtime-images':'runtime-images','images':'images'},
                build={'architectures':['arm64','amd64']},
                launch_surface=['up','server.py','runtime_images.py','compose.yaml']))
        write(consumer/'compose.yaml',{'services':{'consumer':dict(image='${RIG_BASE_IMAGE}',
            network_mode='host',restart='no',command=['python3','/server.py'],
            environment={'ROS_DOMAIN_ID':'${ROS_DOMAIN_ID}', 'RMW_IMPLEMENTATION':'${RMW_IMPLEMENTATION}',
                         'CONSUMER_START_DELAY_S':str(args.consumer_delay),
                         'CAMERA_TOPIC':('/'+names['camera']+'/image_raw') if args.camera_service else '',
                         'ZENOH_CONFIG_OVERRIDE':'${ZENOH_CONFIG_OVERRIDE}'},
            volumes=['./server.py:/server.py:ro',str(evidence)+':/evidence'])}})
        write(fleet/'services.yaml',{'services':{
            'zenoh-router':{'path':'services/rig-infra/zenoh-router'},
            'ros2-bag-player':{'path':'services/rig-infra/ros2-bag-player'},
            'smoke-consumer':{'path':'services/smoke-consumer'}}})
        write(fleet/'router.yaml',{'service':'zenoh-router','name':names['router'],'router_config':{
            'listen':{'endpoints':[f'tcp/127.0.0.1:{port}']},'connect':{'endpoints':[]},
            'scouting':{'multicast':{'enabled':False},'gossip':{'enabled':True}},
            'transport':{'shared_memory':{'enabled':False}}}})
        write(fleet/'player.yaml',{'service':'ros2-bag-player','name':names['player'],
            'source':{'logger':'logger','session':'exact'},'play':{'rate':args.rate,'services_source':'service'},
            'zenoh':{'shared_memory':False}})
        write(fleet/'consumer.yaml',{'service':'smoke-consumer','name':names['consumer']})
        write(fleet/'vehicle.yaml',{'vehicle':'isolated replay smoke','vehicle_id':vehicle_id,
            'arch':arch,'ros':{'domain_id':domain,'rmw':'rmw_zenoh_cpp','distro':'lyrical'},
            'images':{'base':image},'data_dir':str(data),
            'env':{'ZENOH_CONFIG_OVERRIDE':f'connect/endpoints=["tcp/127.0.0.1:{port}"];scouting/multicast/enabled=false'},
            'infra':[dict(name=names['router'],service='zenoh-router',config='router.yaml',order=0)],
            'autonomy':[dict(name=names['consumer'],service='smoke-consumer',config='consumer.yaml',
                             order=1,replay={'start':True}),
                        dict(name=names['player'],service='ros2-bag-player',config='player.yaml',order=999,enabled=False)]})
        (root/'generate.py').write_text(GENERATE)
        (root/'source').mkdir()
        docker('run','--rm','--name',generator,'--network','none','--pull','never','--platform','linux/'+arch,
               '-v',str(root)+':/fixture',image,'python3','/fixture/generate.py',label='generate')
        write(root/'source/manifest.yaml',{'run':'synthetic-'+token,'ended':'2026-10-22T00:00:00Z'})
        write(root/'source/graph/logger/epoch_20261022T000000Z.yaml',{'schema':1,'nodes':{
            '/rig_smoke/controller/server':{'pubs':[{'topic':'/rig_smoke/output','type':'std_msgs/msg/String'}],
                'subs':[{'topic':'/rig_smoke/input','type':'std_msgs/msg/String'}],
                'provides':[{'service':'/rig_smoke/set_bool','type':'std_srvs/srv/SetBool'}]}}})
        if args.camera_service:
            shutil.copytree(args.camera_service, fleet/'services/camera-service',
                            ignore=shutil.ignore_patterns('.git','.venv','var','recordings','__pycache__'))
            routes=yaml.safe_load((fleet/'services.yaml').read_text())
            routes['services']['camera-service']={'path':'services/camera-service'}
            write(fleet/'services.yaml',routes)
            camera_config={'service':'camera-service','name':names['camera'],
                'camera':{'type':'usb','frame_rate':25}, 'usb':{'fake':True},
                'recording':{'enabled':False},'preview':{'enabled':False},
                'replay':{'speed':args.rate},
                'plugins':[{'name':'ros2-bridge','enabled':True,'isolation':'container',
                            'params':{'zenoh_shm':False}}]}
            write(fleet/'camera.yaml',camera_config)
            vehicle=yaml.safe_load((fleet/'vehicle.yaml').read_text())
            vehicle['platform']='dev'
            vehicle['env'].update(CAM_DEV_IMAGE=args.camera_image,CAM_ROS2_IMAGE=args.bridge_image)
            vehicle['sensors']=[dict(name=names['camera'],service='camera-service',config='camera.yaml',order=0)]
            write(fleet/'vehicle.yaml',vehicle)
            graphpath=root/'source/graph/logger/epoch_20261022T000000Z.yaml'
            graph=yaml.safe_load(graphpath.read_text())
            topic={'topic':'/'+names['camera']+'/image_raw','type':'sensor_msgs/msg/Image'}
            graph['nodes']['/rig_smoke/controller/server']['subs'].append(topic)
            graph['nodes']['/'+names['camera']+'/cam_ros2_bridge']={'pubs':[topic]}
            write(graphpath,graph)
            script=("import sys; from pathlib import Path; sys.path.insert(0,'/camera/core-driver/tests'); "
                    "from test_replay_source import _write_session; "
                    f"p=Path('/fixture/source/recordings/{names['camera']}'); p.mkdir(parents=True); "
                    "_write_session(p,'camera',1,100_000_000_000,151)")
            docker('run','--rm','--network','none','--pull','never',
                   '-v',str(root)+':/fixture','-v',str(args.camera_service.resolve())+':/camera:ro',
                   args.camera_image,'python3','-c',script,label='generate-camera')
        if args.auto_player:
            vehicle=yaml.safe_load((fleet/'vehicle.yaml').read_text())
            vehicle['autonomy']=[row for row in vehicle['autonomy'] if row['service']!='ros2-bag-player']
            write(fleet/'vehicle.yaml',vehicle)
            routes=yaml.safe_load((fleet/'services.yaml').read_text())
            del routes['services']['ros2-bag-player']
            write(fleet/'services.yaml',routes)
        if args.bake:
            previous_tags=set(docker('image','ls','--format','{{.Repository}}:{{.Tag}}',
                                     label='pre-bake-image-tags').stdout.splitlines())
            bake_flags=['--bundle-images'] if args.bake=='offline' else []
            rig('bake','--tag','acceptance','--include-replay',*bake_flags,label='bake',timeout=600)
            archive=fleet/'var/artifacts/acceptance.tar.gz'
            rig('unbake',str(archive),'--into',str(root/'extracted'),label='unbake',timeout=300)
            fleet=root/'extracted/acceptance'
            baked=True
            env.pop('PYTHONPATH',None)
            # No sibling checkout remains in the execution directory. The entry point and
            # launch surfaces must come entirely from the artifact.
            result_summary['artifact']=str(archive)
            result_summary['artifact_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
            result_summary['bake']=args.bake
            result_summary['auto_player']=args.auto_player
            if args.bake=='offline':
                metadata=yaml.safe_load((fleet/'metadata.yaml').read_text())
                aliases=list(metadata['bundle']['image_ids'])
                owned_aliases.extend(alias for alias in aliases if alias not in previous_tags)
                for alias in owned_aliases:
                    if docker('image','inspect',alias,label='inspect-new-bundle-alias',required=False).returncode==0:
                        docker('image','rm',alias,label='clear-new-bundle-alias')
                    assert docker('image','inspect',alias,label='offline-cold-alias',required=False).returncode!=0
                result_summary['offline_aliases_initially_absent']=len(owned_aliases)==len(aliases)
                call(['./run.sh','load'],label='load-offline-images',timeout=300)
        before=fingerprints()
        flags=['--from',str(args.window_from)]
        if not args.legacy_default:
            flags+=['--startup','coordinated']
        if args.wall_clock:
            flags += ['--wall-clock']
        if args.scripted_calls:
            write(root/'calls.yaml', {'schema':1,'calls':[
                {'t':0,'service':'/rig_smoke/set_bool','type':'std_srvs/srv/SetBool','request':{'data':True}},
                {'t':3,'service':'/rig_smoke/set_bool','type':'std_srvs/srv/SetBool','request':{'data':False}}]})
            flags += ['--calls',str(root/'calls.yaml')]
        check=rig('replay','check',str(root/'source'),'--to','5','--json','--pull','never',*flags,label='cold-evidence-check',required=False)
        cold=json.loads(check.stdout)
        if not args.scripted_calls:
            assert check.returncode==1 and not cold['ready_to_run'],cold
            assert any(i['code']=='recorded_calls' and i['status']=='not_checked' for i in cold['issues']),cold['issues']
        else:
            assert cold['ready_to_run'],cold['issues']
        assert cold['playback']['topics']==['/rig_smoke/input'],cold['playback']
        assert not (fleet/'var/replay/call-inspections').exists()
        if args.fault:
            flags+=['--ready-timeout','8','--startup-timeout','30']
            docker('run','-d','--name',sentinel,'--network','none','--pull','never',image,
                   'sleep','180',label='unrelated-sentinel')
        delay=[] if args.legacy_default else ['--start-delay','0']
        run=rig('replay',str(root/'source'),'--to','5',*delay,'--auto-end','0','--pull','never',*flags,
                label='replay-auto-end',timeout=180,required=not args.fault)
        manifests=list((data/'runs').glob('*/manifest.yaml'))
        assert len(manifests)==1,manifests
        manifest=yaml.safe_load(manifests[0].read_text())
        if args.fault:
            assert run.returncode!=0,run.stdout
            if args.fault!='timeout':
                assert injected==[args.fault],injected
            control=next((fleet/'var/replay/sessions').glob('*/control'))
            assert json.loads((control/'command.json').read_text())['op']=='abort'
            statuses=[json.loads(p.read_text()) for p in (control/'statuses').glob('*.json')]
            assert not any(s.get('actual_release_at_unix_s') for s in statuses),statuses
            assert not manifest.get('ended'),manifest
            for project in projects:
                assert not docker('ps','-aq','--filter','label=com.docker.compose.project='+project,
                                  label='failure-cleanup-check').stdout.strip(),project
            assert docker('inspect','--format','{{.State.Running}}',sentinel,label='sentinel-survives').stdout.strip()=='true'
            assert fingerprints()==before,'source run changed'
            result_summary.update(success=True,fault=args.fault,returncode=run.returncode,
                                  source_unchanged=True,unrelated_container_preserved=True)
            return
        assert manifest.get('ended'),manifest
        assert (evidence/'stopped').is_file(),'consumer was removed before graceful finalization'
        plan=yaml.safe_load((manifests[0].parent/'.rig/replay/resolved.yaml').read_text())
        assert plan['playback']['topics']==['/rig_smoke/input'],plan['playback']
        if not args.scripted_calls:
            assert plan['playback']['services']==['/rig_smoke/set_bool'],plan['playback']
            assert plan['call_inspection']['result']['services']['/rig_smoke/set_bool']['request_count']==2
        received=[json.loads(line) for line in (evidence/'received.jsonl').read_text().splitlines()]
        calls=[row['value'] for row in received if row['kind']=='call']
        inputs=[row['value'] for row in received if row['kind']=='input']
        scheduled_calls=[(0,True),(3,False)] if args.scripted_calls else [(1.5,True),(3,False)]
        scheduled_calls=[item for item in scheduled_calls if item[0]>=args.window_from]
        assert calls==[value for stamp,value in scheduled_calls],received
        assert inputs==[value for stamp,value in [(0,'start'),(1,'one'),(4,'four')]
                        if stamp>=args.window_from],received
        assert any(row['kind']=='output' and row['value']=='LIVE_OUTPUT' for row in received),received
        assert not any(row['value']=='RECORDED_OUTPUT' for row in received),received
        if args.legacy_default:
            assert plan['startup']['mode']=='legacy',plan['startup']
            assert not list((fleet/'var/replay/sessions').glob('*/control/command.json'))
            assert fingerprints()==before,'source run changed'
            assert not (data/'current').exists()
            for project in projects:
                assert not docker('ps','-aq','--filter','label=com.docker.compose.project='+project,
                                  label='legacy-cleanup-check').stdout.strip(),project
            result_summary.update(success=True,startup='legacy',inputs=inputs,calls=calls,source_unchanged=True)
            return
        startup=manifest['replay']['startup']
        assert startup['startup_mode']=='coordinated' and startup['status']=='completed',startup
        target=startup['start_at_unix_s']
        assert all(row['at']>=target for row in received if row['kind'] in ('call','input','frame')),received
        assert all(item['release_at_unix_s']==target for item in startup['participants'].values()),startup
        assert all(item['actual_release_at_unix_s']>=target for item in startup['participants'].values()),startup
        assert target>=next(row['at'] for row in received if row['kind']=='ready'),startup
        if args.camera_service:
            frames=[row['value'] for row in received if row['kind']=='frame']
            indices=[i for i in range(125) if i*.04>=args.window_from]
            expected=[hashlib.sha256(bytes([(50+i*7)%256])*256).hexdigest() for i in indices]
            assert [f['sha256'] for f in frames]==expected,(len(frames),len(expected),frames[:2])
            if not args.wall_clock:
                assert [f['stamp_ns'] for f in frames]==[100_000_000_000+i*40_000_000 for i in indices]
            result_summary['camera_frames']=len(frames)
        if args.scripted_calls:
            actual_calls=[row for row in received if row['kind']=='call']
            for row,(stamp,value) in zip(actual_calls,scheduled_calls):
                expected=(stamp-args.window_from)/args.rate
                assert -.05 <= row['at']-target-expected < .75,(row,target,expected)
        result_summary['startup']=startup
        assert fingerprints()==before,'source run changed'
        assert not (data/'current').exists(),'output run still open'
        for project in projects:
            assert not docker('ps','-aq','--filter','label=com.docker.compose.project='+project,
                              label='auto-end-cleanup-check').stdout.strip(),project
        warm=rig('replay','check',str(root/'source'),'--to','5','--json','--pull','never',*flags,label='warm-evidence-check')
        assert json.loads(warm.stdout)['ready_to_run']
        result_summary.update(success=True,run_id=manifest['run'],calls=calls,inputs=inputs,
                              suppressed_topic='/rig_smoke/output',source_unchanged=True,
                              capture=manifest.get('capture'),plan=str(manifests[0].parent/'.rig/replay/resolved.yaml'))
    finally:
        # Capture only our still-existing projects, then remove only their exact container IDs.
        for project in projects:
            found=docker('ps','-aq','--no-trunc','--filter','label=com.docker.compose.project='+project,
                         label='cleanup-inventory',required=False)
            ids=found.stdout.split()
            for ident in ids:
                docker('logs',ident,label='failure-container-log',required=False)
            if ids:
                docker('rm','-f',*ids,label='cleanup-owned-containers',required=False)
        docker('rm','-f',generator,label='cleanup-generator',required=False)
        if args.fault:
            docker('rm','-f',sentinel,label='cleanup-sentinel',required=False)
        if args.camera_service:
            docker('volume','rm','cam_'+names['camera']+'_sock',label='cleanup-camera-volume',required=False)
        for alias in owned_aliases:
            docker('image','rm',alias,label='cleanup-bundle-alias',required=False)
        (root/'summary.json').write_text(json.dumps(result_summary,indent=2)+'\n')
        print(json.dumps(result_summary,indent=2),flush=True)


if __name__=='__main__':
    main()
