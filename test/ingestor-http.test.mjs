import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createHash} from 'node:crypto';
import {createIngestorHttp} from '../dist/services/data-ingestor/src/http.js';
const token='a'.repeat(43), workerToken='b'.repeat(43);
const hash=t=>createHash('sha256').update(t).digest('hex');
async function fixture(run){
  let ready=false,calls=0;
  const app=createIngestorHttp({clients:[{tokenSha256:hash(token),channels:['channel-a'],operations:true},{tokenSha256:hash(workerToken),channels:['channel-a'],operations:false}],
    ready:()=>ready,version:'test',revision:'test',metrics:()=> 'ingestor_retry_total 0\n',
    receipt:async input=>{calls++;return{status:'NOT_OBSERVED',identity:input.identity};},record:async()=>({disposition:'QUARANTINED'})});
  await new Promise(r=>app.server.listen(0,'127.0.0.1',r));const url='http://127.0.0.1:'+app.server.address().port;
  try{await run({app,url,setReady:()=>{ready=true;},calls:()=>calls});}finally{app.server.closeAllConnections();await new Promise(r=>app.server.close(r));}
}
const request={submission_id:'submission-a',identity:{plan_id:'plan-a',channel_id:'channel-a',logical_batch_key:'batch-a'},content_sha256:'0'.repeat(64)};
const post=(url,path,body,secret=token)=>fetch(url+path,{method:'POST',headers:{authorization:'Bearer '+secret,'content-type':'application/json'},body:JSON.stringify(body)});
test('ingestor readiness follows dependencies and withdraws on drain',()=>fixture(async({app,url,setReady})=>{
  assert.equal((await fetch(url+'/health/live')).status,200);assert.equal((await fetch(url+'/health/ready')).status,503);
  setReady();assert.equal((await fetch(url+'/health/ready')).status,200);app.drain();assert.equal((await fetch(url+'/health/ready')).status,503);
  assert.equal((await post(url,'/v1/receipts/lookup',request)).status,503);
}));
test('receipt lookup requires token and channel authorization before database access',()=>fixture(async({url,calls})=>{
  assert.equal((await post(url,'/v1/receipts/lookup',request,'c'.repeat(43))).status,401);
  assert.equal((await post(url,'/v1/receipts/lookup',{...request,identity:{...request.identity,channel_id:'other'}})).status,403);
  assert.equal(calls(),0);const response=await post(url,'/v1/receipts/lookup',request,workerToken);
  assert.equal(response.status,200);assert.equal((await response.json()).status,'NOT_OBSERVED');assert.equal(calls(),1);
}));
test('record metadata and metrics require operations scope; malformed or oversized queries are rejected',()=>fixture(async({url,calls})=>{
  assert.equal((await post(url,'/v1/records/lookup',{partition:0,offset:'0'},workerToken)).status,403);
  assert.equal((await fetch(url+'/metrics',{headers:{authorization:'Bearer '+workerToken}})).status,403);
  assert.equal((await post(url,'/v1/records/lookup',{partition:0,offset:'0'})).status,200);
  assert.equal((await post(url,'/v1/records/lookup',{partition:0,offset:'9223372036854775808'})).status,400);
  assert.equal((await post(url,'/v1/receipts/lookup',{...request,extra:true})).status,400);
  try{assert.equal((await post(url,'/v1/receipts/lookup',{padding:'x'.repeat(5000)})).status,413);}catch(e){if(!e.cause?.code?.startsWith('UND_ERR'))throw e;}
  assert.equal(calls(),0);
}));
