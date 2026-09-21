import {operatorTls,secureBrokers} from './client.mjs';
// Small, destructive-to-one-service infrastructure drill; never targets business topic data.
// Run only in an authorized maintenance window: node ops/kafka/verify-broker-failover.mjs --execute
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {randomUUID, createHash} from 'node:crypto';
import {writeFile} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import sdk from '@confluentinc/kafka-javascript';

assert.equal(process.argv[2], '--execute', 'Requires --execute; temporarily stops one Kafka service');
const exec = promisify(execFile);
const nodes = {1:'10.4.4.2', 2:'10.4.4.8', 3:'10.4.4.5'};
const id = randomUUID();
const topic = `crawler.infra.failover.${id}`;
const strictTopic = `crawler.infra.strict-isr.${id}`;
const unit = `crawl-kafka-recover-${id}`;
const evidencePath = `ops/checks/kafka-failover-${id}.json`;
const {Kafka, logLevel} = sdk.KafkaJS;
const kafka = new Kafka({...operatorTls(),kafkaJS:{brokers:secureBrokers,
  clientId:`infra-${id}`, logLevel:logLevel.NOTHING}});
const admin = kafka.admin();
const producer = kafka.producer({'message.timeout.ms':30000,
  kafkaJS:{allowAutoTopicCreation:false, idempotent:true, acks:-1, maxInFlightRequests:1}});
const strictProducer = kafka.producer({'message.timeout.ms':6000,
  kafkaJS:{allowAutoTopicCreation:false, acks:-1}});
const evidence = {id, startedAt:new Date().toISOString(), topic, strictTopic,
  fault:'Controlled systemctl stop of current KRaft leader (combined broker/controller), not host power loss',
  status:'RUNNING', events:[], acked:[], cleanup:[]};
const consumers = [], groups = [], expected = new Map();
let target, recoveryArmed = false, fatalConsumerError;
let producing = false, loop;
const ssh = async (ip, command) => (await exec('ssh', ['-i','/home/ubuntu/.ssh/id_ed25519_crawl_infra',
  '-o','BatchMode=yes','-o','ConnectTimeout=8',`ubuntu@${ip}`,command], {timeout:45000,maxBuffer:1024*1024})).stdout.trim();
function event(name, detail={}) {
  const row = {name, at:new Date().toISOString(), ...detail};
  evidence.events.push(row); console.log(JSON.stringify(row));
}
async function until(fn, timeout=60000) {
  const end = Date.now()+timeout;
  while(Date.now()<end) {
    if(fatalConsumerError) throw fatalConsumerError;
    const result = await fn(); if(result) return result;
    await delay(300);
  }
  throw new Error('Timed out waiting for expected cluster state');
}
async function metadata(names=[topic,strictTopic]) {
  return admin.fetchTopicMetadata({topics:names,timeout:5000});
}
async function waitIsr(n, names=[topic,strictTopic]) {
  return until(async()=>{
    try {
      const result=await metadata(names);
      return result.every(t=>t.partitions.every(p=>p.leader>=0 && p.replicas.length===3 && p.isr.length===n)) && result;
    } catch(error) {
      if(error.code===3 || error.retriable)return false; // Topic creation / election propagation.
      throw error;
    }
  });
}
async function quorum(ip) {
  const raw = await ssh(ip,`sudo -u kafka /opt/kafka/bin/kafka-metadata-quorum.sh --bootstrap-server ${ip}:9094 --command-config /etc/kafka/tls/node-client.properties describe --status`);
  const leader=Number(raw.match(/^LeaderId:\s+(\d+)/m)?.[1]);
  assert.ok(nodes[leader], 'Unknown quorum leader');
  return {leader,raw};
}
async function reader(suffix) {
  const groupId = `infra-failover-${id}-${suffix}`; groups.push(groupId);
  const consumer = kafka.consumer({kafkaJS:{groupId,fromBeginning:true,autoCommit:false,allowAutoTopicCreation:false}});
  consumers.push(consumer);
  const seen=new Map(), last=new Map(); let duplicates=0;
  await consumer.connect(); await consumer.subscribe({topics:[topic]});
  await consumer.run({eachMessage:async({partition,message})=>{
    try {
      const body = message.value.toString(); const record=JSON.parse(body);
      assert.equal(body,expected.get(record.id), 'Unexpected or corrupted body');
      assert.equal(record.partition,partition);
      const position=`${partition}:${message.offset}`;
      if(seen.has(position)) {duplicates++; return;}
      assert.ok(!last.has(partition) || BigInt(message.offset)>last.get(partition),'Partition offset regressed');
      last.set(partition,BigInt(message.offset)); seen.set(position,body);
    } catch(error) {fatalConsumerError=error;}
  }});
  return {consumer,seen,last,get duplicates(){return duplicates;}};
}
let sequence=0;
async function sendOne(phase) {
  const n=sequence++, partition=n%3;
  const record={id:`${id}:${n}`,partition,sequence:n,phase,payload:'infra-small-message'};
  const value=JSON.stringify(record); expected.set(record.id,value);
  const started=Date.now();
  const [ack]=await producer.send({topic,messages:[{partition,key:record.id,value}]});
  evidence.acked.push({id:record.id,phase,partition,offset:String(ack.offset??ack.baseOffset),
    atMs:Date.now(),latencyMs:Date.now()-started,sha256:createHash('sha256').update(value).digest('hex')});
}
async function caughtUp(reader) {
  await until(()=>evidence.acked.every(a=>reader.seen.has(`${a.partition}:${a.offset}`)));
}

