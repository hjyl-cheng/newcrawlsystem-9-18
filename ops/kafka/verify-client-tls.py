#!/usr/bin/env python3
"""Inspect real clients and exercise mTLS/legacy-egress rejection from both Ingestors."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
PROBE=r'''
import sdk from '@confluentinc/kafka-javascript';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import tls from 'node:tls';import net from 'node:net';
const dir=process.env.KAFKA_TLS_DIR;
const common={ca:readFileSync(dir+'/ca.crt'),cert:readFileSync(dir+'/client.crt'),key:readFileSync(dir+'/client.key'),rejectUnauthorized:true};
const brokers=process.env.KAFKA_BROKERS.split(',');assert.equal(brokers.length,3);
const kafka=new sdk.KafkaJS.Kafka({'security.protocol':'ssl','ssl.ca.location':dir+'/ca.crt','ssl.certificate.location':dir+'/client.crt',
 'ssl.key.location':dir+'/client.key','enable.ssl.certificate.verification':true,'ssl.endpoint.identification.algorithm':'https',
 kafkaJS:{brokers,clientId:'pod-tls-verifier',logLevel:sdk.KafkaJS.logLevel.NOTHING}});
const admin=kafka.admin();try{await admin.connect();const [t]=await admin.fetchTopicMetadata({topics:['crawler.results.validation.v1']});assert.equal(t.partitions.length,3);}finally{await admin.disconnect();}
function exchange(host,extra={}){return new Promise((resolve,reject)=>{
 const s=tls.connect({host,port:9094,...common,...extra});let body=Buffer.alloc(0);
 s.setTimeout(3000,()=>s.destroy(Error('PROBE_TIMEOUT')));s.on('error',reject);
 s.on('secureConnect',()=>{const req=Buffer.alloc(14);req.writeInt32BE(10,0);req.writeInt16BE(18,4);req.writeInt16BE(0,6);req.writeInt32BE(420,8);req.writeInt16BE(-1,12);s.write(req);});
 s.on('data',data=>{body=Buffer.concat([body,data]);if(body.length<4)return;const n=body.readInt32BE(0);if(n<10||n>100000){s.destroy(Error('INVALID_RESPONSE'));return;}if(body.length<n+4)return;
  try{assert.equal(body.readInt32BE(4),420);assert.equal(body.readInt16BE(8),0);assert.equal(s.authorized,true);resolve(s.getProtocol());s.end();}catch(e){s.destroy(e);}});
});}
function legacyBlocked(host){return new Promise((resolve,reject)=>{const s=net.createConnection({host,port:9092});s.setTimeout(2000,()=>{s.destroy();resolve('timeout');});s.on('connect',()=>{s.destroy();reject(Error('PLAINTEXT_EGRESS_ALLOWED'));});s.on('error',e=>{if(e.code==='ECONNREFUSED')resolve('refused');else reject(e);});});}
const out={nativeMetadata:'passed',nodes:{}};
for(const broker of brokers){const [host,port]=broker.split(':');assert.equal(port,'9094');const result={tls:await exchange(host),legacyEgress:await legacyBlocked(host)};
 for(const [name,extra,codes] of [
  ['wrongCA',{ca:readFileSync('/etc/postgresql-sql-tls/ca.crt')},['UNABLE_TO_VERIFY_LEAF_SIGNATURE','UNABLE_TO_GET_ISSUER_CERT_LOCALLY','SELF_SIGNED_CERT_IN_CHAIN']],
  ['wrongName',{servername:'wrong-kafka.invalid'},['ERR_TLS_CERT_ALTNAME_INVALID']],
  ['noClientCertificate',{cert:undefined,key:undefined,minVersion:'TLSv1.2',maxVersion:'TLSv1.2'},['ERR_SSL_SSLV3_ALERT_HANDSHAKE_FAILURE','ERR_SSL_SSL/TLS_ALERT_HANDSHAKE_FAILURE','ERR_SSL_SSLV3_ALERT_BAD_CERTIFICATE']]]){
   let error;try{await exchange(host,extra);}catch(e){error=e;}assert.ok(error&&codes.includes(error.code),name+': expected certificate rejection, got '+error?.code);result[name]=error.code;
 }
 out.nodes[host]=result;
}
console.log(JSON.stringify(out));
'''


def kube(ns,*args):return subprocess.check_output(['kubectl','-n',ns,*args],text=True,timeout=60)


def main():
    result={'checkedAt':datetime.now(timezone.utc).isoformat(),'workloads':{},'ingestorBoundaries':{}}
    for ns,app,secret,identity in [('crawl-validation','data-ingestor','kafka-ingestor-tls','ingestor'),
        ('crawl-validation','kafka-connect','kafka-connect-tls','connect'),('crawl-monitoring','kafka-exporter','kafka-monitoring-tls','monitoring')]:
        pods=json.loads(kube(ns,'get','pods','-l','app='+app,'-o','json'))['items'];assert len(pods)==2
        result['workloads'][app]=[]
        for pod in pods:
            assert not pod['metadata'].get('deletionTimestamp') and pod['status']['containerStatuses'][0]['ready']
            assert any(v.get('secret',{}).get('secretName')==secret for v in pod['spec']['volumes'])
            name=pod['metadata']['name']
            assert kube(ns,'exec',name,'--','cat','/etc/kafka-tls/client.crt')==(ROOT/'secrets/kafka/pki'/(identity+'.crt')).read_text()
            raw=kube(ns,'exec',name,'--','cat','/proc/net/tcp','/proc/net/tcp6')
            sockets={}
            for line in raw.splitlines():
                fields=line.split()
                if len(fields)>3 and fields[3]=='01':
                    port=int(fields[2].rsplit(':',1)[1],16)
                    if port in [9092,9094]:sockets[str(port)]=sockets.get(str(port),0)+1
            assert sockets.get('9094',0)>0 and not sockets.get('9092',0)
            result['workloads'][app].append({'pod':name,'sockets':sockets,'identity':'CN=crawl-kafka-'+identity,
                'image':pod['spec']['containers'][0]['image'],'restarts':pod['status']['containerStatuses'][0]['restartCount']})
            if app=='data-ingestor':
                result['ingestorBoundaries'][name]=json.loads(kube(ns,'exec',name,'--','node','--input-type=module','-e',PROBE))
            if app=='kafka-connect':
                props=dict(line.split('=',1) for line in kube(ns,'exec',name,'--','cat','/etc/connect/worker.properties').splitlines() if '=' in line and not line.startswith('#'))
                for prefix in ['', 'producer.', 'consumer.', 'admin.']:
                    assert props[prefix+'security.protocol']=='SSL' and props[prefix+'ssl.endpoint.identification.algorithm']=='https'
            if app=='kafka-exporter':
                args=pod['spec']['containers'][0]['args'];assert '--tls.enabled' in args
                assert not any('insecure-skip' in a for a in args)
        print(app+': both real pods connected only to 9094',flush=True)
    result['status']='PASSED'
    result['scope']='Application mTLS and Ingestor old-port isolation; broker/controller internal encryption and ACL not yet enabled'
    (ROOT/'ops/checks/2026-09-21-kafka-client-tls.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':main()
