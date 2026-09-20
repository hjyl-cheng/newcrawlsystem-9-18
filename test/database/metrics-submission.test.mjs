import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { before, after, test } from 'node:test';
import { readFileSync } from 'node:fs';
import pg from 'pg';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { databaseConfig, migrate } from '../../ops/db/migrate.mjs';
import { prepareMetricsPlan, takeOverExecution, applyMetricsSubmission, querySubmissionReceipt,
  contentHash, canonicalJson, targetHash } from '../../dist/services/ingestion/src/metrics-submission.js';

const config = databaseConfig();
assert.match(config.database, /^crawler_schema_test_[a-z0-9_]+$/);
assert.equal(process.env.DB_ALLOW_TEST_WRITES, config.database);
const pool = new pg.Pool({...config,max:6});
const channels = [], plans = [], batches = [];
const ajv = new Ajv2020({strict:true}); addFormats(ajv);
const validateWire = ajv.compile(JSON.parse(readFileSync('contracts/data-plane/v1-draft.schema.json')));
const validatePayload = ajv.compile(JSON.parse(readFileSync('contracts/data-plane/content-metrics-batch.v1-draft.schema.json')));
before(async () => { const c = await pool.connect(); try { await migrate(c,config.database); } finally { c.release(); } });
after(async () => {
  try {
    for (const table of ['ingestion.batch_checkpoints','ingestion.receipts','ingestion.submissions','control.domain_items','ingestion.logical_batches'])
      await pool.query(`DELETE FROM ${table} WHERE logical_batch_key=ANY($1::text[])`,[batches]);
    for (const table of ['control.domain_obligations','control.execution_authorizations'])
      await pool.query(`DELETE FROM ${table} WHERE plan_id=ANY($1::text[])`,[plans]);
    await pool.query('DELETE FROM control.channel_coordination WHERE channel_id=ANY($1::text[])',[channels]);
    await pool.query('DELETE FROM control.plans WHERE plan_id=ANY($1::text[])',[plans]);
    await pool.query('DELETE FROM crawler.contents WHERE channel_id=ANY($1::text[])',[channels]);
    await pool.query('DELETE FROM crawler.channels WHERE channel_id=ANY($1::text[])',[channels]);
  } finally { await pool.end(); }
});
function matches(code) { return e => e.code === code; }
async function setup({count=1,epoch='9007199254740993'}={}) {
  const prefix='submission-test:'+randomUUID();
  const identity={plan_id:prefix+':plan',channel_id:prefix+':channel',logical_batch_key:prefix+':batch'};
  channels.push(identity.channel_id);plans.push(identity.plan_id);batches.push(identity.logical_batch_key);
  await pool.query('INSERT INTO crawler.channels(channel_id,channel_url) VALUES ($1,$2)',[identity.channel_id,'https://example.test/submission']);
  const targets=Array.from({length:count},(_,i)=>({source_content_id:prefix+':video:'+i,content_type:'video'}));
  const frozen_input={generation:'99999999999999999999',input_revision:'10000000000000000000',input_hash:contentHash({}),target_hash:targetHash(targets),policy_version:'metrics-v1'};
  const definition={identity,intent_key:prefix+':intent',scope:prefix+':scope',holder_id:prefix+':worker',execution_epoch:epoch,lease_seconds:300,frozen_input,targets};
  await prepareMetricsPlan(pool,definition);
  const payload={schema_id:'content.metrics.batch/1-draft.1',identity,frozen_input,producer_role:'incremental',mode:'delta',
    observations:targets.map(t=>({channelId:identity.channel_id,sourceContentId:t.source_content_id,contentType:t.content_type,
      observedAt:'2026-09-20T01:00:00.000Z',comment:{value:'120',status:'exact',source:'test-comments'}}))};
  const request={submission_id:prefix+':submission',execution:{scope:definition.scope,execution_epoch:epoch},content_sha256:contentHash(payload),payload};
  assert.equal(validatePayload(payload),true,JSON.stringify(validatePayload.errors));
  return {definition,request,principal:{channelId:identity.channel_id,holderId:definition.holder_id}};
}
function rehash(request) { request.content_sha256=contentHash(request.payload); return request; }
async function lookup(f, request=f.request) {
  return querySubmissionReceipt(pool,request.submission_id,request.payload.identity,request.content_sha256,f.principal.channelId);
}
async function rows(f) {
  const i=f.request.payload.identity;
  const facts=(await pool.query('SELECT * FROM crawler.contents WHERE channel_id=$1',[i.channel_id])).rows;
  const receipt=(await pool.query('SELECT * FROM ingestion.receipts WHERE logical_batch_key=$1',[i.logical_batch_key])).rows;
  const submission=(await pool.query('SELECT * FROM ingestion.submissions WHERE logical_batch_key=$1',[i.logical_batch_key])).rows;
  const checkpoint=(await pool.query('SELECT * FROM ingestion.batch_checkpoints WHERE logical_batch_key=$1',[i.logical_batch_key])).rows;
  return {facts,receipt,submission,checkpoint};
}
function intercept(hook) {
  return {async connect(){const c=await pool.connect();return {query:(sql,values)=>hook(c,sql,values),release:error=>c.release(error)};}};
}

