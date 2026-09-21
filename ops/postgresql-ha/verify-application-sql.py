#!/usr/bin/env python3
"""Application checks used by verify-sql-tls.py --application-clients.

Only bounded existing validation workflows, signed submissions and synthetic objects.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import struct
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
m = runpy.run_path(str(Path(__file__).with_name('secure-application-sql.py')))
pg, nodes = m['pg'], m['nodes']


@contextlib.contextmanager
def forward(service, local, remote):
    child = subprocess.Popen(['kubectl', '-n', 'crawl-validation', 'port-forward',
        'service/' + service, str(local) + ':' + str(remote), '--address', '127.0.0.1'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if child.poll() is not None:
                raise RuntimeError('Port-forward exited: ' + service)
            try:
                with socket.create_connection(('127.0.0.1', local), timeout=.2):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError('Port-forward not ready: ' + service)
        yield
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()


def node_check(filename, env):
    completed = subprocess.run(['node', str(ROOT / filename)], env=env, cwd=ROOT,
        text=True, capture_output=True, timeout=200)
    if completed.returncode:
        # Keep diagnostics protected, never print env or account credentials.
        path = ROOT / 'secrets/postgresql-ha/sql-client-migration/application-check-error.txt'
        path.write_text(completed.stdout + '\n' + completed.stderr); path.chmod(0o600)
        raise RuntimeError(filename + ' failed; inspect protected application-check-error.txt')
    records = []
    for line in completed.stdout.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert records
    return records[-1]


def inspect_connections():
    result = {}
    for node in pg['NODES']:
        raw = pg['sql'](node, "SELECT json_agg(x) FROM (SELECT usename,datname,ssl,version,count(*) AS connections FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE client_addr IS NOT NULL GROUP BY 1,2,3,4) x;")
        rows = json.loads(raw or '[]')
        assert all(r['ssl'] for r in rows), node + ': plaintext PG connection'
        cfg = {r['key']: r['value'] for r in m['pool_admin'](node, 'SHOW CONFIG;')}
        assert cfg['client_tls_sslmode'] == 'require' and cfg['server_tls_sslmode'] == 'verify-full'
        clients = [{k: r.get(k) for k in ['user', 'database', 'addr', 'tls']} for r in m['pool_admin'](node, 'SHOW CLIENTS;') if r.get('addr') != 'unix']
        assert all(r['tls'] for r in clients)
        result[node] = {'pg': rows, 'poolClients': clients, 'poolClientMode': cfg['client_tls_sslmode'], 'poolServerMode': cfg['server_tls_sslmode']}
    rows = result[pg['leader']()]['pg']
    for role in ['crawl_cdc_validation', 'temporal_validation_runtime', 'crawler_ingestor_validation', 'seaweedfs_filer']:
        assert any(r['usename'] == role for r in rows), role + ': real connection missing'
    assert {r['datname'] for r in rows if r['usename'] == 'temporal_validation_runtime'} == {'temporal_validation', 'temporal_visibility_validation'}
    return result


def boundaries():
    out = {}
    ca = m['t']['PRIVATE'] / 'ca.crt'
    # Pool TLS negotiation is PostgreSQL SSLRequest, not direct HTTPS/TLS.
    for node, ip in pg['NODES'].items():
        out[node] = {}
        for host in [ip, m['t']['DNS'][2], '127.0.0.1']:
            ctx = ssl.create_default_context(cafile=str(ca))
            with socket.create_connection((ip, 6432), timeout=5) as sock:
                sock.sendall(struct.pack('!II', 8, 80877103)); assert sock.recv(1) == b'S'
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    out[node][host] = tls.version()
    # Exercise the exact Node pg version, environment and network path used by both real Ingestors.
    pods = json.loads(subprocess.check_output(['kubectl', '-n', 'crawl-validation', 'get', 'pods', '-l', 'app=data-ingestor', '-o', 'json'], text=True))['items']
    script = """import pg from 'pg';import {readFileSync} from 'node:fs';
