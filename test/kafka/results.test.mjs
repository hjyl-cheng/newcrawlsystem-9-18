import assert from 'node:assert/strict';
import {before,after,test} from 'node:test';
import {randomUUID,generateKeyPairSync} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import pg from 'pg';
import sdk from '@confluentinc/kafka-javascript';
import {databaseConfig,migrate} from '../../ops/db/migrate.mjs';
import {prepareMetricsPlan,contentHash,targetHash,querySubmissionReceipt,takeOverExecution} from '../../dist/services/ingestion/src/metrics-submission.js';
import {resultProducer,publishResult,encodeResult,processResultRecord,startResultConsumer} from '../../dist/services/kafka-results/src/transport.js';

const config=databaseConfig();assert.match(config.database,/^crawler_schema_test_[a-z0-9_]+$/);
assert.equal(process.env.DB_ALLOW_TEST_WRITES,config.database);
const brokers=process.env.KAFKA_BROKERS?.split(',').filter(Boolean);assert.ok(brokers?.length,'KAFKA_BROKERS required');
const {Kafka,logLevel}=sdk.KafkaJS;
const makeKafka=name=>new Kafka({kafkaJS:{brokers,clientId:name,logLevel:logLevel.ERROR}});
const pool=new pg.Pool({...config,max:6});
const admin=makeKafka('result-tests-admin').admin();
const producer=resultProducer(makeKafka('result-tests-producer'));
const channels=[],plans=[],batches=[],topics=[],groups=[],streams=[],runners=[];
const keyPair=generateKeyPairSync('ed25519');
const allowedChannels=new Set();
const trust=new Map([['test-worker-key',{publicKey:keyPair.publicKey,holderId:'kafka-test-worker',allowedChannels}]]);
async function until(fn,timeout=30000){const deadline=Date.now()+timeout;while(Date.now()<deadline){const result=await fn();if(result)return result;await delay(100);}throw new Error('Timed out waiting for Kafka/PG state');}
before(async()=>{
  const c=await pool.connect();try{await migrate(c,config.database);}finally{c.release();}
  await admin.connect();await producer.connect();
});
after(async()=>{
  for(const runner of runners)await runner.stop();
  await producer.disconnect();
  try{
    if(groups.length)try{await admin.deleteGroups(groups);}catch(error){
      if(!error.groups?.every(g=>g.errorCode===0 || g.errorCode===69))throw error;
    }
    if(topics.length)await admin.deleteTopics({topics});
    await pool.query('DELETE FROM ingestion.kafka_record_outcomes WHERE stream_id=ANY($1::text[])',[streams]);
    for(const table of ['ingestion.batch_checkpoints','ingestion.receipts','ingestion.submissions','control.domain_items','ingestion.logical_batches'])
      await pool.query(`DELETE FROM ${table} WHERE logical_batch_key=ANY($1::text[])`,[batches]);
    for(const table of ['control.domain_obligations','control.execution_authorizations'])await pool.query(`DELETE FROM ${table} WHERE plan_id=ANY($1::text[])`,[plans]);
    await pool.query('DELETE FROM control.channel_coordination WHERE channel_id=ANY($1::text[])',[channels]);
    await pool.query('DELETE FROM control.plans WHERE plan_id=ANY($1::text[])',[plans]);
    await pool.query('DELETE FROM crawler.contents WHERE channel_id=ANY($1::text[])',[channels]);
    await pool.query('DELETE FROM crawler.channels WHERE channel_id=ANY($1::text[])',[channels]);
  }finally{await admin.disconnect();await pool.end();}
});
async function scenario(){
  const suffix=randomUUID();const topic='crawler.results.test.'+suffix,groupId='result-test-'+suffix,streamId='result-test:'+suffix;
  topics.push(topic);groups.push(groupId);streams.push(streamId);
  await admin.createTopics({topics:[{topic,numPartitions:3,replicationFactor:3,configEntries:[
    {name:'min.insync.replicas',value:'2'},{name:'max.message.bytes',value:'1100000'},{name:'retention.ms',value:'3600000'},
  ]}]});
  // Metadata can advertise all replicas before a newly created leader answers ListOffsets.
  await until(async()=>{try{
    const [t]=await admin.fetchTopicMetadata({topics:[topic]});
    if(t?.partitions.length!==3||!t.partitions.every(p=>p.leader>=0&&p.isr.length===3))return false;
    const ranges=await admin.fetchTopicOffsets(topic);
    return ranges.length===3&&ranges.every(p=>BigInt(p.low)===0n&&BigInt(p.high)===0n);
  }catch{return false;}});
  return {topic,groupId,streamId};
}
async function fixture(){
  const prefix='kafka-test:'+randomUUID();
  const identity={plan_id:prefix+':plan',channel_id:prefix+':channel',logical_batch_key:prefix+':batch'};
  channels.push(identity.channel_id);plans.push(identity.plan_id);batches.push(identity.logical_batch_key);allowedChannels.add(identity.channel_id);
  await pool.query('INSERT INTO crawler.channels(channel_id,channel_url) VALUES ($1,$2)',[identity.channel_id,'https://example.test/kafka']);
  const targets=[{source_content_id:prefix+':video',content_type:'video'}];
  const frozen_input={generation:'1',input_revision:'10000000000000000000',input_hash:contentHash({}),target_hash:targetHash(targets),policy_version:'metrics-v1'};
  const definition={identity,intent_key:prefix+':intent',scope:prefix+':scope',holder_id:'kafka-test-worker',execution_epoch:'1',lease_seconds:300,frozen_input,targets};
  await prepareMetricsPlan(pool,definition);
  const payload={schema_id:'content.metrics.batch/1-draft.1',identity,frozen_input,producer_role:'incremental',mode:'delta',
    observations:[{channelId:identity.channel_id,sourceContentId:targets[0].source_content_id,contentType:'video',observedAt:'2026-09-20T01:00:00.000Z',comment:{value:'120',status:'exact',source:'kafka-test'}}]};
  const request={submission_id:prefix+':submission',execution:{scope:definition.scope,execution_epoch:'1'},content_sha256:contentHash(payload),payload};
  return {definition,request,signer:{keyId:'test-worker-key',privateKey:keyPair.privateKey,principal:{holderId:'kafka-test-worker',channelId:identity.channel_id}}};
}
async function start(s,extra={}){
  const runner=await startResultConsumer({kafka:makeKafka('consumer-'+randomUUID()),...s,
    handleRecord:r=>processResultRecord(pool,r,trust),retryBaseMs:50,...extra});
  let stopped=false;const original=runner.stop;runner.stop=async()=>{if(!stopped){stopped=true;await original();}};
  runners.push(runner);return runner;
}
async function outcome(s,offset,partition){return(await pool.query(`SELECT * FROM ingestion.kafka_record_outcomes
 WHERE stream_id=$1 AND topic=$2 AND partition_id=$3 AND record_offset=$4`,[s.streamId,s.topic,partition,offset])).rows[0];}
