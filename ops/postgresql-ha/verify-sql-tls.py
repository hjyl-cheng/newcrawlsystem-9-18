#!/usr/bin/env python3
"""Verify SQL TLS boundaries and real CDC delivery across controlled switchovers."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import shlex
import ssl
import subprocess
import time
import urllib.request
import urllib.error
import uuid

ROOT = Path(__file__).resolve().parents[2]
t = runpy.run_path(str(Path(__file__).with_name('secure-sql-transport.py')))
pg, nodes = t['pg'], t['nodes']


def kube(*args):
    return subprocess.check_output(['kubectl', *args], text=True, timeout=90)


def wait(check, seconds=150):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (subprocess.CalledProcessError, urllib.error.URLError, TimeoutError, StopIteration):
            pass
        time.sleep(2)
    raise RuntimeError('TLS verification condition not reached before deadline')


def grafana():
    addr = json.loads(kube('-n', 'crawl-monitoring', 'get', 'svc', 'grafana', '-o', 'json'))['spec']['clusterIP']
    auth = base64.b64encode(('admin:' + (ROOT / 'secrets/monitoring/grafana-admin-password').read_text().strip()).encode()).decode()
    def get(path):
        req = urllib.request.Request('http://' + addr + ':3000' + path,
                                     headers={'Authorization': 'Basic ' + auth})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)
    assert get('/api/health')['database'] == 'ok'
    settings = get('/api/admin/settings')['database']
    assert settings['ssl_mode'] == 'verify-full'
    assert settings['ca_cert_path'] == '/etc/postgresql-sql-tls/ca.crt'
    assert get('/api/datasources/uid/clickhouse-logs/health')['status'] == 'OK'
    assert get('/api/dashboards/uid/crawl-infrastructure')['dashboard']['uid'] == 'crawl-infrastructure'
    assert get('/api/dashboards/uid/crawl-logs')['dashboard']['uid'] == 'crawl-logs'
    rows = pg['sql'](pg['leader'](), "SELECT json_agg(json_build_object('client',client_addr,'ssl',ssl,'version',version)) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE usename='crawl_grafana';")
    actual = json.loads(rows)
    assert actual and len(actual) >= 2 and all(r['ssl'] for r in actual)
    return {'sslMode': settings['ssl_mode'], 'database': 'ok', 'connections': actual,
            'logsDatasource': 'OK', 'dashboards': 2}


def replication():
    primary = pg['leader']()
    rows = json.loads(pg['sql'](primary, "SELECT json_agg(json_build_object('name',application_name,'ssl',ssl,'version',version,'state',state)) FROM pg_stat_replication r JOIN pg_stat_ssl s USING(pid) WHERE application_name IN ('s1','s2','s3');") or '[]')
    if not rows or len(rows) != 2 or not all(r['ssl'] and r['state'] == 'streaming' for r in rows):
        return None
    for node in pg['NODES']:
        if not pg['api'](node, '/patroni').get('timeline'):
            return None
        if node != primary:
            if pg['sql'](node, "SELECT count(*) FROM pg_stat_wal_receiver WHERE status='streaming' AND conninfo LIKE '%sslmode=verify-full%';") != '1':
                return None
    guard = json.loads(nodes['run'](primary, 'sudo cat /var/lib/crawl-cdc-guard/status.json').stdout)
    if guard['state'] != 'READY' or set(guard['candidates']) != set(pg['NODES']) - {primary} or time.time() - guard['checked_at'] > 15:
        return None
    return {'primary': primary, 'senders': rows, 'guardCandidates': guard['candidates']}


def boundaries():
    result = {}
    for name, ip in pg['NODES'].items():
        item = {'ip': t['handshake'](ip), 'serviceName': t['handshake'](ip, t['DNS'][2])}
        for mode, hostname, ca in [('wrongName', 'wrong-postgresql.invalid', None),
                                   ('wrongCA', ip, ROOT / 'secrets/monitoring/ca.crt')]:
            try:
                t['handshake'](ip, hostname, ca)
                raise AssertionError(mode + ' was accepted')
            except ssl.SSLCertVerificationError:
                item[mode] = 'rejected'
        result[name] = item
    # Direct SQL login from A1; Service access requires a permitted in-cluster client.
    script = """import pg from 'pg'; let raw='';for await(const chunk of process.stdin)raw+=chunk;
