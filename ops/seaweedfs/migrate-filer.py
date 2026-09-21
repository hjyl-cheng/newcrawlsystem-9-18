#!/usr/bin/env python3
"""One-time local LevelDB -> existing PG schema migration, preserving old files and metadata."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import shlex
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
h = runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run, put = h['run'], h['put']
S3 = '10.4.4.5'
MASTERS = '10.4.4.2:9333,10.4.4.8:9333,10.4.4.5:9333'

def entries(path='/'):
    request = urllib.request.Request('http://'+S3+':8888'+path+'?pretty=y', headers={'Accept':'application/json'})
    result = []
    for entry in json.load(urllib.request.urlopen(request, timeout=10)).get('Entries') or []:
        result.append(entry)
        if entry['Mode'] & 2147483648:
            result.extend(entries(entry['FullPath']+'/'))
    return result

def shell(commands):
    r = run(S3, 'sudo timeout 60s /opt/seaweedfs/weed shell -master='+MASTERS+' -filer='+S3+':8888',
            '\n'.join(commands+['exit'])+'\n', capture=True)
    assert 'error:' not in (r.stdout+r.stderr).lower(), 'SeaweedFS metadata command failed; check protected host logs'
    return r.stdout+r.stderr

def wait_ready():
    for _ in range(60):
        try:
            entries();return
        except Exception: time.sleep(.5)
    raise RuntimeError('Filer did not become ready')

if __name__ == '__main__':
    assert run(S3, 'test ! -f /etc/seaweedfs/filer.toml && echo ready', capture=True).stdout.strip() == 'ready', 'Already configured: do not reimport a stale snapshot'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    private = '/srv/crawlsystem/seaweedfs/migration-' + stamp
    run(S3, 'sudo install -d -m 0700 '+private)
    canary_path = '/crawlsystem-validation/metadata-migration-'+stamp+'.bin'
    payload = bytes(range(256))*1024
    urllib.request.urlopen(urllib.request.Request('http://'+S3+':8888'+canary_path, method='PUT',data=payload), timeout=15).close()
    before = entries()
    files = []
    for entry in before:
        if not entry['Mode'] & 2147483648:
            data = urllib.request.urlopen('http://'+S3+':8888'+entry['FullPath'],timeout=15).read()
            files.append({'path':entry['FullPath'],'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    put(S3, private+'/inventory-before.json', json.dumps({'entries':before,'files':files}), mode=0o600)
    run(S3, 'sudo systemctl stop seaweedfs-s3')
    # Traversal excludes *every descendant* of the internal log directory even
    # when starting at that directory. Export each exact entry as the root.
    exports = [(private+'/root.meta.gz','/')]
    exports += [(private+f'/log-{index}.meta.gz',entry['FullPath']) for index,entry in enumerate(before)
                if entry['FullPath'].startswith('/topics/.system/log/')]
    shell(['fs.meta.save -o '+file+' '+path for file,path in exports])
    # Version 4.47 skips log descendants and returns at EOF before waiting for
    # its final file goroutines. Append an existing directory to force a wait.
    barrier = private+'/directory-barrier.meta.gz'
    shell(['fs.meta.save -o '+barrier+' /topics/.system/log'])
    complete_script = '''import gzip,sys
from pathlib import Path
barrier=gzip.decompress(Path(sys.argv[1]).read_bytes())
for name in sys.argv[2:]:
 p=Path(name)
 p.write_bytes(gzip.compress(gzip.decompress(p.read_bytes())+barrier))
'''
    run(S3, 'sudo python3 - '+shlex.quote(barrier)+' '+ ' '.join(shlex.quote(file) for file,path in exports), complete_script, capture=True)
    run(S3, 'sudo systemctl stop seaweedfs-filer')
    run(S3, 'sudo tar -czf '+private+'/leveldb-before-pg.tar.gz -C /srv/crawlsystem/seaweedfs filer')
    run(S3, 'sudo install -m 0600 -o seaweedfs -g seaweedfs /etc/seaweedfs/filer-postgres.pending.toml /etc/seaweedfs/filer.toml')
    command = (f'/opt/seaweedfs/weed filer -ip={S3} -ip.bind={S3} -port=8888 '
               f'-master={MASTERS} -filerGroup=crawl-objects -defaultReplicaPlacement=001 '
               '-concurrentFileUploadLimit=4 -concurrentUploadLimitMB=16 -maxMB=4')
    put(S3, '/etc/systemd/system/seaweedfs-filer.service.d/30-ha.conf',
        '[Unit]\nAfter=crawl-seaweed-pg.service\nWants=crawl-seaweed-pg.service\n[Service]\nWorkingDirectory=/etc/seaweedfs\nExecStart=\nExecStart='+command+'\n', mode=0o644)
    # From here a failure leaves the public S3 entry stopped. Never silently roll
    # back PG data or old LevelDB after new metadata may have been acknowledged.
    run(S3, 'sudo systemctl daemon-reload && sudo systemctl start seaweedfs-filer')
    wait_ready()
    shell(['fs.meta.load -v=false -concurrency=1 '+file for file,path in exports])
    after = entries()
    assert set(e['FullPath'] for e in before).issubset(set(e['FullPath'] for e in after))
    for original in files:
        data = urllib.request.urlopen('http://'+S3+':8888'+original['path'],timeout=15).read()
        assert len(data)==original['size'] and hashlib.sha256(data).hexdigest()==original['sha256'], original['path']
    run(S3, 'sudo systemctl start seaweedfs-s3')
    evidence = {'status':'PASSED','store':'crawler.object_metadata.filemeta', 'beforeEntries':len(before),
                'afterEntries':len(after),'allOriginalPathsPresent':True,'files':files,
                'canaryPath':canary_path,'protectedSourceBackup':private,
                'oldLevelDB':'retained untouched; no automatic rollback after cutover',
                'rootExportSystemLogs':'each historical log entry exported individually; recursive export skips all log descendants'}
    (ROOT/'ops/checks/2026-09-21-seaweed-metadata-migration.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps({k:v for k,v in evidence.items() if k!='files'}))