async function committed(s,partition){const all=await admin.fetchOffsets({groupId:s.groupId,topics:[s.topic]});return all.find(t=>t.topic===s.topic)?.partitions.find(p=>p.partition===partition)?.offset ?? '-1';}
async function receipt(f){return querySubmissionReceipt(pool,f.request.submission_id,f.request.payload.identity,f.request.content_sha256,f.request.payload.identity.channel_id);}
async function sendAt(s,f,partition=0){const message=encodeResult(f.request,f.signer);const [metadata]=await producer.send({topic:s.topic,messages:[{...message,partition}]});return metadata.offset??metadata.baseOffset;}

test('broker acknowledgment is distinct from PG APPLIED; committed offset follows durable outcome', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture();
  const ack=await publishResult(producer,s.topic,f.request,f.signer);assert.equal(ack.status,'BROKER_ACKED');
  assert.equal((await receipt(f)).status,'NOT_OBSERVED');
  const runner=await start(s);const position=ack.positions[0];
  const row=await until(()=>outcome(s,position.offset,position.partition));
  assert.equal(row.disposition,'APPLIED');assert.equal((await receipt(f)).status,'APPLIED');
  await until(async()=>BigInt(await committed(s,position.partition))===BigInt(position.offset)+1n);
  assert.equal(row.receipt_id,(await receipt(f)).receipt_id);await runner.stop();
});

test('same submission in two Kafka records reuses one PG receipt', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture();const first=await sendAt(s,f),second=await sendAt(s,f);
  const runner=await start(s);await until(()=>outcome(s,second,0));
  const a=await outcome(s,first,0),b=await outcome(s,second,0);assert.equal(a.receipt_id,b.receipt_id);
  assert.equal((await pool.query('SELECT count(*)::int AS n FROM ingestion.receipts WHERE submission_id=$1',[f.request.submission_id])).rows[0].n,1);
  await runner.stop();
});

test('database failure leaves current and later offsets pending; retry resumes in partition order', {timeout:45000},async()=>{
  const s=await scenario(),a=await fixture(),b=await fixture();const first=await sendAt(s,a),second=await sendAt(s,b);
  let fail=true,retries=0;const calls=[];
  const runner=await start(s,{onEvent:e=>{if(e.kind==='retry')retries++;},handleRecord:async record=>{
    calls.push(record.offset);if(fail)throw Object.assign(new Error('simulated database unavailable'),{code:'08006'});
    return processResultRecord(pool,record,trust);
  }});
  await until(()=>retries>0);assert.equal(await outcome(s,first,0),undefined);
  assert.ok(BigInt(await committed(s,0))<BigInt(first)+1n);assert.ok(!calls.includes(second));
  fail=false;await until(()=>outcome(s,second,0));
  await until(async()=>BigInt(await committed(s,0))===BigInt(second)+1n);await runner.stop();
});