const x=JSON.parse(raw), rows=[];
for(const host of x.hosts){
 const client=new pg.Client({host,port:5432,user:'crawl_grafana',database:'crawler_grafana',password:x.password,
  connectionTimeoutMillis:5000,ssl:{ca:x.ca,rejectUnauthorized:true,servername:x.servername}});
 try{await client.connect();const r=await client.query('SELECT ssl,version FROM pg_stat_ssl WHERE pid=pg_backend_pid()');
 if(!r.rows[0]?.ssl)throw Error('TLS missing');rows.push({host,...r.rows[0]});}catch(e){throw Error('TLS probe failed at '+host+': '+e.message);}finally{await client.end();}
 const plain=new pg.Client({host,port:5432,user:'crawl_grafana',database:'crawler_grafana',password:x.password,connectionTimeoutMillis:5000,ssl:false});
 let rejected=false;try{await plain.connect();}catch(e){if(e.code!=='28000')throw e;rejected=true;}finally{await plain.end();}
 if(!rejected)throw Error('Plaintext allowed');rows.at(-1).plaintext='rejected';
}console.log(JSON.stringify(rows));"""
    payload = {'hosts': list(pg['NODES'].values()), 'servername': t['DNS'][2],
               'password': (ROOT / 'secrets/monitoring/grafana-db-password').read_text().strip(),
               'ca': (t['PRIVATE'] / 'ca.crt').read_text()}
    result['grafanaSQL'] = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script],
                                  input=json.dumps(payload), text=True, timeout=45))
    payload['hosts'] = [t['DNS'][2]]
    name = 'sql-tls-probe-' + uuid.uuid4().hex[:8]
    image = kube('-n', 'crawl-validation', 'get', 'deployment', 'data-ingestor', '-o',
                 'jsonpath={.spec.template.spec.containers[0].image}')
    labels = {'postgres-client': 'true', 'sql-tls-probe': name}
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': 'crawl-validation', 'labels': labels},
           'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 180, 'automountServiceAccountToken': False,
                    'tolerations': [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'}],
                    'containers': [{'name': 'probe', 'image': image, 'command': ['node', '-e', 'setInterval(()=>{},1000)'],
                                    'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
                                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'allowPrivilegeEscalation': False,
                                                        'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}}]}}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': name, 'namespace': 'crawl-validation'},
              'spec': {'podSelector': {'matchLabels': {'sql-tls-probe': name}}, 'policyTypes': ['Ingress', 'Egress'], 'egress': [
                  {'to': [{'podSelector': {'matchLabels': {'app': 'postgresql-entry'}}}], 'ports': [{'protocol': 'TCP', 'port': 5432}]},
                  {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                   'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}}
    try:
        for obj in [policy, pod]:
            subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True, check=True, capture_output=True)
        kube('-n', 'crawl-validation', 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=60s')
        result['grafanaSQLThroughService'] = json.loads(subprocess.check_output(
            ['kubectl', '-n', 'crawl-validation', 'exec', '-i', name, '--',
             'node', '--input-type=module', '-e', script], input=json.dumps(payload), text=True, timeout=30))
    finally:
        kube('-n', 'crawl-validation', 'delete', 'pod,networkpolicy', name, '--ignore-not-found', '--wait=false')
    # Test rewind login and both replicator startup modes from an allowed S host.
    probe = """import json,sys,psycopg2
x=json.load(sys.stdin);out=[]
for dest in x['hosts']+['localhost','127.0.0.1']:
 for role,options in [('replication',{}),('replication',{'replication':'true'}),('rewind',{})]:
  if role=='rewind' and dest in ['localhost','127.0.0.1']:continue
  auth=x['auth'][role]
  base=dict(host=dest,port=5432,dbname='postgres',user=auth['username'],password=auth['password'],connect_timeout=5,**options)
  conn=psycopg2.connect(**base,sslmode='verify-full',sslrootcert=x['ca']);conn.autocommit=True
  with conn.cursor() as cur:cur.execute('IDENTIFY_SYSTEM' if options else 'SELECT 1');cur.fetchone()
  conn.close()
  try:
   conn=psycopg2.connect(**base,sslmode='disable');conn.close()
  except psycopg2.OperationalError as e:
   assert 'pg_hba.conf rejects' in str(e) and 'no encryption' in str(e),str(e)
  else:raise AssertionError('Plaintext was allowed')
  out.append(dict(host=dest,role=role,protocol='physical' if options else 'sql',tls='verified',plaintext='rejected'))
