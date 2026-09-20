#!/usr/bin/env python3
"""Stop only A1's API static Pod, verify all local entries, restore automatically."""
import concurrent.futures,json,runpy,shlex,socket,subprocess,sys,time,uuid
from pathlib import Path
m=runpy.run_path(str(Path(__file__).with_name('prepare-api.py')))
run=m['run'];tag=uuid.uuid4().hex[:12]
manifest='/etc/kubernetes/manifests/kube-apiserver.yaml'
parked='/etc/kubernetes/ha-test-disabled/kube-apiserver-'+tag+'.yaml'
unit='crawl-api-restore-'+tag
name='api-ha-probe-'+tag
restore=f'if test -f {parked}; then mv {parked} {manifest}; fi'
run(m['NODES']['a1'],f'sudo -n install -d -m 0700 /etc/kubernetes/ha-test-disabled && sudo -n test ! -e {parked}',capture=True)
started=time.monotonic();created=False
try:
    run(m['NODES']['a1'],'sudo -n systemd-run --quiet --unit='+unit+' --on-active=180s /bin/sh -c '+shlex.quote(restore),capture=True)
    run(m['NODES']['a1'],f'sudo -n mv {manifest} {parked}',capture=True)
    for _ in range(45):
        try:
            with socket.create_connection(('10.4.4.12',6443),timeout=1):pass
        except OSError:break
        time.sleep(1)
    else:raise RuntimeError('A1 API remained listening')
    time.sleep(5) # two health checks before measuring client operation
    outcomes={}
    with concurrent.futures.ThreadPoolExecutor() as ex:
        checks={n:ex.submit(run,ip,'kubectl --request-timeout=8s get --raw=/readyz && kubectl --request-timeout=8s get nodes -o name',capture=True) for n,ip in m['NODES'].items()}
        for n,f in checks.items():
            out=f.result().stdout;assert out.startswith('ok') and out.count('node/')==3
            outcomes[n]='readyz and 3 nodes read'
    run(m['NODES']['a2'],f'kubectl --request-timeout=8s -n kube-system create cm {name} --from-literal=source=a2',capture=True);created=True
    out=run(m['NODES']['a3'],f"kubectl --request-timeout=8s -n kube-system get cm {name} -o jsonpath='{{.data.source}}'",capture=True).stdout
    assert out=='a2'
    during = None
    if len(sys.argv)>1:
        during = subprocess.run(sys.argv[1:],check=True,capture_output=True,text=True,timeout=100).stdout.strip()
        try:
            with socket.create_connection(('10.4.4.12',6443),timeout=1):pass
        except OSError:pass
        else:raise RuntimeError('A1 API recovered before callback verification completed')
    result={'duringFault':during,'status':'PASSED','fault':'A1 API static Pod stopped; OS/etcd/workloads remain running','clients':outcomes,'writeFrom':'a2','readFrom':'a3','elapsedSeconds':round(time.monotonic()-started,2)}
    (m['ROOT']/'ops/checks/2026-09-20-kubernetes-api-failover.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)
finally:
    run(m['NODES']['a1'],'sudo -n /bin/sh -c '+shlex.quote(restore),capture=True)
    if created:run(m['NODES']['a1'],f'kubectl -n kube-system delete cm {name}',capture=True)
    for _ in range(45):
        r=subprocess.run(['curl','--fail','--silent','--max-time','2','--cacert','/etc/kubernetes/pki/ca.crt','https://10.4.4.12:6443/readyz'],capture_output=True)
        if r.returncode==0:break
        time.sleep(1)
    else:raise RuntimeError('A1 API did not recover; manifest has been restored')
    run(m['NODES']['a1'],'sudo -n systemctl stop '+unit+'.timer',capture=True)
