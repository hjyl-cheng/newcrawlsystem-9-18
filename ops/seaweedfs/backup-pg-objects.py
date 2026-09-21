#!/usr/bin/env python3
"""Small-store maintenance snapshot: frozen PostgreSQL Filer metadata + all volume replicas.
Not an online production backup. Requires a bounded outage; refuses >=128 MiB/node.
"""
import argparse,hashlib,json,os,runpy,subprocess,tarfile,tempfile,time,urllib.request,uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/seaweedfs/backup-baseline.py'))
remote,NODES=h['remote'],h['NODES']
FINGERPRINT="SELECT count(*),md5(string_agg(md5(dirhash::text||'/'||name||'/'||directory||'/'||encode(meta,'hex')),'' ORDER BY dirhash,name)) FROM object_metadata.filemeta;"
def metadata_hash(ip):return remote(ip,'sudo -u postgres psql -XAt -d crawler',input=FINGERPRINT,text=True,capture_output=True).stdout.strip()
def inventory(path='/'):
    req=urllib.request.Request('http://10.4.4.5:8888'+path+'?limit=10000',headers={'Accept':'application/json'})
    items=json.load(urllib.request.urlopen(req,timeout=10)).get('Entries') or []
    assert len(items)<10000,'Inventory requires pagination at this size'
    result=[]
    for e in items:
        if e['Mode'] & 2147483648:result.extend(inventory(e['FullPath']+'/'))
        elif not e['FullPath'].startswith('/topics/'):
            data=urllib.request.urlopen('http://10.4.4.5:8888'+e['FullPath'],timeout=10).read()
            result.append({'path':e['FullPath'],'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    return result
def parallel(command):
    with ThreadPoolExecutor(3) as ex:list(ex.map(lambda ip:remote(ip,command,capture_output=True),NODES.values()))
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',required=True,action='store_true');p.parse_args()
    os.umask(0o077)
    for ip in NODES.values():
        size=int(remote(ip,'sudo du -sk /srv/crawlsystem/seaweedfs/volume',text=True,capture_output=True).stdout.split()[0]);assert size<131072
    primaries=[ip for ip in NODES.values() if remote(ip,'sudo -u postgres psql -XAt -c "select pg_is_in_recovery();"',text=True,capture_output=True).stdout.strip()=='f'];assert len(primaries)==1
    primary=primaries[0]
    expected=inventory()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    private=ROOT/'secrets/seaweedfs';private.mkdir(exist_ok=True,mode=0o700)
    encrypted=private/('seaweed-pg-objects-'+stamp+'.tar.gpg')
    manifest={'createdAt':stamp,'scope':'quiesced PG object_metadata schema plus all cold volume replicas','sha256':{},'expected':expected}
    unit='crawl-sw-backup-recover-'+uuid.uuid4().hex[:12]
    armed=[]
    with tempfile.TemporaryDirectory(dir=private,prefix='pg-objects-') as tmp:
        work=Path(tmp)
        try:
            for ip in NODES.values():
                remote(ip,f'sudo systemd-run --unit={unit} --on-active=5m /usr/bin/systemctl start seaweedfs-volume seaweedfs-filer seaweedfs-s3',capture_output=True);armed.append(ip)
            started=time.monotonic()
            parallel('sudo systemctl stop seaweedfs-s3')
            parallel('sudo systemctl stop seaweedfs-filer')
            parallel('sudo systemctl stop seaweedfs-volume')
            manifest['metadataFingerprint']=metadata_hash(primary)
            with (work/'metadata.dump').open('wb') as out:
                remote(primary,'sudo -u postgres pg_dump -Fc --no-owner --no-acl -n object_metadata -d crawler',stdout=out)
            for name,ip in NODES.items():
                with (work/(name+'.tar.gz')).open('wb') as out:remote(ip,'sudo tar -czf - -C /srv/crawlsystem/seaweedfs volume',stdout=out)
            assert metadata_hash(primary)==manifest['metadataFingerprint'],'Metadata changed while quiesced'
            assert time.monotonic()-started<240,'Recovery timer could invalidate snapshot'
            manifest['quiescedSeconds']=round(time.monotonic()-started,2)
            for ip in NODES.values():
                states=remote(ip,'systemctl show seaweedfs-s3 seaweedfs-filer seaweedfs-volume -p ActiveState --value',text=True,capture_output=True).stdout.splitlines();assert all(x=='inactive' for x in states if x),states
        finally:
            # Recover every armed node, even if another node cannot be reached.
            errors=[]
            for ip in armed:
                try:
                    remote(ip,'sudo systemctl start seaweedfs-volume seaweedfs-filer seaweedfs-s3',capture_output=True)
                    remote(ip,f'sudo systemctl stop {unit}.timer',capture_output=True)
                except Exception as error:errors.append(type(error).__name__)
            if errors:raise RuntimeError('Explicit recovery failed; independent timers retained: '+str(errors))
        manifest['sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir()}
        (work/'manifest.json').write_text(json.dumps(manifest,indent=2))
        payload=work/'payload.tar'
        with tarfile.open(payload,'w') as tar:
            for child in sorted(work.iterdir()):
                if child!=payload:tar.add(child,arcname=child.name)
        subprocess.run(['sudo','gpg','--batch','--yes','--pinentry-mode','loopback','--passphrase-file','/etc/crawl-backup/control-cipher-pass','--symmetric','--cipher-algo','AES256','--output',str(encrypted),str(payload)],check=True,capture_output=True)
        subprocess.run(['sudo','chown',f'{os.getuid()}:{os.getgid()}',str(encrypted)],check=True)
        digest=hashlib.sha256(encrypted.read_bytes()).hexdigest()
        for ip in [NODES['s2'],NODES['s3']]:
            folder='/srv/crawlsystem/backups/seaweedfs-baseline'
            remote(ip,'sudo install -d -m 0700 '+folder)
            dest=folder+'/'+encrypted.name
            with encrypted.open('rb') as stream:remote(ip,f'sudo sh -c "umask 077; cat > {dest}.partial"',stdin=stream)
            assert remote(ip,'sudo sha256sum '+dest+'.partial',text=True,capture_output=True).stdout.split()[0]==digest
            remote(ip,'sudo mv '+dest+'.partial '+dest)
    result={'status':'PASSED','file':encrypted.name,'bytes':encrypted.stat().st_size,'sha256':digest,'copies':['a1','s2','s3'],'manifest':manifest,'independentRestore':'pending'}
    (ROOT/'ops/checks/2026-09-21-seaweed-pg-objects-backup.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='manifest'}))
if __name__=='__main__':main()
