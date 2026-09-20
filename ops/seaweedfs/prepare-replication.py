#!/usr/bin/env python3
"""Apply two copies on separate volume servers in the existing single-rack topology."""
import argparse
import json
from pathlib import Path
import runpy
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
helpers = runpy.run_path(str(ROOT / 'ops/backup/bootstrap-pgbackrest.py'))
run, put = helpers['run'], helpers['put']
NODES = {'s1': '10.4.4.2', 's2': '10.4.4.8', 's3': '10.4.4.5'}
MASTERS = ','.join(ip + ':9333' for ip in NODES.values())

def get(ip, path):
    return json.load(urllib.request.urlopen('http://' + ip + ':9333' + path, timeout=5))

def shell(commands):
    return run(NODES['s3'], 'timeout 120s /opt/seaweedfs/weed shell -master=' + MASTERS + ' -filer=10.4.4.5:8888',
               '\n'.join(commands + ['exit']) + '\n', capture=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.parse_args()
    backups = list((ROOT / 'ops/checks').glob('seaweed-baseline-*.json'))
    assert backups and json.loads(sorted(backups)[-1].read_text())['status'] == 'PASSED', 'Baseline backup required'
    before = get(NODES['s1'], '/dir/status')
    listing_before = shell(['volume.list']).stdout
    # Remove service-stop cascades: a local master loss must not also stop its volume/filer.
    for name, ip in NODES.items():
        for service in ['volume'] + (['filer', 's3'] if name == 's3' else []):
            put(ip, f'/etc/systemd/system/seaweedfs-{service}.service.d/20-independent.conf',
                '[Unit]\nRequires=\nWants=network-online.target\n', mode=0o644)
        cmd = (f'/opt/seaweedfs/weed master -ip={ip} -ip.bind={ip} -port=9333 '
               f'-mdir=/srv/crawlsystem/seaweedfs/master -peers={MASTERS} -defaultReplication=001')
        put(ip, '/etc/systemd/system/seaweedfs-master.service.d/20-replication.conf',
            '[Service]\nExecStart=\nExecStart=' + cmd + '\n', mode=0o644)
        run(ip, 'sudo systemctl daemon-reload')
    for name in ['s3', 's2', 's1']:
        ip = NODES[name]
        run(ip, 'sudo systemctl restart seaweedfs-master')
        for _ in range(30):
            try:
                result = get(ip, '/cluster/status')
                if result.get('Leader') or result.get('leader'):
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError(name + ' master did not become ready')
        assert run(ip, 'systemctl is-active seaweedfs-volume', capture=True).stdout.strip() == 'active'
    # Explicitly enumerate the original volume IDs; never delete surplus copies.
    ids = set()
    for dc in before['Topology']['DataCenters']:
        for rack in dc['Racks']:
            for node in rack['DataNodes']:
                for token in node['VolumeIds'].split():
                    first, _, last = token.partition('-')
                    ids.update(range(int(first), int(last or first) + 1))
    commands = ['lock'] + [f'volume.configure.replication -volumeId={vid} -replication=001' for vid in sorted(ids)]
    commands += ['volume.fix.replication -apply -doDelete=false -maxParallelization=1', 'unlock', 'volume.list']
    result = shell(commands)
    assert 'error:' not in (result.stdout + result.stderr).lower(), result.stdout + result.stderr
    time.sleep(3)
    ip = NODES['s3']
    cmd = (f'/opt/seaweedfs/weed filer -ip={ip} -ip.bind={ip} -port=8888 '
           f'-defaultStoreDir=/srv/crawlsystem/seaweedfs/filer -master={MASTERS} -defaultReplicaPlacement=001')
    put(ip, '/etc/systemd/system/seaweedfs-filer.service.d/20-replication.conf',
        '[Service]\nExecStart=\nExecStart=' + cmd + '\n', mode=0o644)
    # Stop the local S3 subscriber before graceful filer restart; otherwise its
    # long-lived gRPC stream can keep GracefulStop waiting until systemd kills it.
    run(ip, 'sudo systemctl daemon-reload && sudo systemctl stop seaweedfs-s3')
    try:
        run(ip, 'sudo systemctl restart seaweedfs-filer')
    finally:
        run(ip, 'sudo systemctl start seaweedfs-filer seaweedfs-s3')
    after = get(NODES['s1'], '/dir/status')
    assert all(x['replication'] == '001' for x in after['Topology']['Layouts']), after
    evidence = {'status': 'PASSED', 'replication': '001', 'meaning': 'two copies on different servers in the same rack',
                'before': before, 'after': after, 'volumeListBefore': listing_before,
                'migrationOutput': result.stdout + result.stderr,
                'volumeListAfter': shell(['volume.list']).stdout,
                'oldVolumeIds': sorted(ids), 'deletionAllowed': False}
    (ROOT / 'ops/checks/2026-09-20-seaweed-replication.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'status': 'PASSED', 'replication': '001', 'oldVolumeIds': sorted(ids)}))
