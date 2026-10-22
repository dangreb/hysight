"""Local orchestrator: stage source, run an isolated probe and build candidate image.

    uv run python tools/run_native_probe.py

Requires the already-configured agentics-server SSH alias and Docker access there.
No credentials are read on this machine; the probe mounts the existing API key on
the server read-only. It does not edit compose files or restart the active API.
"""
from __future__ import annotations

import base64
import datetime
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode='w:gz') as tar:
    for path in ROOT.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(p in {'.venv', '.git', '__pycache__', '.probe_artifacts', 'build'} or p.endswith('.egg-info') for p in rel.parts):
            continue
        if path.suffix in {'.pyc', '.sqlite3', '.lock'}:
            continue
        tar.add(path, arcname=rel.as_posix())

remote = r'''
import base64,datetime,io,json,os,pathlib,subprocess,tarfile,uuid,urllib.request,urllib.error
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ').lower()
parent=pathlib.Path('/home/dagr/helpers').resolve()
root=(parent/('hysight-v1-'+stamp)).resolve()
assert root.is_relative_to(parent) and root!=parent
root.mkdir(mode=0o700)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(PACKAGE)),mode='r:gz') as tar:
    tar.extractall(root,filter='data')
api='agentics-hindsight-hindsight-1'
meta=json.loads(subprocess.check_output(['docker','inspect',api]))[0]
name='hysight-v1-probe-'+stamp
target='hysight-v1-probe-'+stamp+'-'+uuid.uuid4().hex[:6]
args=['docker','run','--rm','--name',name,'--read-only','--network','container:'+api,
      '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','2g','--cpus','1',
      '--user',str(os.getuid())+':'+str(os.getgid()),
      '--tmpfs','/tmp:rw,size=128m,mode=1777',
      '--mount',f'type=bind,src={root},dst=/workspace,readonly',
      '--mount','type=bind,src=/srv/services/hindsight/secrets/api_key,dst=/run/hysight-api-key,readonly',
      '-e','PYTHONPATH=/workspace/src','-e','PYTHONDONTWRITEBYTECODE=1','-e','HOME=/tmp',
      '-e','HYSIGHT_PROBE_TARGET_BANK='+target,
      '--entrypoint','/app/api/.venv/bin/python',meta['Image'],'/workspace/tools/probe_native.py']
print('Running isolated native probe...',flush=True)
try:
    r=subprocess.run(args,capture_output=True,text=True,timeout=180)
finally:
    # Exact, uniquely owned container and bank only. The probe normally removes both.
    assert name.startswith('hysight-v1-probe-') and target.startswith('hysight-v1-probe-')
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    key=pathlib.Path('/srv/services/hindsight/secrets/api_key').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:8888/v1/default/banks/'+target,method='DELETE',headers={'Authorization':'Bearer '+key})
    try:
        with urllib.request.urlopen(req,timeout=30) as response:response.read()
    except urllib.error.HTTPError as e:
        if e.code!=404:raise
if r.returncode:
    print(r.stdout);print(r.stderr[-6000:]);raise SystemExit(r.returncode)
report=json.loads(r.stdout.strip().splitlines()[-1])
report['source_image_id']=meta['Image'];report['source_image_reference']=meta['Config']['Image'];report['staged_source']=str(root)
print('Building extension candidate image without network...',flush=True)
build=subprocess.run(['docker','build','--network','none','-f','deploy/Dockerfile',
    '--build-arg','HINDSIGHT_IMAGE='+meta['Config']['Image'],'-t','hysight:0.1.0-candidate','.'],cwd=root,capture_output=True,text=True,timeout=180)
if build.returncode:
    print(build.stdout[-2000:]);print(build.stderr[-5000:]);raise SystemExit(build.returncode)
report['candidate_image_id']=subprocess.check_output(['docker','image','inspect','hysight:0.1.0-candidate','--format','{{.Id}}'],text=True).strip()
report['candidate_tag']='hysight:0.1.0-candidate'
tests=subprocess.run(['docker','run','--rm','--read-only','--network','none','--cap-drop','ALL',
    '--security-opt','no-new-privileges:true','--user',str(os.getuid())+':'+str(os.getgid()),
    '--tmpfs','/tmp:rw,size=64m,mode=1777','-e','HOME=/tmp','-e','PYTHONDONTWRITEBYTECODE=1',
    '--mount',f'type=bind,src={root}/tests,dst=/tests,readonly',
    '--entrypoint','/app/api/.venv/bin/python','hysight:0.1.0-candidate',
    '-m','unittest','discover','-s','/tests','-v'],capture_output=True,text=True,timeout=60)
if tests.returncode:
    print(tests.stdout);print(tests.stderr[-6000:]);raise SystemExit(tests.returncode)
report['linux_unit_tests']=tests.stderr.strip().splitlines()[-3:]
report['live_service_configuration_changed']=False
print(json.dumps(report,ensure_ascii=False))
'''
script='PACKAGE='+repr(base64.b64encode(archive.getvalue()).decode())+'\n'+remote
result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','agentics-server','python3','-'],input=script,
                      capture_output=True,text=True,encoding='utf-8')
artifacts=ROOT/'.probe_artifacts'
artifacts.mkdir(exist_ok=True)
(artifacts/'native-exec.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
if result.returncode:
    print(result.stdout);print(result.stderr);raise SystemExit(result.returncode)
report=json.loads(result.stdout.strip().splitlines()[-1])
(artifacts/'native-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
