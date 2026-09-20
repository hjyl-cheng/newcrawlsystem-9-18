#!/usr/bin/env python3
"""Controlled cross-node recovery, not proof of unattended lost-node fencing."""
import json,subprocess,time
from pathlib import Path

def k(*args):return subprocess.check_output(['kubectl','--request-timeout=10s',*args],text=True,timeout=45).strip()
old=json.loads(k('-n','argocd','get','pod','argocd-application-controller-0','-o','json'))
assert old['spec']['nodeName']=='a2','This bounded exercise expects the initial controller on A2'
for n in ['a1','a2']:
    assert not json.loads(k('get','node',n,'-o','json'))['spec'].get('unschedulable',False)
start=time.monotonic();cordoned=[]
try:
    for n in ['a1','a2']:k('cordon',n);cordoned.append(n)
    # Normal deletion waits for the healthy kubelet to stop the old Pod; no force deletion.
    k('-n','argocd','delete','pod','argocd-application-controller-0','--timeout=40s')
    for _ in range(100):
        try:
            new=json.loads(k('-n','argocd','get','pod','argocd-application-controller-0','-o','json'))
            if new['metadata']['uid']!=old['metadata']['uid'] and new['spec']['nodeName']=='a3' and any(c['type']=='Ready' and c['status']=='True' for c in new['status'].get('conditions',[])):break
        except subprocess.CalledProcessError:pass
        time.sleep(1)
    else:raise RuntimeError('Replacement controller not Ready on A3')
    ready_seconds=round(time.monotonic()-start,2)
finally:
    for n in cordoned:k('uncordon',n)
# A reversible change to an infra probe ConfigMap proves the new controller reconciles Git.
ds=json.loads(k('-n','crawl-validation','get','ds','infra-smoke','-o','json'))
cm=next(v['configMap']['name'] for v in ds['spec']['template']['spec']['volumes'] if 'configMap' in v)
expected=Path('deploy/base/infra-smoke/index.html').read_text()
subprocess.run(['kubectl','-n','crawl-validation','patch','cm',cm,'--type=merge','--patch-file=/dev/stdin'],input=json.dumps({'data':{'index.html':'temporary-controller-recovery-check\n'}}),text=True,check=True,capture_output=True)
k('-n','argocd','annotate','application','crawl-infra-validation','argocd.argoproj.io/refresh=hard','--overwrite')
try:
    for _ in range(60):
        data=json.loads(k('-n','crawl-validation','get','cm',cm,'-o','json'))['data']['index.html']
        if data==expected:break
        time.sleep(1)
    else:raise RuntimeError('Replacement controller did not repair the controlled drift')
finally:
    # If reconciliation failed, restore only this known probe value, never application state.
    data=json.loads(k('-n','crawl-validation','get','cm',cm,'-o','json'))['data']['index.html']
    if data!=expected:
        subprocess.run(['kubectl','-n','crawl-validation','patch','cm',cm,'--type=merge','--patch-file=/dev/stdin'],input=json.dumps({'data':{'index.html':expected}}),text=True,check=True,capture_output=True)
result={'status':'PASSED','mode':'controlled relocation with confirmed normal old-Pod deletion','from':'a2','to':'a3','oldUid':old['metadata']['uid'],'newUid':new['metadata']['uid'],'readySeconds':ready_seconds,'gitDriftRepaired':True,'unattendedLostNodeFailover':False}
Path('ops/checks/2026-09-20-argocd-controller-recovery.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
