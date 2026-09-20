#!/usr/bin/env python3
"""One volume-server outage with a live filer; small, isolated test files only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
FILER = 'http://10.4.4.5:8888'
MASTER = 'http://10.4.4.2:9333'
SSH = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']

def request(url, method='GET', data=None):
    return urllib.request.urlopen(urllib.request.Request(url, method=method, data=data,
                                  headers={'Content-Type': 'application/octet-stream', 'Accept': 'application/json'}), timeout=15)

def remote(ip, command):
    return subprocess.check_output(SSH + ['ubuntu@' + ip, command], text=True, timeout=40).strip()

def locations(entry):
    result = []
    for chunk in entry['chunks']:
        vid = chunk['file_id'].split(',')[0]
        detail = json.load(request(MASTER + '/dir/lookup?volumeId=' + vid))
        hosts = [loc['url'].split(':')[0] for loc in detail['locations']]
        result.append({'volumeId': vid, 'hosts': hosts})
    return result

def entry(path):
    entries = json.load(request(FILER + '/crawlsystem-validation/?pretty=y'))['Entries']
    return next(e for e in entries if e['FullPath'] == path)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.parse_args()
    identifier = uuid.uuid4().hex
    paths = ['/crawlsystem-validation/infra-volume-' + identifier + '-' + phase + '.bin' for phase in ['before', 'during']]
    payload = bytes(range(256)) * 1024
    digest = hashlib.sha256(payload).hexdigest()
    unit = 'crawl-seaweed-volume-recover-' + identifier
    evidence = {'status': 'RUNNING', 'fault': 'One volume service stopped; master and single filer remain running',
                'fileBytes': len(payload), 'sha256': digest, 'startedAt': time.time(), 'paths': paths}
    target = None
    armed = False
    try:
        assert request(FILER + paths[0], 'PUT', payload).status == 201
        before = locations(entry(paths[0]))
        assert all(len(set(item['hosts'])) == 2 for item in before)
        target = before[0]['hosts'][0]
        assert target in ['10.4.4.2', '10.4.4.8', '10.4.4.5']
        evidence['beforeLocations'] = before
        remote(target, f'sudo systemd-run --unit={unit} --on-active=3m /usr/bin/systemctl start seaweedfs-volume')
        armed = True
        started = time.monotonic()
        remote(target, 'sudo systemctl stop seaweedfs-volume')
        assert remote(target, 'systemctl show seaweedfs-volume -p ActiveState --value') == 'inactive'
        evidence['stoppedHost'] = target
        evidence['stopSeconds'] = round(time.monotonic() - started, 3)
        # Wait for topology convergence before checking surviving copy placement.
        for _ in range(40):
            remaining = locations(entry(paths[0]))
            if all(target not in item['hosts'] for item in remaining):
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Stopped volume server remained registered')
        assert hashlib.sha256(request(FILER + paths[0]).read()).hexdigest() == digest
        assert request(FILER + paths[1], 'PUT', payload).status == 201
        during = locations(entry(paths[1]))
        assert all(len(set(item['hosts'])) == 2 and target not in item['hosts'] for item in during)
        assert hashlib.sha256(request(FILER + paths[1]).read()).hexdigest() == digest
        evidence['remainingLocations'] = remaining
        evidence['writtenDuringOutageLocations'] = during
        started = time.monotonic()
        remote(target, 'sudo systemctl start seaweedfs-volume')
        for _ in range(40):
            restored = locations(entry(paths[0]))
            if all(len(set(item['hosts'])) == 2 for item in restored):
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Original replica did not re-register')
        evidence['restoreSeconds'] = round(time.monotonic() - started, 3)
        for path in paths:
            assert hashlib.sha256(request(FILER + path).read()).hexdigest() == digest
        evidence['restoredLocations'] = restored
        evidence['readBeforeAndDuringFilesAfterRecovery'] = 'PASSED'
        evidence['status'] = 'PASSED'
    except Exception as error:
        evidence['status'] = 'FAILED'
        evidence['error'] = str(error)
        raise
    finally:
        if armed:
            remote(target, 'sudo systemctl start seaweedfs-volume')
            assert remote(target, 'systemctl is-active seaweedfs-volume') == 'active'
            remote(target, f'sudo systemctl stop {unit}.timer')
        for path in paths:
            try:
                request(FILER + path, 'DELETE').close()
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        evidence['finishedAt'] = time.time()
        (ROOT / 'ops/checks/2026-09-20-seaweed-volume-failover.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))

if __name__ == '__main__':
    main()
