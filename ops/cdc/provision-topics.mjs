import {operatorTls,secureBrokers} from '../kafka/client.mjs';
// Infrastructure-only topics; production publication topics are not created here.
import {setTimeout as delay} from 'node:timers/promises';
import sdk from '@confluentinc/kafka-javascript';
const {Kafka,logLevel}=sdk.KafkaJS;
const kafka=new Kafka({...operatorTls(),kafkaJS:{clientId:'cdc-provision',brokers:secureBrokers,logLevel:logLevel.NOTHING}});
const admin=kafka.admin();
const specs=[['crawl.connect.validation.configs',1,'compact'],['crawl.connect.validation.offsets',3,'compact'],['crawl.connect.validation.status',3,'compact'],['crawler.publication.validation.v1',3,'delete'],['__debezium-heartbeat.crawl_cdc_validation',1,'compact']];
try {
 await admin.connect();
 const existing=await admin.listTopics();
 const missing=specs.filter(s=>!existing.includes(s[0]));
 if(missing.length) await admin.createTopics({topics:missing.map(([topic,numPartitions,cleanup])=>({topic,numPartitions,replicationFactor:3,configEntries:[{name:'cleanup.policy',value:cleanup},{name:'min.insync.replicas',value:'2'},...(cleanup==='delete'?[{name:'retention.ms',value:'604800000'},{name:'retention.bytes',value:'268435456'}]:[])]}))});
 let meta;
 for(let attempt=0;attempt<30;attempt++){
  try {meta=await admin.fetchTopicMetadata({topics:specs.map(s=>s[0])});}
  catch(error){if(error.code!==3)throw error;await delay(500);continue;}
  if(meta.length===specs.length && meta.every(t=>t.partitions.every(p=>p.isr.length===3)))break;
  await delay(500);
 }
 for(const topic of meta) {
  const spec=specs.find(s=>s[0]===topic.name);
  if(topic.partitions.length!==spec[1]||topic.partitions.some(p=>p.replicas.length!==3||p.isr.length!==3))throw Error('Topic replica/partition mismatch '+topic.name);
 }
 console.log(JSON.stringify({status:'PASSED',topics:meta.map(t=>({name:t.name,partitions:t.partitions.length,replication:3,minISR:2}))},null,2));
} finally {await admin.disconnect();}
