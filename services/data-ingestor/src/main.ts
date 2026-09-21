import pg from 'pg';
import sdk from '@confluentinc/kafka-javascript';
import { readCredentials } from './config.js';
import { createIngestorHttp } from './http.js';
import { querySubmissionReceipt } from '../../ingestion/src/metrics-submission.js';
import { processResultRecord, startResultConsumer, readCommittedOffsets } from '../../kafka-results/src/transport.js';

const required=(name:string)=>{const value=process.env[name];if(!value)throw new Error('MISSING_'+name);return value;};
const database=required('PGDATABASE'), expected=required('DB_EXPECTED_NAME');
if(database!==expected || database!=='crawler_validation_ingestor')throw new Error('VALIDATION_DATABASE_REQUIRED');
const topic=required('RESULT_TOPIC'),groupId=required('RESULT_GROUP'),streamId=required('RESULT_STREAM_ID');
if(topic!=='crawler.results.validation.v1'||groupId!=='crawler-results-apply-validation-v1')throw new Error('VALIDATION_STREAM_REQUIRED');
const port=Number(process.env.PORT??'8080');if(!Number.isInteger(port)||port<1||port>65535)throw new Error('INVALID_PORT');
const {trust,clients}=readCredentials(process.env.CREDENTIALS_DIR??'/var/run/ingestor');
const pool=new pg.Pool({host:required('PGHOST'),port:Number(required('PGPORT')),database,user:required('PGUSER'),password:required('PGPASSWORD'),
  max:4,connectionTimeoutMillis:5000,idleTimeoutMillis:30000,statement_timeout:30000,idle_in_transaction_session_timeout:45000,
  application_name:'data-ingestor-validation'});
