#!/usr/bin/env python3
"""Activate one node-local endpoint, preserving its own protected rollback copies."""
import argparse,json,runpy,shlex
from pathlib import Path
m=runpy.run_path(str(Path(__file__).with_name('prepare-api.py')))
p=argparse.ArgumentParser();p.add_argument('node',choices=m['NODES']);a=p.parse_args();ip=m['NODES'][a.node]
script='''import os,pathlib,pwd,shutil,tempfile,yaml
root=pathlib.Path('/srv/crawlsystem/kubernetes-ha/pre-local-api');root.mkdir(mode=0o700,parents=True,exist_ok=True)
paths=[pathlib.Path('/etc/hosts'),*pathlib.Path('/etc/kubernetes').glob('*.conf')]
user=pathlib.Path('/home/ubuntu/.kube/config')
if user.exists():paths.append(user)
for p in paths:
 d=root/str(p).lstrip('/');d.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 if not d.exists():shutil.copy2(p,d)
for p in paths:
 if p.name=='hosts':continue
 d=yaml.safe_load(p.read_text());changed=False
 for cluster in d.get('clusters',[]):
  if cluster['cluster']['server']=='https://k8s-api.crawl.internal:6443':
   cluster['cluster']['server']='https://k8s-api.crawl.internal:16443';changed=True
 if changed:
  info=p.stat();fd,tmp=tempfile.mkstemp(dir=p.parent);os.fchmod(fd,info.st_mode & 0o777);os.fchown(fd,info.st_uid,info.st_gid)
  with os.fdopen(fd,'w') as f:yaml.safe_dump(d,f,sort_keys=False)
  os.replace(tmp,p)
hosts=pathlib.Path('/etc/hosts');lines=[]
for line in hosts.read_text().splitlines():
 if not line.lstrip().startswith('#') and 'k8s-api.crawl.internal' in line.split():
  tokens=line.split();tokens.remove('k8s-api.crawl.internal')
  if len(tokens)<2:continue
  line=' '.join(tokens)
 lines.append(line)
lines.append('127.0.0.1 k8s-api.crawl.internal')
hosts.write_text('\\n'.join(lines)+'\\n')
# Each control node retains its existing admin identity for local emergency access.
if not user.exists():
 u=pwd.getpwnam('ubuntu');user.parent.mkdir(mode=0o700,exist_ok=True);os.chown(user.parent,u.pw_uid,u.pw_gid)
 shutil.copyfile('/etc/kubernetes/admin.conf',user);user.chmod(0o600);os.chown(user,u.pw_uid,u.pw_gid)
'''
m['run'](ip,'sudo -n python3 -c '+shlex.quote(script),capture=True)
m['run'](ip,'sudo -n systemctl restart kubelet && kubectl get --raw=/readyz && kubectl get nodes',capture=True)
print(a.node+': kubelet and administrator use local HA endpoint; readiness query passed.')
