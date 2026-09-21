import {operatorTls,secureBrokers} from '../kafka/client.mjs';
/** Real service acceptance. Writes bounded synthetic records only to the dedicated validation DB/topic. */
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createPrivateKey,randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import pg from 'pg';
import sdk from '@confluentinc/kafka-javascript';
import {prepareMetricsPlan,contentHash,targetHash,canonicalJson} from '../../dist/services/ingestion/src/metrics-submission.js';
import {resultProducer,encodeResult,publishResult} from '../../dist/services/kafka-results/src/transport.js';
const dir=new URL('../../secrets/data-ingestor/',import.meta.url);
const runtime=Object.fromEntries((await readFile(new URL('runtime.env',dir),'utf8')).trim().split('\n').map(l=>l.split('=')));
const operator=JSON.parse(await readFile(new URL('operator.json',dir),'utf8'));
const privateKey=createPrivateKey(await readFile(new URL('worker-private.pem',dir)));
const url=process.env.INGESTOR_URL??'http://127.0.0.1:18081';
assert.equal(process.env.PGDATABASE,'crawler_validation_ingestor');
assert.equal(process.env.PGUSER,'crawler');
const owner=new pg.Pool({max:2});
const consumer=new pg.Pool({host:process.env.CONSUMER_PGHOST??runtime.PGHOST,port:Number(process.env.CONSUMER_PGPORT??runtime.PGPORT),database:runtime.PGDATABASE,user:runtime.PGUSER,password:runtime.PGPASSWORD,max:2});
const kafka=new sdk.KafkaJS.Kafka({...operatorTls(),kafkaJS:{brokers:secureBrokers,clientId:'ingestor-acceptance',logLevel:sdk.KafkaJS.logLevel.ERROR}});
const producer=resultProducer(kafka),admin=kafka.admin();
const topic='crawler.results.validation.v1',groupId='crawler-results-apply-validation-v1';
const fixtures=[],positions=[];
async function query(path,input,token=operator.token){const r=await fetch(url+path,{method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json'},body:JSON.stringify(input),signal:AbortSignal.timeout(8000)});return {status:r.status,body:await r.json()};}
async function until(fn){const until=Date.now()+60000;while(Date.now()<until){const value=await fn();if(value)return value;await delay(250);}throw new Error('Acceptance timeout');}
try{
  assert.equal((await fetch(url+'/health/ready')).status,200);
  // Runtime can lock but cannot mutate authority, delete receipts, create schema or access other DBs.
  for(const sql of ['CREATE TABLE public.forbidden_probe(id integer)','DELETE FROM ingestion.receipts WHERE false',
    'UPDATE control.plans SET plan_id=plan_id WHERE false','UPDATE control.execution_authorizations SET scope=scope WHERE false',
    'UPDATE control.channel_coordination SET channel_id=channel_id WHERE false','UPDATE ingestion.logical_batches SET logical_batch_key=logical_batch_key WHERE false',
    "INSERT INTO control.plans(plan_id) VALUES ('forbidden')",'UPDATE crawler.contents SET title=title WHERE false']){
    await assert.rejects(consumer.query(sql),e=>e.code==='42501');
  }
  for(const host of ['10.4.4.2','10.4.4.8','10.4.4.5']){
    const other=new pg.Client({host,port:5432,database:'crawler',user:runtime.PGUSER,password:runtime.PGPASSWORD,connectionTimeoutMillis:5000});
    try{await assert.rejects(other.connect(),e=>e.code==='28000');}finally{await other.end();}
  }
  await producer.connect();await admin.connect();
  for(const [index,channel] of operator.channels.entries()){
    const prefix='service-validation:'+randomUUID();
    await owner.query('INSERT INTO crawler.channels(channel_id,channel_url) VALUES($1,$2) ON CONFLICT DO NOTHING',[channel,'https://example.test/ingestor-validation']);
    // Controlled harness, never exposed to the consumer. Previous synthetic plans remain auditable.
    await owner.query("UPDATE control.plans SET lifecycle_state='cancelled' WHERE channel_id=$1 AND lifecycle_state='active'",[channel]);
    await owner.query('UPDATE control.channel_coordination SET active_plan_id=NULL WHERE channel_id=$1',[channel]);
    const identity={plan_id:prefix+':plan',channel_id:channel,logical_batch_key:prefix+':batch'};
    const targets=[{source_content_id:prefix+':video',content_type:'video'}];
    const frozen_input={generation:'1',input_revision:'1',input_hash:contentHash({}),target_hash:targetHash(targets),policy_version:'metrics-v1'};
    const definition={identity,intent_key:prefix+':intent',scope:prefix+':scope',holder_id:operator.holderId,execution_epoch:'1',lease_seconds:3600,frozen_input,targets};
    await prepareMetricsPlan(owner,definition);
    const payload={schema_id:'content.metrics.batch/1-draft.1',identity,frozen_input,producer_role:'incremental',mode:'delta',observations:[{channelId:channel,sourceContentId:targets[0].source_content_id,contentType:'video',observedAt:new Date().toISOString(),comment:{value:String(120+index),status:'exact',source:'service-validation'}}]};
    const request={submission_id:prefix+':submission',execution:{scope:definition.scope,execution_epoch:'1'},content_sha256:contentHash(payload),payload};
    const signer={keyId:operator.keyId,privateKey,principal:{holderId:operator.holderId,channelId:channel}};
    const lookup={submission_id:request.submission_id,identity,content_sha256:request.content_sha256};
    assert.equal((await query('/v1/receipts/lookup',lookup,'x'.repeat(43))).status,401);
    assert.equal((await query('/v1/receipts/lookup',{...lookup,identity:{...identity,channel_id:'unauthorized-channel'}})).status,403);
    assert.equal((await query('/v1/receipts/lookup',lookup)).body.status,'NOT_OBSERVED');
    const ack=await publishResult(producer,topic,request,signer);assert.equal(ack.status,'BROKER_ACKED');positions.push(...ack.positions);
    const receipt=await until(async()=>{const r=await query('/v1/receipts/lookup',lookup);return r.status===200&&r.body.status==='APPLIED'?r.body:null;});
    const duplicate=await publishResult(producer,topic,request,signer);positions.push(...duplicate.positions);
    const signed=encodeResult(request,signer),bad=JSON.parse(signed.value);bad.signature='A'.repeat(86)+'==';
    const [badAck]=await producer.send({topic,messages:[{key:signed.key,value:Buffer.from(canonicalJson(bad))}]});
    const badPosition={topic,partition:badAck.partition,offset:badAck.offset??badAck.baseOffset};positions.push(badPosition);
    const parked=await until(async()=>{const r=await query('/v1/records/lookup',{partition:badPosition.partition,offset:String(badPosition.offset)});return r.body.disposition==='QUARANTINED'?r.body:null;});
    assert.equal(parked.error_code,'KAFKA.SIGNATURE_INVALID');assert.ok(parked.value_size>0);assert.equal(parked.receipt_id,null);assert.equal(parked.raw_value,undefined);
    assert.deepEqual((await query('/v1/receipts/lookup',lookup)).body,receipt);
    fixtures.push({lookup,receipt,commentCount:String(120+index),sourceContentId:targets[0].source_content_id});
  }
  await until(async()=>{const [state]=await admin.fetchOffsets({groupId,topics:[topic]});return positions.every(p=>BigInt(state.partitions.find(x=>x.partition===p.partition)?.offset??'-1')>BigInt(p.offset));});
  for(const f of fixtures){const row=(await owner.query('SELECT comment_count::text AS n FROM crawler.contents WHERE source_content_id=$1',[f.sourceContentId])).rows[0];assert.equal(row.n,f.commentCount);}
  const metrics=await fetch(url+'/metrics',{headers:{authorization:'Bearer '+operator.token}});assert.equal(metrics.status,200);
  const summary={checkedAt:new Date().toISOString(),database:runtime.PGDATABASE,topic,fixtures,positions,checks:['permissions','receipt-auth','3-applied','duplicate-idempotency','3-quarantined','offsets-after-durable','metrics']};
  await writeFile(new URL('last-acceptance.json',dir),JSON.stringify(summary,null,2),{mode:0o600});
  console.log(JSON.stringify({checks:summary.checks,positions,sampleCount:fixtures.length}));
}finally{await Promise.allSettled([producer.disconnect(),admin.disconnect(),owner.end(),consumer.end()]);}
