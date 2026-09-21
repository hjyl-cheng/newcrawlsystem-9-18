#!/usr/bin/env python3
"""S3 root: restore encrypted small-store bundle into isolated PG and SeaweedFS.
JSON stdin {file}. Never uses live PostgreSQL, Filer or Master endpoints.
"""
import hashlib,json,os,pwd,re,shutil,subprocess,sys,tarfile,time,urllib.request,uuid
from pathlib import Path
FINGERPRINT="SELECT count(*),md5(string_agg(md5(dirhash::text||'/'||name||'/'||directory||'/'||encode(meta,'hex')),'' ORDER BY dirhash,name)) FROM object_metadata.filemeta;"
def run(args,**kwargs):return subprocess.run(args,check=True,capture_output=True,timeout=60,**kwargs)
def main():
    assert os.geteuid()==0;os.umask(0o077)
    spec=json.load(sys.stdin);assert re.fullmatch(r'seaweed-pg-objects-\d{8}T\d{6}Z\.tar\.gpg',spec['file'])
    work=Path('/srv/crawlsystem/restore-check-swpg-'+uuid.uuid4().hex);work.mkdir(mode=0o755)
    units=[]
    evidence={'archive':spec['file'],'objects':[],'isolation':'new PGDATA + independent loopback PG/Master/Volume/Filer; no live endpoints'}
    def unit(suffix,args,user,cwd):
        name='crawl-swpg-'+work.name[-12:]+'-'+suffix
        run(['systemd-run','--unit='+name,'--property=User='+user,'--property=Group='+user,'--property=WorkingDirectory='+str(cwd),'--property=RuntimeMaxSec=180','--property=TimeoutStopSec=15',*args]);units.append(name)
    def sql(statement):
        return run(['sudo','-u','postgres','psql','-XAt','-v','ON_ERROR_STOP=1','-h',str(work/'socket'),'-p','55433','-d','crawler'],input=statement,text=True).stdout.strip()
    try:
        payload=work/'payload.tar'
        run(['gpg','--batch','--pinentry-mode','loopback','--passphrase-file','/etc/crawl-backup/control-cipher-pass','--output',str(payload),'--decrypt','/srv/crawlsystem/backups/seaweedfs-baseline/'+spec['file']])
        with tarfile.open(payload) as tar:tar.extractall(work,filter='data')
        manifest=json.loads((work/'manifest.json').read_text())
        for name,digest in manifest['sha256'].items():assert hashlib.sha256((work/name).read_bytes()).hexdigest()==digest
        # Pick one complete replica per volume stem. Duplicate IDs must never be
        # mounted twice into the independent Volume server.
        volume=work/'volume';volume.mkdir()
        chosen={}
        for node in ['s1','s2','s3']:
            folder=work/node;folder.mkdir()
            with tarfile.open(work/(node+'.tar.gz')) as tar:tar.extractall(folder,filter='data')
            for dat in (folder/'volume').glob('*.dat'):
                if dat.stem in chosen:continue
                chosen[dat.stem]=node
                for child in dat.parent.glob(dat.stem+'.*'):shutil.copy2(child,volume/child.name)
        assert chosen
        (work/'master').mkdir();config=work/'config';config.mkdir()
        (config/'filer.toml').write_text('[postgres]\nenabled=true\ncreateTable=false\nhostname="127.0.0.1"\nport=55433\nusername="sw_restore"\npassword=""\ndatabase="crawler"\nsslmode="disable"\nconnection_max_open=0\nenableUpsert=true\n')
        pg=pwd.getpwnam('postgres');sw=pwd.getpwnam('seaweedfs')
        for folder in [work/'pgdata',work/'socket']:
            folder.mkdir(mode=0o700);os.chown(folder,pg.pw_uid,pg.pw_gid)
        # Restrict access to the containing directory to the two service users.
        os.chown(work,0,sw.pw_gid);os.chmod(work,0o751)
        os.chown(work/'metadata.dump',pg.pw_uid,pg.pw_gid)
        for folder in [volume,work/'master',config]:
            for root,dirs,files in os.walk(folder):
                os.chown(root,sw.pw_uid,sw.pw_gid);os.chmod(root,0o700)
                for name in files:os.chown(Path(root)/name,sw.pw_uid,sw.pw_gid)
        run(['sudo','-u','postgres','/usr/lib/postgresql/17/bin/initdb','-D',str(work/'pgdata'),'--auth-local=peer','--auth-host=reject','--no-locale','--encoding=UTF8'])
        # Only the isolated restore role and DB may use TCP on this temporary port.
        (work/'pgdata/pg_hba.conf').write_text('local all all peer\nhost crawler sw_restore 127.0.0.1/32 trust\n')
        (work/'pgdata/postgresql.conf').write_text("listen_addresses='127.0.0.1'\nport=55433\nunix_socket_directories='"+str(work/'socket')+"'\nshared_buffers='32MB'\nmax_connections=30\narchive_mode=off\n")
        unit('pg',['/usr/lib/postgresql/17/bin/postgres','-D',str(work/'pgdata')],'postgres',work/'pgdata')
        for _ in range(60):
            try:
                run(['sudo','-u','postgres','createdb','-h',str(work/'socket'),'-p','55433','crawler']);break
            except subprocess.CalledProcessError:time.sleep(.5)
        else:raise RuntimeError('Isolated PG not ready')
        run(['sudo','-u','postgres','pg_restore','--exit-on-error','--no-owner','--no-acl','-h',str(work/'socket'),'-p','55433','-d','crawler',str(work/'metadata.dump')])
        assert sql(FINGERPRINT)==manifest['metadataFingerprint'];evidence['metadataFingerprintMatches']=True
        sql("CREATE ROLE sw_restore LOGIN; GRANT USAGE ON SCHEMA object_metadata TO sw_restore; GRANT SELECT,INSERT,UPDATE,DELETE ON object_metadata.filemeta TO sw_restore; ALTER ROLE sw_restore SET search_path TO object_metadata,pg_catalog;")
        unit('master',['/opt/seaweedfs/weed','master','-ip=127.0.0.1','-ip.bind=127.0.0.1','-port=19334','-port.grpc=29334','-peers=none','-mdir='+str(work/'master')],'seaweedfs',config)
        unit('volume',['/opt/seaweedfs/weed','volume','-ip=127.0.0.1','-ip.bind=127.0.0.1','-port=18081','-port.grpc=28081','-master=127.0.0.1:19334','-max=64','-dir='+str(volume)],'seaweedfs',config)
        unit('filer',['/opt/seaweedfs/weed','filer','-ip=127.0.0.1','-ip.bind=127.0.0.1','-port=18889','-port.grpc=28889','-master=127.0.0.1:19334','-filerGroup=isolated-restore'],'seaweedfs',config)
        for expected in manifest['expected']:
            for _ in range(90):
                try:data=urllib.request.urlopen('http://127.0.0.1:18889'+expected['path'],timeout=2).read();break
                except Exception:time.sleep(.5)
            else:raise RuntimeError('Restored file unavailable: '+expected['path'])
            assert len(data)==expected['bytes'] and hashlib.sha256(data).hexdigest()==expected['sha256'],expected['path']
            evidence['objects'].append(expected)
        topology=json.load(urllib.request.urlopen('http://127.0.0.1:19334/dir/status',timeout=3))
        for dc in topology['Topology']['DataCenters']:
            for rack in dc['Racks']:assert all(n['Url']=='127.0.0.1:18081' for n in rack['DataNodes'])
        evidence.update(status='PASSED',restoredVolumeSources=chosen)
    finally:
        for name in reversed(units):run(['systemctl','stop',name])
        shutil.rmtree(work)
    evidence['cleanup']='isolated processes stopped and temporary directories removed'
    print(json.dumps(evidence))
if __name__=='__main__':main()