try {
  for(const ip of Object.values(nodes)) assert.equal(await ssh(ip,'systemctl is-active kafka'),'active');
  await admin.connect();
  const existing=await admin.listTopics();
  evidence.beforeTopics=await waitIsr(3,existing);
  evidence.quorumBefore=await quorum(nodes[1]); target=nodes[evidence.quorumBefore.leader];
  evidence.target={id:evidence.quorumBefore.leader,ip:target};
  await admin.createTopics({topics:[topic,strictTopic].map(name=>({topic:name,numPartitions:3,replicationFactor:3,
    configEntries:[{name:'min.insync.replicas',value:name===topic?'2':'3'},
      {name:'unclean.leader.election.enable',value:'false'},
      {name:'retention.ms',value:'3600000'},{name:'segment.bytes',value:'1048576'}]}))});
  evidence.probeBefore=await waitIsr(3);
  await producer.connect(); await strictProducer.connect();
  // Prove strict topic is writable with three ISR before using it as the refusal control.
  await strictProducer.send({topic:strictTopic,messages:[{partition:0,value:'three-isr-baseline'}]});
  const strictBefore=await admin.fetchTopicOffsets(strictTopic); evidence.strictOffsetsBefore=strictBefore;
  const live=await reader('live');
  for(let i=0;i<30;i++) await sendOne('before');
  await caughtUp(live); event('baseline-passed',{acked:evidence.acked.length,target});
  // Independent remote recovery remains effective if this SSH client or process disappears.
  await ssh(target,`sudo systemd-run --unit=${unit} --on-active=5m /usr/bin/systemctl start kafka`);
  recoveryArmed=true;
  producing=true;
  loop=(async()=>{while(producing){await sendOne('transition');await delay(150);}})();
  // Attach a rejection handler immediately; the error is checked before proceeding.
  let loopError; loop.catch(error=>{loopError=error;});
  evidence.stopRequestedMs=Date.now(); event('stopping-service',{target});
  await ssh(target,'sudo systemctl stop kafka');
  assert.equal(await ssh(target,'systemctl show kafka --property=ActiveState --value'),'inactive');
  evidence.stopCompletedMs=Date.now();
  evidence.probeDegraded=await waitIsr(2);
  const survivor=Object.values(nodes).find(ip=>ip!==target);
  evidence.quorumDegraded=await quorum(survivor);
  assert.notEqual(evidence.quorumDegraded.leader,evidence.quorumBefore.leader);
  producing=false; await loop; if(loopError)throw loopError;
  for(let i=0;i<30;i++) await sendOne('one-broker-down');
  await caughtUp(live);
  event('degraded-read-write-passed',{leader:evidence.quorumDegraded.leader,acked:evidence.acked.length});
  let rejected;
  try {await strictProducer.send({topic:strictTopic,messages:[{partition:0,value:'must-not-be-acked-with-two-isr'}]});}
  catch(error) {rejected={code:error.code,message:error.message};}
  assert.ok(rejected,'minISR=3 unexpectedly acknowledged a write with only two ISR');
  evidence.strictRejection=rejected;
  const strictAfter=await admin.fetchTopicOffsets(strictTopic);
  assert.deepEqual(strictAfter.map(p=>[p.partition,p.high]).sort(),strictBefore.map(p=>[p.partition,p.high]).sort());
  evidence.strictOffsetsDuringFault=strictAfter;
  await strictProducer.disconnect();
  event('insufficient-isr-refusal-passed',rejected);
  evidence.restartRequestedMs=Date.now(); await ssh(target,'sudo systemctl start kafka');
  evidence.probeRecovered=await waitIsr(3);
  evidence.allTopicsRecovered=await waitIsr(3,existing);
  evidence.recoveredMs=Date.now();
  evidence.quorumRecovered=await until(async()=>{
    const q=await quorum(survivor);return /^MaxFollowerLag:\s+0\s*$/m.test(q.raw)&&q;
  });
  for(let i=0;i<30;i++) await sendOne('recovered');
  await caughtUp(live);
  await live.consumer.commitOffsets([...live.last].map(([partition,offset])=>({topic,partition,offset:String(offset+1n)})));
  evidence.liveCommittedOffsets=await admin.fetchOffsets({groupId:groups[0],topics:[topic]});
  for(const p of evidence.liveCommittedOffsets[0].partitions)assert.equal(p.offset,String(live.last.get(p.partition)+1n));
  await live.consumer.disconnect();
  const replay=await reader('fresh-replay'); await caughtUp(replay);
  assert.equal(replay.seen.size,evidence.acked.length);
  evidence.replay={records:replay.seen.size,duplicateDeliveries:replay.duplicates,
    allAcknowledgedBodiesMatched:true,partitionOrderChecked:true};
  evidence.live={records:live.seen.size,duplicateDeliveries:live.duplicates};
  const times=evidence.acked.map(a=>a.atMs);
  evidence.maxObservedAckGapMs=Math.max(...times.slice(1).map((t,i)=>t-times[i]));
  evidence.maxTransitionSendLatencyMs=Math.max(...evidence.acked.filter(a=>a.phase==='transition').map(a=>a.latencyMs));
  evidence.timingNote='Observed ACK gaps include deliberate checks/pauses; send latency and topology observations are not host-failure RTO';
  event('recovered-and-full-replay-passed',evidence.replay);
  evidence.status='PASSED';
} catch(error) {
  evidence.status='FAILED'; evidence.error={message:error.message,stack:error.stack}; process.exitCode=1;
  event('failed',{message:error.message});
} finally {
  producing=false;
  // Recover first, before waiting for client teardown or cleanup.
  let restored=!recoveryArmed;
  if(recoveryArmed) {
    try {
      await ssh(target,'sudo systemctl start kafka');
      assert.equal(await ssh(target,'systemctl is-active kafka'),'active');
      restored=true;
      await ssh(target,`sudo systemctl stop ${unit}.timer`);
      evidence.cleanup.push('Kafka active; independent recovery timer removed');
    } catch(error) {evidence.cleanup.push(`Recovery requires attention: ${error.message}`);process.exitCode=1;}
  }
  if(loop) await loop.catch(()=>{});
  for(const client of [...consumers,producer,strictProducer]) {
    try {await client.disconnect();} catch(error) {evidence.cleanup.push(`Client disconnect: ${error.message}`);}
  }
  if(restored) {
    try {
      const owned=[topic,strictTopic];
      // Delete both exact owned names; metadata caches can omit a just-created topic.
      for(const name of owned) {
        try {await admin.deleteTopics({topics:[name],timeout:15000});}
        catch(error) {if(error.code!==3)throw error;}
      }
      await until(async()=>!(await admin.listTopics()).some(t=>owned.includes(t)));
      if(groups.length)await until(async()=>{
        try {await admin.deleteGroups(groups,{timeout:10000});return true;}
        catch(error) {
          // Consumers with autoCommit=false may leave no persistent group (69).
          if(error.groups?.every(g=>g.errorCode===0 || g.errorCode===69))return true;
          if(error.groups?.every(g=>[0,14,15,16,27,68,69].includes(g.errorCode)))return false;
          throw error;
        }
      });
      evidence.cleanup.push('Only this run\'s two probe topics and consumer groups removed');
    } catch(error) {evidence.cleanup.push(`Probe cleanup requires attention: ${error.message}`);process.exitCode=1;}
  }
  await admin.disconnect().catch(()=>{});
  if(process.exitCode)evidence.status='FAILED';
  evidence.finishedAt=new Date().toISOString();
  await writeFile(evidencePath,JSON.stringify(evidence,(_,v)=>typeof v==='bigint'?String(v):v,2)+'\n');
  console.log(JSON.stringify({status:evidence.status,evidencePath,records:evidence.acked.length,cleanup:evidence.cleanup}));
}