const kafka=new sdk.KafkaJS.Kafka({kafkaJS:{brokers:required('KAFKA_BROKERS').split(','),clientId:'data-ingestor-'+(process.env.HOSTNAME??'local'),logLevel:sdk.KafkaJS.logLevel.ERROR}});
const admin=kafka.admin();
let runner:Awaited<ReturnType<typeof startResultConsumer>>|undefined,stopping=false,checkedAt=0,dependenciesOk=false;
let applied=0,quarantined=0,retries=0,lag: {partition:number;lag:string;gap:boolean}[]=[];
const blocked=new Set<number>();
const log=(event:string,detail:object={})=>console.log(JSON.stringify({service:'data-ingestor',event,...detail}));
pool.on('error',()=>{dependenciesOk=false;log('database_connection_error');});
const http=createIngestorHttp({clients,version:process.env.APP_VERSION??'development',revision:process.env.APP_REVISION??'development',
  ready:()=>!!runner&&!stopping&&dependenciesOk&&Date.now()-checkedAt<20000&&blocked.size===0,
  metrics:()=>[
    '# TYPE ingestor_applied_total counter',`ingestor_applied_total ${applied}`,
    '# TYPE ingestor_quarantined_total counter',`ingestor_quarantined_total ${quarantined}`,
    '# TYPE ingestor_retry_total counter',`ingestor_retry_total ${retries}`,
    '# TYPE ingestor_dependencies_ready gauge',`ingestor_dependencies_ready ${dependenciesOk&&Date.now()-checkedAt<20000?1:0}`,
    '# TYPE ingestor_metrics_sample_timestamp_seconds gauge',`ingestor_metrics_sample_timestamp_seconds ${checkedAt/1000}`,
    '# TYPE ingestor_assigned_partitions gauge',`ingestor_assigned_partitions ${runner?.consumer.assignment().length??0}`,
    '# TYPE ingestor_blocked_partitions gauge',`ingestor_blocked_partitions ${blocked.size}`,
    '# TYPE ingestor_group_lag gauge',...lag.map(p=>`ingestor_group_lag{partition="${p.partition}"} ${p.lag}`),
    '# TYPE ingestor_retention_gap gauge',...lag.map(p=>`ingestor_retention_gap{partition="${p.partition}"} ${p.gap?1:0}`),''].join('\n'),
  receipt:(input,channel)=>querySubmissionReceipt(pool,input.submission_id,input.identity,input.content_sha256,channel),
  async record(input){const row=(await pool.query(`SELECT disposition,receipt_id,error_code,record_sha256,recorded_at,
    octet_length(key_bytes) AS key_size,octet_length(raw_value) AS value_size FROM ingestion.kafka_record_outcomes
    WHERE stream_id=$1 AND topic=$2 AND partition_id=$3 AND record_offset=$4`,[streamId,topic,input.partition,input.offset])).rows[0];
    return row?{...row,stream_id:streamId,topic,...input}:{status:'NOT_OBSERVED',stream_id:streamId,topic,...input};},
});
let timer:ReturnType<typeof setTimeout>|undefined;
async function checkDependencies(){
  try{
    const db=(await pool.query('SELECT current_database() AS name,pg_is_in_recovery() AS standby')).rows[0];
    if(db.name!==expected||db.standby)throw new Error('DATABASE_IDENTITY');
    const ranges=await admin.fetchTopicOffsets(topic,{timeout:5000,isolationLevel:sdk.KafkaJS.IsolationLevel.READ_COMMITTED});
    if(!runner)throw new Error('CONSUMER_NOT_STARTED');
    const committed=await readCommittedOffsets(runner.consumer,topic,ranges.map(r=>r.partition));
    lag=ranges.map(r=>{const saved=committed.find(c=>c.partition===r.partition)?.offset??'-1';const next=BigInt(saved)<0n?0n:BigInt(saved);
      return {partition:r.partition,lag:(BigInt(r.high)>next?BigInt(r.high)-next:0n).toString(),gap:next<BigInt(r.low)||next>BigInt(r.high)};});
    // Only local retry state matters; after rebalance discard partitions no longer owned.
    const assigned=new Set(runner?.consumer.assignment().map(p=>p.partition)??[]);
    for(const p of blocked)if(!assigned.has(p))blocked.delete(p);
    dependenciesOk=lag.length===3&&!lag.some(p=>p.gap);checkedAt=Date.now();
  }catch{dependenciesOk=false;log('dependency_check_failed');}
  if(!stopping)timer=setTimeout(()=>void checkDependencies(),5000);
}
async function shutdown(exitCode=0){
  if(stopping)return;stopping=true;http.drain();if(timer)clearTimeout(timer);log('draining');
  const deadline=setTimeout(()=>process.exit(1),50000);deadline.unref();
  const closeHttp=new Promise<void>(resolve=>{http.server.close(()=>resolve());http.server.closeIdleConnections();});
  try{await runner?.stop();await closeHttp;await admin.disconnect();await pool.end();clearTimeout(deadline);log('stopped');process.exitCode=exitCode;}
  catch{process.exit(1);}
}
process.on('SIGTERM',()=>void shutdown());process.on('SIGINT',()=>void shutdown());
try{
  // No migrations or self-granted plans from a runtime process.
  const versions=(await pool.query('SELECT version FROM platform.schema_migrations ORDER BY version')).rows.map(r=>r.version);
  if(versions.join(',')!=='0001,0002,0003,0004')throw new Error('SCHEMA_VERSION_MISMATCH');
  await admin.connect();
  http.server.listen(port,'0.0.0.0',()=>log('listening'));
  runner=await startResultConsumer({kafka,topic,groupId,streamId,partitionsConcurrent:2,
    handleRecord:record=>processResultRecord(pool,record,trust),
    onEvent:event=>{if(event.kind==='retry'){retries++;blocked.add(event.partition);}else{blocked.delete(event.partition);if(event.disposition==='APPLIED')applied++;else quarantined++;}log(event.kind,event);},
  });
  await checkDependencies();log('consumer_started');
}catch{log('startup_failed');await shutdown(1);}
