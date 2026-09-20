import {readFile} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import sdk from '@confluentinc/kafka-javascript';
const {Kafka,logLevel}=sdk.KafkaJS;

const brokers=process.env.KAFKA_BROKERS?.split(',').filter(Boolean);
if(!brokers?.length)throw new Error('KAFKA_BROKERS is required');
const spec=JSON.parse(await readFile(new URL('../../infra/kafka/validation-results.json',import.meta.url),'utf8'));
if(spec.topic!=='crawler.results.validation.v1')throw new Error('This provisioner only owns the validation result topic');
const admin=new Kafka({kafkaJS:{clientId:'provision-validation-results',brokers,logLevel:logLevel.ERROR}}).admin();
try{
  await admin.connect();
  const exists=(await admin.listTopics()).includes(spec.topic);
  const created=exists ? false : await admin.createTopics({topics:[{
    topic:spec.topic,numPartitions:spec.partitions,replicationFactor:spec.replication_factor,
    configEntries:Object.entries(spec.config).map(([name,value])=>({name,value})),
  }]});
  let topic;
  const deadline=Date.now()+15000;
  while(!topic){
    try {const metadata=await admin.fetchTopicMetadata({topics:[spec.topic]});
      if(metadata[0]?.partitions.every(p=>p.leader>=0 && p.isr.length>=2))topic=metadata[0];
    }catch(error){if(!error.retriable)throw error;}
    if(!topic){if(Date.now()>=deadline)throw new Error('Result topic leaders/ISR not ready');await delay(250);}
  }
  if(topic.partitions.length!==spec.partitions || topic.partitions.some(p=>p.replicas.length!==spec.replication_factor || p.isr.length<2))
    throw new Error('Topic topology/ISR differs from validation specification');
  console.log(JSON.stringify({created,topic:spec.topic,partitions:topic.partitions.map(p=>({partition:p.partitionId,replicas:p.replicas,isr:p.isr})),requestedConfig:spec.config,
    existingConfigVerification:'Use kafka-configs.sh --describe on the broker; this SDK admin API does not expose DescribeConfigs.'}));
}finally{await admin.disconnect();}
