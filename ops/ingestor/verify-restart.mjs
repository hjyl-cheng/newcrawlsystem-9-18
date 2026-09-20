/** Re-send accepted synthetic submissions after/during a controlled rollout; never grants new work. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createPrivateKey} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import pg from 'pg';
import sdk from '@confluentinc/kafka-javascript';
import {resultProducer,publishResult} from '../../dist/services/kafka-results/src/transport.js';
const dir=new URL('../../secrets/data-ingestor/',import.meta.url);
const previous=JSON.parse(await readFile(new URL('last-acceptance.json',dir),'utf8'));
const operator=JSON.parse(await readFile(new URL('operator.json',dir),'utf8'));
const privateKey=createPrivateKey(await readFile(new URL('worker-private.pem',dir)));
assert.equal(process.env.PGDATABASE,'crawler_validation_ingestor');assert.equal(process.env.PGUSER,'crawler');
const url=process.env.INGESTOR_URL??'http://127.0.0.1:18081';
const pool=new pg.Pool({max:2});
const kafka=new sdk.KafkaJS.Kafka({kafkaJS:{brokers:['10.4.4.2:9092','10.4.4.8:9092','10.4.4.5:9092'],clientId:'ingestor-replay-acceptance',logLevel:sdk.KafkaJS.logLevel.ERROR}});
const producer=resultProducer(kafka),admin=kafka.admin();const positions=[];
try{
  await producer.connect();await admin.connect();
  const rounds=Number(process.env.REPLAY_ROUNDS??'1');assert.ok(Number.isInteger(rounds)&&rounds>=1&&rounds<=100);
  for(let n=0;n<rounds;n++){
    for(const f of previous.fixtures){
      const row=(await pool.query('SELECT * FROM ingestion.submissions WHERE submission_id=$1',[f.lookup.submission_id])).rows[0];
      const payload=JSON.parse(row.canonical_payload.toString('utf8'));
      const request={submission_id:row.submission_id,execution:{scope:row.original_scope,execution_epoch:row.original_execution_epoch},content_sha256:row.content_sha256,payload};
      const ack=await publishResult(producer,previous.topic,request,{keyId:operator.keyId,privateKey,principal:{holderId:operator.holderId,channelId:payload.identity.channel_id}});
      positions.push(...ack.positions);
    }
    if(rounds>1)await delay(1000);
  }
  const deadline=Date.now()+60000;
  for(;;){
    const [state]=await admin.fetchOffsets({groupId:'crawler-results-apply-validation-v1',topics:[previous.topic]});
    if(positions.every(p=>BigInt(state.partitions.find(x=>x.partition===p.partition)?.offset??'-1')>BigInt(p.offset)))break;
    assert.ok(Date.now()<deadline,'Replay consumption timeout');await delay(300);
  }
  for(const f of previous.fixtures){
    const response=await fetch(url+'/v1/receipts/lookup',{method:'POST',headers:{authorization:'Bearer '+operator.token,'content-type':'application/json'},body:JSON.stringify(f.lookup),signal:AbortSignal.timeout(5000)});
    assert.equal(response.status,200);assert.deepEqual(await response.json(),f.receipt);
    assert.equal((await pool.query('SELECT count(*)::int AS n FROM ingestion.receipts WHERE submission_id=$1',[f.lookup.submission_id])).rows[0].n,1);
    assert.equal((await pool.query('SELECT comment_count::text AS n FROM crawler.contents WHERE source_content_id=$1',[f.sourceContentId])).rows[0].n,f.commentCount);
  }
  console.log(JSON.stringify({replayed:positions.length,receiptsUnchanged:previous.fixtures.length,consumerCaughtUp:true}));
}finally{await Promise.allSettled([producer.disconnect(),admin.disconnect(),pool.end()]);}
