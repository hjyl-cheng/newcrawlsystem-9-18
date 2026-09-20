#!/usr/bin/env python3
"""Publish a bounded infra probe update through Git/Argo, used during API fault tests."""
import json,re,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
marker=sys.argv[1] if len(sys.argv)>1 else 'crawlsystem-infra-validation-ha-20260920'
assert re.fullmatch(r'crawlsystem-infra-validation-[a-z0-9-]+',marker)
path=ROOT/'deploy/base/infra-smoke/index.html'
assert subprocess.check_output(['git','diff','--cached','--name-only'],cwd=ROOT,text=True).strip()==''
if path.read_text().strip()!=marker:
    path.write_text(marker+'\n')
    subprocess.run(['kubectl','kustomize','deploy/overlays/validation/infra-smoke'],cwd=ROOT,check=True,capture_output=True)
    subprocess.run(['git','add',str(path)],cwd=ROOT,check=True,capture_output=True)
    subprocess.run(['git','commit','-m','infra: publish API failover validation probe'],cwd=ROOT,check=True,capture_output=True)
    subprocess.run(['git','push','origin','main'],cwd=ROOT,check=True,capture_output=True,timeout=25)
revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
subprocess.run(['kubectl','-n','argocd','annotate','application','crawl-infra-validation','argocd.argoproj.io/refresh=hard','--overwrite'],check=True,capture_output=True)
start=time.monotonic()
for _ in range(180):
    app=json.loads(subprocess.check_output(['kubectl','--request-timeout=5s','-n','argocd','get','application','crawl-infra-validation','-o','json']))
    status=app.get('status',{})
    if status.get('sync',{}).get('revision')==revision and status['sync']['status']=='Synced' and status.get('health',{}).get('status')=='Healthy':break
    time.sleep(1)
else:raise RuntimeError('Argo did not complete probe release during fault window')
subprocess.run(['python3','ops/scripts/check-cluster-network.py',marker],cwd=ROOT,check=True,capture_output=True,timeout=25)
print(json.dumps({'status':'PASSED','gitRevision':revision,'marker':marker,'argo':'Synced/Healthy','network':'all 3 nodes: Pod, DNS, Service and storage TCP','releaseSeconds':round(time.monotonic()-start,2)}))
