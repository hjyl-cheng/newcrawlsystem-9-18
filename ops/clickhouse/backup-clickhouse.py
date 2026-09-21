#!/usr/bin/env python3
"""A1 root: native online backup of crawler_analytics, encrypted copies on A1/S2.

Keeps the seven most recent completed full backups; no business application logic. Never
deletes an old archive before verifying the new ciphertext on the other server.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import zipfile

DEST = Path('/srv/crawlsystem/backups/clickhouse')
NATIVE = '/srv/crawlsystem/backups/clickhouse/native/'
KEY = '/etc/crawl-backup/control-cipher-pass'
SSH = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes',
       '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'UserKnownHostsFile=/home/ubuntu/.ssh/known_hosts']
S3, S2 = '10.4.4.5', '10.4.4.8'

def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=900, **kwargs)

def remote(ip, args, **kwargs):
    return command(SSH + ['ubuntu@'+ip, shlex.join(args)], **kwargs)

def sql(statement):
    r = remote(S3, ['sudo', '-n', 'clickhouse-client', '--host', '127.0.0.1',
                    '--max_threads', '2', '--query', statement], capture_output=True, text=True)
    return r.stdout.strip()

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute', required=True, action='store_true');p.parse_args()
    assert os.geteuid()==0, 'Run on A1 as root'
    os.umask(0o077);DEST.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DEST/'.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name='clickhouse-'+stamp+'-'+uuid.uuid4().hex[:8]
        native=name+'.zip';archive=DEST/(native+'.gpg');pending_archive=DEST/(native+'.gpg.pending')
        source_bytes=int(sql("SELECT coalesce(sum(bytes_on_disk),0) FROM system.parts WHERE active AND database='crawler_analytics'"))
        required=max(1024**3, source_bytes*3)
        assert shutil.disk_usage(DEST).free>required, 'Insufficient A1 backup space'
        source_free=int(sql("SELECT free_space FROM system.disks WHERE name='crawl_backups'"))
        assert source_free>required, 'Insufficient S3 staging space'
        remote(S2,['sudo','-n','install','-d','-m','0700',str(DEST)])
        free=int(remote(S2,['sudo','-n','python3','-c',
            'import shutil;print(shutil.disk_usage("'+str(DEST)+'").free)'],capture_output=True,text=True).stdout)
        assert free>required, 'Insufficient S2 backup space'
        tables=[json.loads(line) for line in sql("SELECT name,engine FROM system.tables WHERE database='crawler_analytics' ORDER BY name FORMAT JSONEachRow").splitlines() if line]
        # A full native backup includes schema and data, not system logs or users.
        response=sql("BACKUP DATABASE crawler_analytics TO Disk('crawl_backups', '"+native+"') SETTINGS compression_method='deflate', compression_level=3 FORMAT JSONEachRow")
        result=json.loads(response);assert result['status']=='BACKUP_CREATED', result
        with tempfile.TemporaryDirectory(prefix='.pending-',dir=DEST) as tmp:
            work=Path(tmp);raw=work/native
            with raw.open('wb') as output:remote(S3,['sudo','-n','cat',NATIVE+native],stdout=output)
            raw_digest=digest(raw)
            assert remote(S3,['sudo','-n','sha256sum',NATIVE+native],text=True,capture_output=True).stdout.split()[0]==raw_digest
            with zipfile.ZipFile(raw) as z:
                assert z.testzip() is None, 'Native archive CRC failure'
                assert '.backup' in z.namelist(), 'Missing native backup metadata'
            cipher=work/'encrypted.gpg'
            command(['gpg','--batch','--yes','--pinentry-mode','loopback','--no-symkey-cache',
                '--passphrase-file',KEY,'--symmetric','--cipher-algo','AES256','--compress-algo','none',
                '--output',str(cipher),str(raw)],capture_output=True)
            decoded=work/'decoded.zip'
            command(['gpg','--batch','--pinentry-mode','loopback','--passphrase-file',KEY,
                '--output',str(decoded),'--decrypt',str(cipher)],capture_output=True)
            assert digest(decoded)==raw_digest
            with cipher.open('rb') as f:os.fsync(f.fileno())
            os.replace(cipher,pending_archive)
        encrypted_digest=digest(pending_archive)
        receiver='import os,sys; p=sys.argv[1]; f=open(p,"xb"); os.chmod(p,0o600);\nwhile chunk:=sys.stdin.buffer.read(1024*1024): f.write(chunk)\nf.flush();os.fsync(f.fileno());f.close()'
        target=str(archive)
        with pending_archive.open('rb') as source:
            remote(S2,['sudo','-n','python3','-c',receiver,target+'.partial'],stdin=source)
        assert remote(S2,['sudo','-n','sha256sum',target+'.partial'],capture_output=True,text=True).stdout.split()[0]==encrypted_digest
        remote(S2,['sudo','-n','mv',target+'.partial',target])
        os.replace(pending_archive,archive)
        # Remove only this run's unencrypted staging file after durable copies.
        remote(S3,['sudo','-n','python3','-c','from pathlib import Path;import sys;Path(sys.argv[1]).unlink()',NATIVE+native])
        prune=r'''from pathlib import Path
import re
p=Path('/srv/crawlsystem/backups/clickhouse')
items=sorted(f for f in p.iterdir() if re.fullmatch(r'clickhouse-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip\.gpg',f.name))
for old in items[:-7]:old.unlink()
'''
        command(['python3','-c',prune]);remote(S2,['sudo','-n','python3','-c',prune])
        evidence={'status':'PASSED','completedAt':datetime.now(timezone.utc).isoformat(),
            'file':archive.name,'sha256':encrypted_digest,'nativeSha256':raw_digest,
            'bytes':archive.stat().st_size,'nativeBackupId':result['id'],'tables':tables,
            'copies':['a1','s2'],'database':'crawler_analytics','source':'s3',
            'decryptionAndZipCRC':'PASSED','scope':'Native full analytics backup; no system logs/users; cross-table transactional consistency not promised'}
        pending=DEST/'last-success.json.tmp';pending.write_text(json.dumps(evidence,indent=2)+'\n')
        os.replace(pending,DEST/'last-success.json')
        print(json.dumps(evidence))

if __name__=='__main__':main()