test('restricted JCS codec matches existing canonical golden bytes and rejects lossy values',()=>{
  const bytes=readFileSync('contracts/data-plane/fixtures/submission-content.jcs','utf8');
  assert.equal(canonicalJson(JSON.parse(bytes)),bytes);
  assert.equal(contentHash(JSON.parse(bytes)),JSON.parse(readFileSync('contracts/data-plane/fixtures/submission-stream.json')).payload.content_sha256);
  assert.equal(canonicalJson({b:1,a:'中文😀'}),'\u007b"a":"中文😀","b":1\u007d');
  for(const value of [undefined,1.1,Number.MAX_SAFE_INTEGER+1,'\ud800',{x:undefined},[undefined]]) assert.throws(()=>canonicalJson(value));
});

test('facts, checkpoint, immutable payload and APPLIED receipt commit together; repeat returns same receipt',async()=>{
  const f=await setup({count:2});
  const receipt=await applyMetricsSubmission(pool,f.request,f.principal);
  assert.equal(validateWire(receipt),true,JSON.stringify(validateWire.errors));
  assert.deepEqual(await applyMetricsSubmission(pool,f.request,f.principal),receipt);
  assert.deepEqual(await lookup(f),receipt);
  const state=await rows(f);
  assert.equal(state.facts.length,2); assert.equal(state.receipt.length,1); assert.equal(state.submission.length,1);
  assert.equal(state.checkpoint[0].item_count,2);
  assert.equal(state.checkpoint[0].receipt_id,receipt.receipt_id);
  assert.equal(contentHash(JSON.parse(state.submission[0].canonical_payload.toString('utf8'))),f.request.content_sha256);
  assert.equal(state.submission[0].original_execution_epoch,'9007199254740993');
  assert.equal((await pool.query('SELECT lifecycle_state FROM control.plans WHERE plan_id=$1',[f.definition.identity.plan_id])).rows[0].lifecycle_state,'active');
});

test('same submission ID with different content is rejected without changing original result',async()=>{
  const f=await setup(); const receipt=await applyMetricsSubmission(pool,f.request,f.principal);
  const changed=structuredClone(f.request);changed.payload.observations[0].comment.value='121';rehash(changed);
  await assert.rejects(applyMetricsSubmission(pool,changed,f.principal),matches('IDEMPOTENCY.PAYLOAD_CONFLICT'));
  assert.deepEqual(await lookup(f),receipt);assert.equal((await rows(f)).facts[0].comment_count,'120');
});

test('two submissions competing for one batch yield one receipt, never borrowed success',async()=>{
  const f=await setup();const other={...f.request,submission_id:f.request.submission_id+':other'};
  const results=await Promise.allSettled([applyMetricsSubmission(pool,f.request,f.principal),applyMetricsSubmission(pool,other,f.principal)]);
  assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal(results.find(r=>r.status==='rejected').reason.code,'IDEMPOTENCY.BATCH_ALREADY_APPLIED');
  const state=await rows(f);assert.equal(state.submission.length,1);assert.equal(state.receipt.length,1);assert.equal(state.checkpoint.length,1);
});

test('concurrent retry of same submission receives one identical durable receipt',async()=>{
  const f=await setup();const result=await Promise.all([applyMetricsSubmission(pool,f.request,f.principal),applyMetricsSubmission(pool,f.request,f.principal)]);
  assert.deepEqual(result[0],result[1]);assert.equal((await rows(f)).receipt.length,1);
});

test('same submission ID racing across channels cannot acquire two different meanings',async()=>{
  const first=await setup(),second=await setup();second.request.submission_id=first.request.submission_id;
  const results=await Promise.allSettled([applyMetricsSubmission(pool,first.request,first.principal),applyMetricsSubmission(pool,second.request,second.principal)]);
  assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal(results.find(r=>r.status==='rejected').reason.code,'IDEMPOTENCY.PAYLOAD_CONFLICT');
  assert.equal((await rows(first)).facts.length+(await rows(second)).facts.length,1);
});

