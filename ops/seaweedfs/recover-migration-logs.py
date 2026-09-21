#!/usr/bin/env python3
"""S3 root-only repair for a migration whose root export omitted historical logs.

Argument: exact protected migration directory. S3 gateway must remain stopped.
Uses a copy of old LevelDB, never opens the original store, and only imports logs.
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=40, **kwargs)

def listing(path='/', endpoint='127.0.0.1:18889'):
    req=urllib.request.Request('http://'+endpoint+path+'?pretty=y',headers={'Accept':'application/json'})
    result=[]
    for e in json.load(urllib.request.urlopen(req,timeout=5)).get('Entries') or []:
        result.append(e)
        if e['Mode'] & 2147483648: result.extend(listing(e['FullPath']+'/',endpoint))
    return result

def shell(endpoint, text):
    r=command(['/opt/seaweedfs/weed','shell','-master=10.4.4.2:9333','-filer='+endpoint],input=text+'\nexit\n',text=True)
    assert 'error:' not in (r.stdout+r.stderr).lower(), 'Metadata export/import error'

def original_bytes(entry):
    if entry.get('Content'): return base64.b64decode(entry['Content'])
    data=bytearray()
    for chunk in sorted(entry.get('chunks') or [],key=lambda c:c.get('offset',0)):
        assert not chunk.get('is_chunk_manifest') and not chunk.get('cipher_key')
        assert chunk.get('offset',0)==len(data), 'This repair only handles contiguous plain chunks'
        fid=chunk['file_id']
        locations=json.load(urllib.request.urlopen('http://10.4.4.2:9333/dir/lookup?volumeId='+fid.split(',')[0],timeout=5))['locations']
        data.extend(urllib.request.urlopen('http://'+locations[0]['url']+'/'+fid,timeout=15).read())
    assert len(data)==entry.get('FileSize',0)
    return bytes(data)

def main():
    assert os.geteuid()==0
    base=Path(sys.argv[1])
    assert re.fullmatch(r'/srv/crawlsystem/seaweedfs/migration-\d{8}T\d{6}Z',str(base))
    assert command(['systemctl','show','seaweedfs-s3','-p','ActiveState','--value'],text=True).stdout.strip()=='inactive'
    work=base.parent/('source-reader-'+uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    with tarfile.open(base/'leveldb-before-pg.tar.gz') as archive: archive.extractall(work,filter='data')
    (work/'filer.toml').write_text('[leveldb2]\nenabled=true\ndir="'+str(work/'filer/filerldb2')+'"\n')
    (work/'master').mkdir()
    user=pwd.getpwnam('seaweedfs')
    for root,dirs,files in os.walk(work):
        os.chown(root,user.pw_uid,user.pw_gid)
        for name in files: os.chown(Path(root)/name,user.pw_uid,user.pw_gid)
    unit='crawl-sw-migration-source-'+work.name[-12:]
    master=unit+'-master'
    command(['systemd-run','--unit='+master,'--property=User=seaweedfs','--property=RuntimeMaxSec=180',
             '--property=TimeoutStopSec=15','/opt/seaweedfs/weed','master','-ip=127.0.0.1','-ip.bind=127.0.0.1',
             '-port=19334','-port.grpc=29334','-peers=none','-mdir='+str(work/'master')])
    command(['systemd-run','--unit='+unit,'--property=User=seaweedfs','--property=WorkingDirectory='+str(work),
             '--property=RuntimeMaxSec=180','--property=TimeoutStopSec=15','/opt/seaweedfs/weed','filer',
             '-ip=127.0.0.1','-ip.bind=127.0.0.1','-port=18889','-port.grpc=28889',
             '-master=127.0.0.1:19334','-filerGroup=migration-source-reader'])
    try:
        for _ in range(120):
            try: entries=listing(); break
            except Exception: time.sleep(.5)
        else: raise RuntimeError('Source reader not ready')
        inventory=[]
        exports=[]
        for i,e in enumerate(entries):
            if not e['Mode'] & 2147483648:
                data=original_bytes(e)
                inventory.append({'path':e['FullPath'],'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            if e['FullPath'].startswith('/topics/.system/log/'):
                dest=str(base/f'log-exact-{i}.meta.gz')
                shell('127.0.0.1:18889','fs.meta.save -o '+dest+' '+e['FullPath'])
                exports.append(dest)
        (base/'inventory-before.json').write_text(json.dumps({'entries':entries,'files':inventory}))
        (base/'inventory-before.json').chmod(0o600)
        # 4.47 fs.meta.load does not wait for file goroutines at EOF. A final
        # existing directory record forces wg.Wait() before the loader returns.
        barrier=base/'directory-barrier.meta.gz'
        shell('127.0.0.1:18889','fs.meta.save -o '+str(barrier)+' /topics/.system/log')
        directory_record=gzip.decompress(barrier.read_bytes())
        for dest in exports:
            source=Path(dest)
            completed=source.with_name(source.name+'.complete.gz')
            completed.write_bytes(gzip.compress(gzip.decompress(source.read_bytes())+directory_record))
            shell('10.4.4.5:8888','fs.meta.load -v=false -concurrency=1 '+str(completed))
        after=listing(endpoint='10.4.4.5:8888')
        assert {e['FullPath'] for e in entries}.issubset({e['FullPath'] for e in after}), 'Original paths missing'
        for f in inventory:
            data=urllib.request.urlopen('http://10.4.4.5:8888'+f['path'],timeout=15).read()
            assert len(data)==f['size'] and hashlib.sha256(data).hexdigest()==f['sha256']
        print(json.dumps({'status':'PASSED','store':'crawler.object_metadata.filemeta',
                         'beforeEntries':len(entries),'afterEntries':len(after),'allOriginalPathsPresent':True,
                         'files':inventory,'exactLogExports':len(exports),'protectedSourceBackup':str(base),
                         'importCompletion':'trailing existing directory forces file completion before EOF'}))
    finally:
        command(['systemctl','stop',unit,master])
        shutil.rmtree(work)

if __name__=='__main__': main()
