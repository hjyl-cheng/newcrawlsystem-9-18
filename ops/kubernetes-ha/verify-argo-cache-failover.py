#!/usr/bin/env python3
"""Controlled Redis-master Pod replacement; Argo cache only, never business Redis."""
import json,subprocess,time
from pathlib import Path

def kubectl(*args):return subprocess.check_output(['kubectl','--request-timeout=10s','-n','argocd',*args],text=True,timeout=20).strip()
def role(pod):
    out=kubectl('exec',pod,'-c','redis','--','sh','-c','REDISCLI_AUTH="$AUTH" timeout 5 redis-cli --no-auth-warning info replication')
    return next(line.split(':',1)[1].strip() for line in out.splitlines() if line.startswith('role:'))
pods=['argocd-redis-ha-server-'+str(i) for i in range(3)]
before={p:role(p) for p in pods};masters=[p for p,r in before.items() if r=='master'];assert len(masters)==1
kubectl('apply','-f','ops/kubernetes-ha/argo-cache-probe.yaml')
kubectl('wait','--for=jsonpath={.status.phase}=Running','pod/argocd-cache-probe','--timeout=15s')
assert kubectl('exec','argocd-cache-probe','--','timeout','5','redis-cli','--no-auth-warning','-h','argocd-redis-ha-haproxy','PING')=='PONG'
old=masters[0];uid=json.loads(kubectl('get','pod',old,'-o','json'))['metadata']['uid']
start=time.monotonic();kubectl('delete','pod',old,'--wait=false')
new=None
for _ in range(60):
    for pod in pods:
        if pod==old:continue
        try:
            if role(pod)=='master':new=pod;break
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired):pass
    if new:break
    time.sleep(1)
assert new,'No replacement Redis master'
elapsed=round(time.monotonic()-start,2)
# Query through the actual cache entry; credentials remain in the container environment.
for _ in range(10):
    try:
        assert kubectl('exec','argocd-cache-probe','--','timeout','3','redis-cli','--no-auth-warning','-h','argocd-redis-ha-haproxy','PING')=='PONG'
        break
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired):time.sleep(1)
else:raise RuntimeError('Stable cache endpoint did not recover')
kubectl('delete','pod','argocd-cache-probe','--wait=false')
quorum=kubectl('exec',new,'-c','sentinel','--','timeout','5','redis-cli','-p','26379','sentinel','ckquorum','argocd')
assert quorum.startswith('OK')
result={'status':'PASSED','fault':'graceful Redis master Pod replacement, including upstream preStop failover','oldMaster':old,'oldUid':uid,'newMaster':new,'newMasterObservedSeconds':elapsed,'stableCacheEntry':'PONG','quorum':quorum}
Path('ops/checks/2026-09-20-argocd-cache-failover.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