test('takeover fences old epoch; completed submission remains readable after takeover and cancellation',async()=>{
  const f=await setup();
  const epoch=await takeOverExecution(pool,f.definition.scope,f.definition.execution_epoch,'replacement-worker',300);
  assert.equal(epoch,'9007199254740994');
  await assert.rejects(applyMetricsSubmission(pool,f.request,f.principal),matches('AUTHZ.EXECUTION_STALE'));
  const request={...f.request,execution:{...f.request.execution,execution_epoch:epoch}};
  const receipt=await applyMetricsSubmission(pool,request,{...f.principal,holderId:'replacement-worker'});
  await pool.query("UPDATE control.plans SET lifecycle_state='cancelled' WHERE plan_id=$1",[f.definition.identity.plan_id]);
  assert.deepEqual(await applyMetricsSubmission(pool,f.request,f.principal),receipt);
  assert.deepEqual(await lookup(f),receipt);
  await assert.rejects(takeOverExecution(pool,f.definition.scope,epoch,'third-worker',300),matches('AUTHZ.EXECUTION_STALE'));
});

test('wrong holder, expired/revoked lease and cancelled plan cannot produce facts',async()=>{
  const f=await setup();
  await assert.rejects(applyMetricsSubmission(pool,f.request,{...f.principal,holderId:'wrong'}),matches('AUTHZ.EXECUTION_STALE'));
  await pool.query("UPDATE control.execution_authorizations SET expires_at=clock_timestamp()-interval '1 second' WHERE scope=$1",[f.definition.scope]);
  await assert.rejects(applyMetricsSubmission(pool,f.request,f.principal),matches('AUTHZ.EXECUTION_STALE'));
  await pool.query("UPDATE control.execution_authorizations SET expires_at=clock_timestamp()+interval '1 hour',state='revoked' WHERE scope=$1",[f.definition.scope]);
  await assert.rejects(applyMetricsSubmission(pool,f.request,f.principal),matches('AUTHZ.EXECUTION_STALE'));
  await pool.query("UPDATE control.execution_authorizations SET state='active' WHERE scope=$1",[f.definition.scope]);
  await pool.query("UPDATE control.plans SET lifecycle_state='cancelled' WHERE plan_id=$1",[f.definition.identity.plan_id]);
  await assert.rejects(applyMetricsSubmission(pool,f.request,f.principal),matches('AUTHZ.EXECUTION_STALE'));
  assert.equal((await rows(f)).facts.length,0);
});

test('wrong plan/channel/frozen input/target and changed payload bytes fail closed',async()=>{
  const f=await setup();
  for(const [change,code] of [
    [r=>r.payload.identity.plan_id+=':wrong','AUTHZ.EXECUTION_STALE'],
    [r=>r.payload.identity.channel_id+=':wrong','AUTHZ.CHANNEL_DENIED'],
    [r=>r.payload.frozen_input.input_revision='42','DATA.VERSION_CONFLICT'],
    [r=>r.payload.frozen_input.generation='2','DATA.VERSION_CONFLICT'],
    [r=>r.payload.frozen_input.policy_version='wrong-policy','DATA.VERSION_CONFLICT'],
    [r=>{r.payload.observations[0].sourceContentId+=':wrong';r.payload.frozen_input.target_hash=targetHash(r.payload.observations.map(o=>({source_content_id:o.sourceContentId,content_type:o.contentType})));},'DATA.VERSION_CONFLICT'],
  ]) {
    const r=structuredClone(f.request);change(r);rehash(r);
    await assert.rejects(applyMetricsSubmission(pool,r,f.principal),matches(code));
  }
  const tampered=structuredClone(f.request);tampered.payload.observations[0].comment.value='1';
  await assert.rejects(applyMetricsSubmission(pool,tampered,f.principal),matches('OBJECT.HASH_MISMATCH'));
  assert.equal((await rows(f)).facts.length,0);
});

test('incomplete and duplicated target sets cannot settle a frozen batch',async()=>{
  const f=await setup({count:2});
  const missing=structuredClone(f.request);missing.payload.observations.pop();rehash(missing);
  await assert.rejects(applyMetricsSubmission(pool,missing,f.principal),matches('DATA.VERSION_CONFLICT'));
  const duplicate=structuredClone(f.request);duplicate.payload.observations[1]=duplicate.payload.observations[0];rehash(duplicate);
  await assert.rejects(applyMetricsSubmission(pool,duplicate,f.principal),matches('CONTRACT.INVALID'));
  assert.equal((await rows(f)).receipt.length,0);
});

