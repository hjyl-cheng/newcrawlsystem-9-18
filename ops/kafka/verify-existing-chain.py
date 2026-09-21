#!/usr/bin/env python3
"""Bounded Ingestor/CDC regression through operator mTLS and the deployed clients."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'ops/cdc'))
import common as c


def main():
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('Requires --execute; writes small synthetic validation records')
    sql = runpy.run_path(str(ROOT/'ops/postgresql-ha/verify-application-sql.py'))
    owner = runpy.run_path(str(ROOT/'ops/postgresql-ha/prepare-patroni.py'))['envfile'](ROOT/'secrets/validation-storage.env')
    env = dict(os.environ, PGSSLMODE='verify-full', NODE_EXTRA_CA_CERTS=str(ROOT/'secrets/postgresql-ha/sql-pki/ca.crt'),
        PGHOST=c.leader(), PGPORT='5432', PGUSER='crawler', PGPASSWORD=owner['CRAWLER_PASSWORD'],
        PGDATABASE='crawler_validation_ingestor', CONSUMER_PGHOST=c.leader(), CONSUMER_PGPORT='6432')
    result = {'startedAt': datetime.now(timezone.utc).isoformat(), 'transport': '9094 mTLS; verify deployed application config separately'}
    with sql['forward']('data-ingestor', 18081, 8080):
        result['ingestor'] = sql['node_check']('ops/ingestor/verify-validation.mjs', env)
    print('Existing Ingestor: valid, duplicate and invalid-signature submissions passed', flush=True)
    run_id = uuid.uuid4().hex
    private = ROOT/'secrets/kafka/migration'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = private/('cdc-'+run_id+'.txt')
    expected = []
    rows = []
    for seq in range(4):
        identifier = str(uuid.uuid4()); expected.append(identifier)
        channel = 'kafka-tls-validation-'+str(seq%2)
        body = {'event_id': identifier, 'run_id': run_id, 'seq': seq, 'channel_id': channel, 'phase': 'listener-staged'}
        rows.append("('"+identifier+"','infra','"+channel+"','Probe','"+json.dumps(body)+"'::jsonb)")
    with raw.open('w') as log:
        raw.chmod(0o600)
        consumer = subprocess.Popen(['node', str(ROOT/'ops/cdc/consume-probe.mjs'), run_id, '4', '90000'], stdout=log, stderr=log)
        try:
            c.sql(c.leader(), "SET statement_timeout='15s'; INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES "+','.join(rows)+';', 'crawler')
            consumer.wait(timeout=110)
            assert consumer.returncode == 0, 'CDC probe failed; inspect protected '+str(raw)
        finally:
            if consumer.poll() is None:
                consumer.terminate(); consumer.wait(timeout=10)
    records = []
    for line in raw.read_text().splitlines():
        try: records.append(json.loads(line))
        except json.JSONDecodeError: pass
    events = [r for r in records if r.get('phase')=='event']
    assert set(expected) == {r['id'] for r in events}
    result['cdc'] = {'runId': run_id, 'confirmedEventIds': expected, 'received': events,
        'duplicates': len(events)-len(expected), 'allConfirmedEventsReceived': True}
    print('Existing CDC: four confirmed events received', flush=True)
    result['cdcHealth'] = json.loads(subprocess.check_output(['python3', str(ROOT/'ops/cdc/check-health.py')], text=True, timeout=90))
    result['kafkaHealth'] = json.loads(subprocess.check_output(['node', str(ROOT/'ops/kafka/check-health.mjs')], text=True, timeout=60))
    result['status'] = 'PASSED'
    result['finishedAt'] = datetime.now(timezone.utc).isoformat()
    (ROOT/'ops/checks/2026-09-21-kafka-client-chain.json').write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':main()
