#!/usr/bin/env python3
"""Stage an additional mTLS broker listener; keep existing clients/quorum intact.

This does NOT enable ACLs or secure the existing broker/controller transports.
Each mutation has a host-local timed rollback, cancelled after ISR/quorum recovery.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('nodes', ROOT/'ops/monitoring/bootstrap-nodes.py')
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)
NODES = {n: nodes.NODES[n] for n in ('s1', 's2', 's3')}
PRIVATE = ROOT/'secrets/kafka/pki'
CONFIG = '/etc/kafka/server.properties'
REMOTE = '/etc/kafka/tls'


def run(node, command, data=None):
    return subprocess.run([*nodes.SSH, '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=/home/ubuntu/.ssh/known_hosts',
        'ubuntu@'+NODES[node], command], input=data, text=True,
        capture_output=True, check=True, timeout=180)


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True)


def prepare_pki():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (PRIVATE/'ca.crt').exists():
        assert not (PRIVATE/'ca.key').exists(), 'Partial CA exists; inspect before proceeding'
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
            '-nodes', '-keyout', PRIVATE/'ca.key', '-out', PRIVATE/'ca.crt', '-days', '1825',
            '-subj', '/CN=crawl-kafka-ca', '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    # Only broker and operator identities issued now. Applications get separate identities at migration.
    for name in [*NODES, 'operator']:
        if (PRIVATE/(name+'.crt')).exists():
            openssl('verify', '-CAfile', PRIVATE/'ca.crt', PRIVATE/(name+'.crt'))
            openssl('x509', '-in', PRIVATE/(name+'.crt'), '-checkend', '2592000', '-noout')
            assert (PRIVATE/(name+'.key')).exists()
            continue
        assert not (PRIVATE/(name+'.key')).exists(), 'Partial identity exists: '+name
        openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
            '-keyout', PRIVATE/(name+'.key'), '-out', PRIVATE/(name+'.csr'), '-subj', '/CN=crawl-kafka-'+name)
        ext = 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage='
        ext += 'serverAuth,clientAuth\n' if name in NODES else 'clientAuth\n'
        if name in NODES:
            ext += 'subjectAltName=IP:'+NODES[name]+',DNS:'+name+'\n'
        (PRIVATE/(name+'.ext')).write_text(ext)
        openssl('x509', '-req', '-in', PRIVATE/(name+'.csr'), '-CA', PRIVATE/'ca.crt',
            '-CAkey', PRIVATE/'ca.key', '-CAcreateserial', '-out', PRIVATE/(name+'.crt'),
            '-days', '365', '-sha256', '-extfile', PRIVATE/(name+'.ext'))


def render(original, node):
    props = dict(line.split('=', 1) for line in original.splitlines() if '=' in line and not line.startswith('#'))
    assert props['process.roles'] == 'broker,controller'
    assert props['inter.broker.listener.name'] == 'PLAINTEXT', 'This tool only handles first-stage migration'
    assert props['controller.listener.names'] == 'CONTROLLER'
    assert props['controller.quorum.voters'] == '1@10.4.4.2:9093,2@10.4.4.8:9093,3@10.4.4.5:9093'
    assert 'authorizer.class.name' not in props, 'Review existing authorization before using first-stage tool'
    ip = NODES[node]
    assert props['node.id'] == node[1:]
    assert props['listeners'] in [f'PLAINTEXT://{ip}:9092,CONTROLLER://{ip}:9093',
        f'PLAINTEXT://{ip}:9092,CONTROLLER://{ip}:9093,SECURE://{ip}:9094'], 'Unexpected listener topology'
    changes = {
        'listeners': f'PLAINTEXT://{ip}:9092,CONTROLLER://{ip}:9093,SECURE://{ip}:9094',
        'advertised.listeners': f'PLAINTEXT://{ip}:9092,SECURE://{ip}:9094',
        'listener.security.protocol.map': 'CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,SECURE:SSL',
        'listener.name.secure.ssl.keystore.type': 'PEM',
        'listener.name.secure.ssl.keystore.location': REMOTE+'/node.pem',
        'listener.name.secure.ssl.truststore.type': 'PEM',
        'listener.name.secure.ssl.truststore.location': REMOTE+'/ca.crt',
        'listener.name.secure.ssl.client.auth': 'required',
        'listener.name.secure.ssl.enabled.protocols': 'TLSv1.2,TLSv1.3',
        'listener.name.secure.ssl.endpoint.identification.algorithm': 'https',
    }
    lines = [line for line in original.splitlines() if line.split('=', 1)[0] not in changes]
    return '\n'.join(lines + [k+'='+v for k,v in changes.items()])+'\n'


def health():
    result = subprocess.run(['node', str(ROOT/'ops/kafka/check-health.mjs')],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def quorum(node, tls=False):
    config = ' --command-config '+REMOTE+'/node-client.properties' if tls else ''
    port = '9094' if tls else '9092'
    return run(node, 'sudo -u kafka /opt/kafka/bin/kafka-metadata-quorum.sh --bootstrap-server '+
        NODES[node]+':'+port+config+' describe --status').stdout


def stable(node):
    deadline = time.monotonic()+150
    while True:
        try:
            h = health()
            q = quorum(node)
            assert any(line.split(':',1)[1].strip() == '0' for line in q.splitlines() if line.startswith('MaxFollowerLag:'))
            assert all(run(n, 'systemctl is-active kafka').stdout.strip() == 'active' for n in NODES)
            return {'health': h, 'quorum': q}
        except (subprocess.SubprocessError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def local_api(node):
    # A Java AdminClient may discover and select another broker after bootstrap.
    # Until new ports are reachable cross-host, probe this local socket explicitly.
    deadline = time.monotonic()+60
    while True:
        try:
            return json.loads(run(node, 'sudo -u kafka python3 - '+NODES[node],
                Path(__file__).with_name('probe-local-tls.py').read_text()).stdout)
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', required=True, action='store_true')
    parser.parse_args()
    os.umask(0o077)
    evidence = {'startedAt': datetime.now(timezone.utc).isoformat(), 'status': 'RUNNING', 'nodes': {},
        'scope': 'Additional broker mTLS listener only; existing 9092/9093 and ACL migration pending'}
    path = ROOT/'ops/checks/2026-09-21-kafka-mtls-listener.json'
    try:
        evidence['before'] = stable('s1')
        prepare_pki()
        # Keep the current quorum leader until last where possible.
        leader = next(int(l.split(':',1)[1]) for l in evidence['before']['quorum'].splitlines() if l.startswith('LeaderId:'))
        order = sorted(NODES, key=lambda n: int(n[1:]) == leader)
        for node in order:
            stable(node)
            original = run(node, 'sudo cat '+CONFIG).stdout
            updated = render(original, node)
            snapshot = ROOT/'secrets/kafka/migration'/str(uuid.uuid4())
            snapshot.mkdir(parents=True, mode=0o700)
            (snapshot/(node+'.properties')).write_text(original)
            run(node, 'sudo install -d -m 0700 -o kafka -g kafka '+REMOTE)
            nodes.put(node, REMOTE+'/ca.crt', (PRIVATE/'ca.crt').read_text(), 'kafka')
            nodes.put(node, REMOTE+'/node.pem', (PRIVATE/(node+'.key')).read_text()+(PRIVATE/(node+'.crt')).read_text(), 'kafka')
            nodes.put(node, REMOTE+'/node.crt', (PRIVATE/(node+'.crt')).read_text(), 'kafka')
            nodes.put(node, REMOTE+'/node.key', (PRIVATE/(node+'.key')).read_text(), 'kafka')
            client = 'security.protocol=SSL\nssl.keystore.type=PEM\nssl.keystore.location='+REMOTE+'/node.pem\n'
            client += 'ssl.truststore.type=PEM\nssl.truststore.location='+REMOTE+'/ca.crt\nssl.endpoint.identification.algorithm=https\nrequest.timeout.ms=10000\ndefault.api.timeout.ms=20000\n'
            nodes.put(node, REMOTE+'/node-client.properties', client, 'kafka')
            if updated != original:
                rollback = '/etc/kafka/rollback-'+snapshot.name
                nodes.put(node, rollback+'.properties', original, 'kafka')
                nodes.put(node, rollback+'.sh', '#!/bin/bash\nset -euo pipefail\ninstall -o kafka -g kafka -m 0640 '+rollback+'.properties '+CONFIG+'\nsystemctl restart kafka\n', mode=0o700)
                unit = 'crawl-kafka-tls-rollback-'+snapshot.name
                run(node, 'sudo systemd-run --unit='+unit+' --on-active=8m /bin/bash '+rollback+'.sh')
                try:
                    nodes.put(node, CONFIG, updated, 'kafka', 0o640)
                    print(node+': restarting one broker/controller; timed rollback armed', flush=True)
                    run(node, 'sudo systemctl restart kafka')
                    recovered = stable(node)
                    tls = local_api(node)
                except BaseException:
                    run(node, 'sudo /bin/bash '+rollback+'.sh')
                    stable(node)
                    run(node, 'sudo systemctl stop '+unit+'.timer')
                    run(node, 'sudo rm -- '+rollback+'.sh '+rollback+'.properties')
                    raise
                run(node, 'sudo systemctl stop '+unit+'.timer')
                run(node, 'sudo rm -- '+rollback+'.sh '+rollback+'.properties')
            else:
                recovered = stable(node)
                tls = local_api(node)
            evidence['nodes'][node] = {'tlsLocalApi': tls, 'recovered': recovered}
            print(node+': mTLS Kafka API works locally; all partitions ISR=3 and quorum caught up', flush=True)
        evidence['status'] = 'LISTENER_STAGED'
    except BaseException as error:
        evidence['status'] = 'FAILED'
        evidence['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        evidence['finishedAt'] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    main()