test('failure after facts and receipt SQL rolls back facts/submission/receipt/checkpoint together',async()=>{
  const f=await setup();
  const failing=intercept((c,sql,values)=>sql.includes('INSERT INTO ingestion.batch_checkpoints')?c.query('SELECT 1/0'):c.query(sql,values));
  await assert.rejects(applyMetricsSubmission(failing,f.request,f.principal),matches('22012'));
  for(const list of Object.values(await rows(f)))assert.equal(list.length,0);
  assert.equal((await lookup(f)).status,'NOT_OBSERVED');
  assert.equal((await applyMetricsSubmission(pool,f.request,f.principal)).status,'APPLIED');
});

test('lost commit acknowledgement reconciles by original identity; failure before commit remains retryable',async()=>{
  for(const commitFirst of [true,false]) {
    const f=await setup();
    const flaky=intercept(async(c,sql,values)=>{
      if(sql==='COMMIT'){if(commitFirst)await c.query(sql,values);throw Object.assign(new Error('simulated acknowledgement loss'),{code:'ECONNRESET'});}
      return c.query(sql,values);
    });
    await assert.rejects(applyMetricsSubmission(flaky,f.request,f.principal),matches('DB.COMMIT_UNKNOWN'));
    const observation=await lookup(f);assert.equal(validateWire(observation),true,JSON.stringify(validateWire.errors));
    assert.equal(observation.status,commitFirst?'APPLIED':'NOT_OBSERVED');
    const applied=await applyMetricsSubmission(pool,f.request,f.principal);
    if(commitFirst)assert.deepEqual(applied,observation);
    assert.equal((await rows(f)).receipt.length,1);
  }
});

test('takeover serializes after an already-authorized transaction, without invalidating its receipt',async()=>{
  const f=await setup();let entered,release;
  const atFacts=new Promise(resolve=>{entered=resolve;});const gate=new Promise(resolve=>{release=resolve;});
  const blocked=intercept(async(c,sql,values)=>{if(sql.includes('INSERT INTO crawler.contents')){entered();await gate;}return c.query(sql,values);});
  const applying=applyMetricsSubmission(blocked,f.request,f.principal);
  await atFacts;
  assert.equal((await lookup(f)).status,'NOT_OBSERVED'); // Not a permanent negative receipt: the transaction is still running.
  const takeover=takeOverExecution(pool,f.definition.scope,f.definition.execution_epoch,'next-worker',300);
  release();
  const [receipt,epoch]=await Promise.all([applying,takeover]);
  assert.equal(epoch,'9007199254740994');assert.deepEqual(await lookup(f),receipt);
});

test('plan preparation is idempotent without renewing lease; immutable rows and 20-digit range are enforced',async()=>{
  const f=await setup({epoch:'99999999999999999999'});
  const beforeLease=(await pool.query('SELECT expires_at FROM control.execution_authorizations WHERE scope=$1',[f.definition.scope])).rows[0].expires_at;
  assert.equal((await prepareMetricsPlan(pool,f.definition)).created,false);
  assert.deepEqual((await pool.query('SELECT expires_at FROM control.execution_authorizations WHERE scope=$1',[f.definition.scope])).rows[0].expires_at,beforeLease);
  await assert.rejects(takeOverExecution(pool,f.definition.scope,f.definition.execution_epoch,'next',300),matches('CONTRACT.INVALID'));
  for(const value of ['1.5','100000000000000000000','-1'])await assert.rejects(pool.query('SELECT $1::platform.counter20',[value]),matches('23514'));
  await assert.rejects(pool.query("UPDATE ingestion.logical_batches SET policy_version='changed' WHERE logical_batch_key=$1",[f.definition.identity.logical_batch_key]),matches('23514'));
  const receipt=await applyMetricsSubmission(pool,f.request,f.principal);
  await assert.rejects(pool.query('UPDATE ingestion.receipts SET recorded_at=clock_timestamp() WHERE receipt_id=$1',[receipt.receipt_id]),matches('23514'));
  await assert.rejects(querySubmissionReceipt(pool,f.request.submission_id,f.request.payload.identity,f.request.content_sha256,'other-channel'),matches('AUTHZ.CHANNEL_DENIED'));
});
