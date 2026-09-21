import {operatorTls,secureBrokers} from './client.mjs';
// Read-only, on-demand inspection. Not a scheduled monitor or alert delivery service.
import sdk from '@confluentinc/kafka-javascript';
const {Kafka,logLevel}=sdk.KafkaJS;
const topic='crawler.results.validation.v1', groupId='crawler-results-apply-validation-v1';
const kafka=new Kafka({...operatorTls(),kafkaJS:{clientId:'infra-kafka-health',
  brokers:secureBrokers,logLevel:logLevel.NOTHING}});
const admin=kafka.admin();
try {
  await admin.connect();
  const metadata=await admin.fetchTopicMetadata();
  const problems=[];
  const topics=metadata.map(t=>({topic:t.name,partitions:t.partitions.map(p=>{
    if(p.leader<0)problems.push(`${t.name}:${p.partitionId}: no leader`);
    if(p.replicas.length!==3 || p.isr.length!==3)problems.push(`${t.name}:${p.partitionId}: not three synchronized replicas`);
    return {partition:p.partitionId,leader:p.leader,replicas:p.replicas,isr:p.isr};
  })}));
  const [committed]=await admin.fetchOffsets({groupId,topics:[topic]});
  const ends=await admin.fetchTopicOffsets(topic);
  const partitions=ends.map(p=>{
    const value=committed?.partitions.find(c=>c.partition===p.partition);
    if(value?.error || !value || BigInt(value.offset)<0) {
      problems.push(`${topic}:${p.partition}: committed offset unknown`);
      return {...p,committed:null,lag:null,retentionGap:null};
    }
    const offset=BigInt(value.offset),low=BigInt(p.low),high=BigInt(p.high);
    const retentionGap=offset<low;
    if(retentionGap || offset>high)problems.push(`${topic}:${p.partition}: committed offset outside retained range`);
    return {...p,committed:String(offset),lag:String(high-offset),retentionGap};
  });
  const described=await admin.describeGroups([groupId]);
  const group=described.groups[0];
  if(!group?.members?.length)problems.push(`${groupId}: no active consumer members`);
  console.log(JSON.stringify({checkedAt:new Date().toISOString(),status:problems.length?'ATTENTION':'PASS',
    topics,consumer:{groupId,members:group?.members?.length??0,partitions},problems,
    scope:'Point-in-time topic ISR and result-consumer offsets; does not check quorum, disks, lag age, TLS, backups or deliver alerts'},null,2));
  if(problems.length)process.exitCode=1;
} catch(error) {
  console.error(JSON.stringify({status:'ERROR',message:error.message}));process.exitCode=1;
} finally {await admin.disconnect();}