let raw='';for await(const c of process.stdin)raw+=c;const x=JSON.parse(raw);
const client=new pg.Client({connectionTimeoutMillis:5000});
await client.connect();const frontend={authorized:client.connection.stream.authorized,version:client.connection.stream.getProtocol()};
if(!frontend.authorized)throw Error('Unauthenticated TLS');
const backend=(await client.query('SELECT ssl,version FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).rows[0];
if(!backend.ssl)throw Error('Unencrypted backend');
const ip=client.connection.stream.remoteAddress;
await client.query('BEGIN');await client.query('SELECT pg_advisory_xact_lock(243,2921)');await client.query('COMMIT');
for(let i=0;i<2;i++)await client.query({name:'tls-probe-statement',text:'SELECT $1::int AS n',values:[i]});
await client.end();const negatives={};
for(const [label,options] of [['plaintext',{ssl:false}],['wrongCA',{ssl:{ca:x.wrongCA,rejectUnauthorized:true}}],
 ['wrongName',{host:ip,ssl:{ca:readFileSync(process.env.NODE_EXTRA_CA_CERTS,'utf8'),servername:'invalid-sql.invalid',rejectUnauthorized:true}}]]){
 const c=new pg.Client({...options,connectionTimeoutMillis:5000});let rejected=false;
 try{await c.connect();}catch(e){const match=label==='plaintext'?/SSL required|TLS required/i.test(e.message):label==='wrongName'?e.code==='ERR_TLS_CERT_ALTNAME_INVALID':/certificate|issuer|self.signed/i.test(e.message);if(!match)throw e;rejected=true;}finally{await c.end();}
 if(!rejected)throw Error(label+' was accepted');negatives[label]='rejected';
}console.log(JSON.stringify({frontend,backend,negatives,transactionAndPreparedStatement:'passed'}));"""
    out['ingestorClients'] = {}
    for pod in pods:
        result = subprocess.check_output(['kubectl', '-n', 'crawl-validation', 'exec', '-i', pod['metadata']['name'], '--',
            'node', '--input-type=module', '-e', script], input=json.dumps({'wrongCA': (ROOT / 'secrets/monitoring/ca.crt').read_text()}), text=True, timeout=60)
        out['ingestorClients'][pod['metadata']['name']] = json.loads(result)
    assert len(out['ingestorClients']) == 2
    return out


def plaintext_boundaries():
    """All six listeners reject a plaintext startup before requesting a password."""
    results = []
    def receive(sock, count):
        data = b''
        while len(data) < count:
            part = sock.recv(count - len(data))
            if not part:
                raise RuntimeError('Unexpected PostgreSQL protocol EOF')
            data += part
        return data
    for node, ip in pg['NODES'].items():
        for port in [5432, 6432]:
            for role, database in [('crawler', 'crawler'), ('temporal_validation_owner', 'temporal_validation')]:
                params = struct.pack('!I', 196608) + ('user\0' + role + '\0database\0' + database + '\0\0').encode()
                with socket.create_connection((ip, port), timeout=5) as sock:
                    sock.sendall(struct.pack('!I', len(params) + 4) + params)
                    header = receive(sock, 5)
                    size = struct.unpack('!I', header[1:])[0]
                    assert header[:1] == b'E' and 4 <= size < 4096
                    error = receive(sock, size - 4).decode()
                    assert ('no encryption' in error and 'pg_hba.conf rejects' in error) if port == 5432 else 'SSL required' in error
                results.append({'node': node, 'port': port, 'user': role, 'plaintext': 'rejected before password exchange'})
    return results


def verify_phase(phase):
    import boto3
    from botocore.config import Config
    env = dict(os.environ, PGSSLMODE='verify-full', NODE_EXTRA_CA_CERTS=str(m['t']['PRIVATE'] / 'ca.crt'))
    owner = runpy.run_path(str(ROOT / 'ops/postgresql-ha/prepare-patroni.py'))['envfile'](ROOT / 'secrets/validation-storage.env')
    # Use known /etc/hosts node names so pg passes an explicit DNS servername to Node TLS.
    env.update(PGHOST=pg['leader'](), PGPORT='5432', PGUSER='crawler', PGPASSWORD=owner['CRAWLER_PASSWORD'],
        PGDATABASE='crawler_validation_ingestor', CONSUMER_PGHOST=pg['leader'](), CONSUMER_PGPORT='6432', REPLAY_ROUNDS='1')
    result = {}
    with forward('data-ingestor', 18081, 8080):
        filename = 'ops/ingestor/verify-validation.mjs' if phase == 'before-switch' else 'ops/ingestor/verify-restart.mjs'
        result['ingestor'] = node_check(filename, env)
    with forward('temporal', 17233, 7233):
        result['temporal'] = node_check('ops/temporal/verify-validation.mjs', env)
    key = 'infra/sql-tls-' + uuid.uuid4().hex + '.bin'
    body = ('SQL-TLS-' + phase + '\n').encode() * 16
    keys = json.loads((ROOT / 'secrets/seaweedfs/s3-credentials.json').read_text())['worker']
    clients = [boto3.client('s3', endpoint_url='http://' + ip + ':8333',
        aws_access_key_id=keys['accessKey'], aws_secret_access_key=keys['secretKey'], region_name='us-east-1',
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}, connect_timeout=3, read_timeout=15, retries={'max_attempts': 2})) for ip in pg['NODES'].values()]
    bucket = 'crawl-validation-objects'
    try:
        clients[0].put_object(Bucket=bucket, Key=key, Body=body)
        for client in clients:
            assert client.get_object(Bucket=bucket, Key=key)['Body'].read() == body
            assert key in [x['Key'] for x in client.list_objects_v2(Bucket=bucket, Prefix=key)['Contents']]
        clients[1].put_object(Bucket=bucket, Key=key, Body=body + b'updated')
        assert clients[2].get_object(Bucket=bucket, Key=key)['Body'].read() == body + b'updated'
        for node in pg['NODES']:
            assert m['filer_ready'](node)
        result['objects'] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'threeGateways': 'put/get/list/overwrite passed'}
    finally:
        clients[0].delete_object(Bucket=bucket, Key=key)
    result['connections'] = inspect_connections()
    return result