print(json.dumps(out))
"""
    for source in pg['NODES']:
        cfg = t['config'](source)
        payload = {'hosts': list(pg['NODES'].values()), 'auth': cfg['postgresql']['authentication'],
                   'ca': t['REMOTE'] + '/ca.crt'}
        result[source]['roles'] = json.loads(nodes['run'](source, 'sudo -u postgres python3 -c ' + shlex.quote(probe), json.dumps(payload)).stdout)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', required=True, action='store_true')
    parser.parse_args()
    os.umask(0o077)
    run_id = uuid.uuid4().hex
    evidence = {'runId': run_id, 'startedAt': datetime.now(timezone.utc).isoformat(), 'phases': []}
    evidence['boundaries'] = boundaries()
    print('All certificate, hostname, role and plaintext boundaries passed.', flush=True)
    original = pg['leader']()
    expected = []
    raw = ROOT / ('ops/checks/sql-tls-' + run_id + '.raw.txt')
    def events():
        rows = []
        for line in raw.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('phase') == 'event':
                rows.append(row)
        return rows
    def phase(name):
        state = wait(replication)
        for _ in range(4):
            identifier = str(uuid.uuid4())
            expected.append(identifier)
            payload = json.dumps({'event_id': identifier, 'run_id': run_id, 'seq': len(expected), 'channel_id': 'sql-tls'})
            pg['sql'](pg['leader'](), "SET statement_timeout='15s'; INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES ('" + identifier + "','infra','sql-tls','TLSProbe','" + payload + "'::jsonb);", 'crawler')
        wait(lambda: set(expected).issubset({row['id'] for row in events()}))
        evidence['phases'].append({'phase': name, 'replication': state, 'grafana': grafana(),
                                   'acknowledged': len(expected), 'received': len({row['id'] for row in events()})})
        print(name + ': ' + str(len(expected)) + ' confirmed CDC events received, Grafana and TLS healthy.', flush=True)
    def restore_original():
        if pg['leader']() == original:
            return
        wait(replication)
        count = pg['api'](original, '/config')['synchronous_node_count']
        try:
            pg['api'](original, '/config', {'synchronous_node_count': 2}, 'PATCH')
            wait(lambda: any(m['name'] == original and m['role'] == 'sync_standby' for m in pg['members']()))
            pg['api'](pg['leader'](), '/switchover', {'leader': pg['leader'](), 'candidate': original})
            pg['wait_members']()
        finally:
            pg['api'](original, '/config', {'synchronous_node_count': count}, 'PATCH')
    with raw.open('w') as log:
        consumer = subprocess.Popen(['node', str(ROOT / 'ops/cdc/consume-probe.mjs'), run_id, '12', '600000'], stdout=log, stderr=log)
        try:
            phase('before-switch')
            candidate = next(m['name'] for m in pg['members']() if m['role'] == 'sync_standby')
            started = time.monotonic()
            pg['api'](original, '/switchover', {'leader': original, 'candidate': candidate})
            wait(lambda: pg['leader']() == candidate)
            phase('after-switch')
            evidence['switch'] = {'from': original, 'to': candidate, 'verificationSeconds': round(time.monotonic() - started, 2)}
            restore_original()
            phase('original-restored')
            consumer.wait(timeout=30)
            assert consumer.returncode == 0
        finally:
            if consumer.poll() is None:
                consumer.terminate()
                consumer.wait(timeout=15)
            restore_original()
    evidence.update(status='PASSED', acknowledged=len(expected), received=len({r['id'] for r in events()}),
                    duplicates=len(events()) - len(expected), finalPrimary=pg['leader'](),
                    completedAt=datetime.now(timezone.utc).isoformat())
    path = ROOT / 'ops/checks/2026-09-21-postgresql-sql-tls.json'
    path.write_text(json.dumps(evidence, indent=2) + '\n')
    print(str(path), flush=True)


if __name__ == '__main__':
    main()
