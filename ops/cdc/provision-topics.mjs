// Infrastructure-only topics; production publication topics are not created here.
import sdk from '@confluentinc/kafka-javascript';
const {Kafka,logLevel}=sdk.KafkaJS;
const kafka=new Kafka({kafkaJS:{clientId:'cdc-provision',brokers:['10.4.4.2:9092','10.4.4.8:9092','10.4.4.5:9092'],logLevel:logLevel.NOTHING}});
const admin=kafka.admin();
const specs=[['crawl.connect.validation.configs',1,'compact'],['crawl.connect.validation.offsets',3,'compact'],['crawl.connect.validation.status',3,'compact'],['crawler.publication.validation.v1',3,'delete'],['__debezium-heartbeat.crawl_cdc_validation',1,'compact']];
try {
 await admin.connect();
 await admin.createTopics({waitForLeaders:true,topics:specs.map(([topic,numPartitions,cleanup])=>({topic,numPartitions,replicationFactor:3,configEntries:[{name:'cleanup.policy',value:cleanup},{name:'min.insync.replicas',value:'2'},...(cleanup==='delete'?[{name:'retention.ms',value:'604800000'},{name:'retention.bytes',value:'268435456'}]:[])]}))});
 const meta=await admin.fetchTopicMetadata({topics:specs.map(s=>s[0])});
 for(const topic of meta) {
  const spec=specs.find(s=>s[0]===topic.name);
  if(topic.partitions.length!==spec[1]||topic.partitions.some(p=>p.replicas.length!==3||p.isr.length!==3))throw Error('Topic replica/partition mismatch '+topic.name);
 }
 console.log(JSON.stringify({status:'PASSED',topics:meta.map(t=>({name:t.name,partitions:t.partitions.length,replication:3,minISR:2}))},null,2));
} finally {await admin.disconnect();}