test('PG commit followed by missing Kafka offset commit is safely replayed after consumer restart', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture(),offset=await sendAt(s,f);let retried=false;
  const runner=await start(s,{onEvent:e=>{if(e.kind==='retry')retried=true;},handleRecord:async record=>{
    await processResultRecord(pool,record,trust);throw Object.assign(new Error('simulated crash before offset commit'),{code:'TEST.CRASH_WINDOW'});
  }});
  await until(()=>retried);const before=await receipt(f);assert.equal(before.status,'APPLIED');
  assert.ok(BigInt(await committed(s,0))<BigInt(offset)+1n);await runner.stop();
  const replacement=await start(s);await until(async()=>BigInt(await committed(s,0))===BigInt(offset)+1n);
  assert.deepEqual(await receipt(f),before);await replacement.stop();
});

test('bad signature is durably quarantined and does not block a later valid message', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture();const original=encodeResult(f.request,f.signer);
  const parsed=JSON.parse(original.value);parsed.signature='A'.repeat(86)+'==';
  const {canonicalJson}=await import('../../dist/services/ingestion/src/metrics-submission.js');
  const raw=Buffer.from(canonicalJson(parsed));const [bad]=await producer.send({topic:s.topic,messages:[{key:original.key,value:raw,partition:0}]});
  const good=await sendAt(s,f);const runner=await start(s);await until(()=>outcome(s,good,0));
  const parked=await outcome(s,bad.offset??bad.baseOffset,0);assert.equal(parked.disposition,'QUARANTINED');
  assert.equal(parked.error_code,'KAFKA.SIGNATURE_INVALID');assert.deepEqual(parked.raw_value,raw);assert.equal(parked.receipt_id,null);
  assert.equal((await receipt(f)).status,'APPLIED');await runner.stop();
});

test('queued old epoch is quarantined after takeover, without creating a false APPLIED', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture();const offset=await sendAt(s,f);
  await takeOverExecution(pool,f.definition.scope,'1','new-worker',300);
  const runner=await start(s);const parked=await until(()=>outcome(s,offset,0));
  assert.equal(parked.error_code,'AUTHZ.EXECUTION_STALE');assert.equal(parked.disposition,'QUARANTINED');
  assert.equal((await receipt(f)).status,'NOT_OBSERVED');
  assert.equal((await pool.query('SELECT count(*)::int AS n FROM crawler.contents WHERE channel_id=$1',[f.definition.identity.channel_id])).rows[0].n,0);
  await runner.stop();
});

test('two consumers in one group process different partitions concurrently', {timeout:45000},async()=>{
  const s=await scenario();let release;const gate=new Promise(resolve=>{release=resolve;});const entered=new Set();
  const handler=member=>async record=>{entered.add(member);await gate;return processResultRecord(pool,record,trust);};
  const one=await start(s,{handleRecord:handler('one')}),two=await start(s,{handleRecord:handler('two')});
  try{
    await until(()=>one.consumer.assignment().length>0 && two.consumer.assignment().length>0);
    const p1=one.consumer.assignment()[0].partition,p2=two.consumer.assignment()[0].partition;assert.notEqual(p1,p2);
    const a=await fixture(),b=await fixture();const oa=await sendAt(s,a,p1),ob=await sendAt(s,b,p2);
    await until(()=>entered.size===2);release();
    await until(async()=>await outcome(s,oa,p1)&&await outcome(s,ob,p2));
  }finally{release();await one.stop();await two.stop();}
});

test('record-outcome SQL failure rolls back facts and receipt in the same transaction', {timeout:45000},async()=>{
  const s=await scenario(),f=await fixture();const message=encodeResult(f.request,f.signer);
  const record={...message,...s,partition:0,offset:'0'};
  const failing={async connect(){const c=await pool.connect();return {release:e=>c.release(e),query:(sql,values)=>
    sql.includes('INSERT INTO ingestion.kafka_record_outcomes')?c.query('SELECT 1/0'):c.query(sql,values)};}};
  await assert.rejects(processResultRecord(failing,record,trust),e=>e.code==='22012');
  assert.equal((await receipt(f)).status,'NOT_OBSERVED');assert.equal(await outcome(s,'0',0),undefined);
  assert.equal((await processResultRecord(pool,record,trust)).disposition,'APPLIED');
  const altered={...record,value:Buffer.from(record.value.toString()+' ')};
  await assert.rejects(processResultRecord(pool,altered,trust),e=>e.code==='KAFKA.STREAM_IDENTITY_CONFLICT');
});

test('retention gap behind an existing consumer offset blocks restart instead of silently skipping data', {timeout:45000},async()=>{
  const s=await scenario(),a=await fixture(),b=await fixture();
  const first=await sendAt(s,a);const runner=await start(s);
  await until(async()=>BigInt(await committed(s,0))===BigInt(first)+1n);await runner.stop();
  const second=await sendAt(s,b);
  await admin.deleteTopicRecords({topic:s.topic,partitions:[{partition:0,offset:(BigInt(second)+1n).toString()}]});
  await assert.rejects(start(s),e=>e.code==='KAFKA.RETENTION_OR_STREAM_GAP');
  assert.equal((await receipt(b)).status,'NOT_OBSERVED');
});
