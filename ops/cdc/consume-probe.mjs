import sdk from '@confluentinc/kafka-javascript';
import {setTimeout as delay} from 'node:timers/promises';
const {Kafka,logLevel}=sdk.KafkaJS;
const [runId,countText]=process.argv.slice(2),target=Number(countText);
if(!/^[a-f0-9]{32}$/.test(runId)||!Number.isSafeInteger(target))throw Error('Invalid probe arguments');
const consumer=new Kafka({kafkaJS:{clientId:'cdc-probe',brokers:['10.4.4.2:9092','10.4.4.8:9092','10.4.4.5:9092'],logLevel:logLevel.NOTHING}}).consumer({kafkaJS:{groupId:'cdc-probe-'+runId,fromBeginning:true,autoCommit:false}});
const ids=new Set();let error,duplicates=0;
try{
 await consumer.connect();await consumer.subscribe({topics:['crawler.publication.validation.v1']});
 await consumer.run({eachMessage:async({topic,partition,message})=>{
  try{
   if(!message.value)return;const data=JSON.parse(message.value.toString());if(data.run_id!==runId)return;
   const id=message.headers?.id?.toString();if(id!==data.event_id)throw Error('Event id header mismatch');
   if(message.key?.toString()!==data.channel_id)throw Error('Routing key mismatch');
   if(ids.has(id))duplicates++;ids.add(id);
   console.log(JSON.stringify({phase:'event',id,seq:data.seq,partition,offset:message.offset,topic}));
  }catch(e){error=e;}
 }});
 console.log(JSON.stringify({phase:'ready'}));
 const deadline=Date.now()+360000;
 while(ids.size<target && !error && Date.now()<deadline)await delay(200);
 if(error)throw error;if(ids.size!==target)throw Error('Missing probe messages: '+ids.size+'/'+target);
 console.log(JSON.stringify({phase:'passed',unique:ids.size,duplicates}));
}finally{await consumer.disconnect();}
