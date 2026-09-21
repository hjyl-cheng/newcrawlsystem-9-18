#!/usr/bin/env python3
"""Install guarded CDC reconciliation without restarting PG or changing its leader."""
import argparse,json,os,time
import common as c

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
    os.umask(0o077)
    private=c.ROOT/'secrets/cdc/pre-guard';private.mkdir(parents=True,exist_ok=True,mode=0o700)
    primary=c.leader()
    # Stage root-owned code on every server before enabling any promotion hook.
    for node,ip in c.NODES.items():
        cfg=c.run(ip,'sudo cat /etc/crawl-patroni/patroni.yml',capture=True).stdout
        path=private/(node+'.json')
        if not path.exists():path.write_text(cfg)
        c.run(ip,'sudo install -d -m 0755 -o root -g root /opt/crawlsystem/cdc && sudo install -d -m 0700 -o postgres -g postgres /var/lib/crawl-cdc-guard',capture=True)
        c.put(ip,'/opt/crawlsystem/cdc/ha-guard.py',(c.ROOT/'ops/cdc/ha-guard.py').read_text(),'root',0o755)
        c.put(ip,'/etc/systemd/system/crawl-cdc-guard.service',(c.ROOT/'ops/cdc/crawl-cdc-guard.service').read_text(),'root',0o644)
    # Initial durable policy includes both healthy replicas while PG still waits
    # for both. Normal transitions may start only after ALL hooks are installed.
    raw=c.run(c.NODES[primary],'sudo -u postgres /usr/bin/python3 /opt/crawlsystem/cdc/ha-guard.py --once',capture=True).stdout
    result=json.loads(raw.splitlines()[-1]);assert result['state']=='READY' and len(result['candidates'])==2,result
    for node,ip in c.NODES.items():
        path='/etc/crawl-patroni/patroni.yml';cfg=json.loads(c.run(ip,'sudo cat '+path,capture=True).stdout)
        expected='/usr/bin/python3 /opt/crawlsystem/cdc/ha-guard.py --pre-promote'
        previous=cfg['postgresql'].get('pre_promote');assert not previous or previous==expected,'Refuse to overwrite another promotion hook'
        cfg['postgresql']['pre_promote']=expected
        c.put(ip,path,json.dumps(cfg,indent=2),'postgres')
        c.run(ip,'sudo -u postgres patroni --validate-config --ignore-listen-port /etc/crawl-patroni/patroni.yml && sudo systemctl reload crawl-patroni && sudo systemctl daemon-reload',capture=True)
    time.sleep(6)
    for node,ip in c.NODES.items():
        c.run(ip,'sudo systemctl enable --now crawl-cdc-guard',capture=True)
    time.sleep(4)
    states={node:json.loads(c.run(ip,'sudo cat /var/lib/crawl-cdc-guard/status.json',capture=True).stdout) for node,ip in c.NODES.items()}
    assert states[primary]['state']=='READY',states
    result={'status':'PASSED','primary':primary,'guards':states,'scope':'Three reconciliation services plus Patroni pre_promote fencing; no PG restart'}
    (c.ROOT/'ops/checks/2026-09-21-cdc-guard-deploy.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__':main()
