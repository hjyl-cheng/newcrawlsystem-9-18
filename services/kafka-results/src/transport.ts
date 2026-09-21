import { createHash, sign, verify, type KeyObject } from 'node:crypto';
import type { KafkaJS } from '@confluentinc/kafka-javascript';
type Kafka = KafkaJS.Kafka;
type Consumer = KafkaJS.Consumer;
type Producer = KafkaJS.Producer;
import { canonicalJson, prepareMetricsApply, type Submission, type Principal } from '../../ingestion/src/metrics-submission.js';
import { withContentFactsTransaction, type FactsTransaction, type Pool } from '../../facts-store/src/content-store.js';

export const MAX_RECORD_BYTES = 1048576;
export const RESULT_TOPIC = 'crawler.results.validation.v1';
export const RESULT_GROUP = 'crawler-results-apply-validation-v1';
export const VERSION = 'crawler-result-kafka/1-draft.1';
export class KafkaResultError extends Error {
  constructor(public readonly code: string) { super(code); }
}
function ensure(value: unknown, code: string): asserts value { if (!value) throw new KafkaResultError(code); }
function exact(value: unknown, names: string[]) {
  ensure(value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value,name)), 'KAFKA.CONTRACT_INVALID');
}
function stableId(value: unknown) { ensure(typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$/.test(value), 'KAFKA.CONTRACT_INVALID'); }
function digest(value: Buffer | string) { return createHash('sha256').update(value).digest('hex'); }
function signingBytes(signed: unknown) { return Buffer.from(VERSION+'\n'+canonicalJson(signed),'utf8'); }

export interface SigningIdentity { keyId: string; privateKey: KeyObject; principal: Principal }
export interface VerificationIdentity { publicKey: KeyObject; holderId: string; allowedChannels: ReadonlySet<string> }
export type TrustStore = ReadonlyMap<string, VerificationIdentity>;
export interface ResultRecord { streamId: string; topic: string; partition: number; offset: string; key: Buffer | null; value: Buffer | null }
export interface RecordOutcome { disposition: 'APPLIED' | 'QUARANTINED'; receiptId: string | null; errorCode: string | null }

export function encodeResult(request: Submission, signer: SigningIdentity) {
  prepareMetricsApply(request,signer.principal); // Business shape/hash checks; no database activity.
  stableId(signer.keyId);
  ensure(signer.privateKey.asymmetricKeyType === 'ed25519', 'KAFKA.SIGNING_KEY_INVALID');
  const signed = { transport_version: VERSION, key_id: signer.keyId, submission: request };
  const signature = sign(null,signingBytes(signed),signer.privateKey).toString('base64');
  const value = Buffer.from(canonicalJson({signed,signature}),'utf8');
  ensure(value.length <= MAX_RECORD_BYTES,'KAFKA.RECORD_TOO_LARGE');
  return {key:Buffer.from(request.payload.identity.channel_id,'utf8'),value};
}

export function decodeResult(record: Pick<ResultRecord,'key'|'value'>, trust: TrustStore) {
  ensure(record.value && record.value.length <= MAX_RECORD_BYTES, 'KAFKA.RECORD_INVALID_SIZE');
  let envelope: {signed:{transport_version:string;key_id:string;submission:Submission};signature:string};
  try {
    const text = new TextDecoder('utf-8',{fatal:true}).decode(record.value);
    envelope = JSON.parse(text);
    exact(envelope,['signed','signature']); exact(envelope.signed,['transport_version','key_id','submission']);
    ensure(canonicalJson(envelope) === text, 'KAFKA.NONCANONICAL_RECORD');
  } catch { throw new KafkaResultError('KAFKA.CONTRACT_INVALID'); }
  ensure(envelope.signed.transport_version === VERSION,'KAFKA.VERSION_UNSUPPORTED');
  stableId(envelope.signed.key_id);
  const identity = trust.get(envelope.signed.key_id);
  ensure(identity && identity.publicKey.asymmetricKeyType === 'ed25519','KAFKA.SIGNATURE_INVALID');
  ensure(typeof envelope.signature === 'string','KAFKA.SIGNATURE_INVALID');
  const signature = Buffer.from(envelope.signature,'base64');
  ensure(signature.length === 64 && signature.toString('base64') === envelope.signature &&
    verify(null,signingBytes(envelope.signed),identity.publicKey,signature),'KAFKA.SIGNATURE_INVALID');
  const request = envelope.signed.submission;
  const channel = request?.payload?.identity?.channel_id;
  ensure(typeof channel === 'string' && identity.allowedChannels.has(channel),'KAFKA.CHANNEL_DENIED');
  ensure(record.key?.equals(Buffer.from(channel,'utf8')),'KAFKA.PARTITION_KEY_MISMATCH');
  const principal = {holderId:identity.holderId,channelId:channel};
  return {request,principal};
}

/** acks=-1 + idempotent producer; Kafka acknowledgment is deliberately not a PG receipt. */
export function resultProducer(kafka: Kafka) {
  return kafka.producer({'partitioner':'murmur2_random','queue.buffering.max.kbytes':4096,'queue.buffering.max.messages':1000,
    'message.timeout.ms':30000,'linger.ms':5,kafkaJS:{allowAutoTopicCreation:false,idempotent:true,maxInFlightRequests:1,acks:-1,retry:{retries:5}}});
}
export async function publishResult(producer: Producer, topic: string, request: Submission, signer: SigningIdentity) {
  const message = encodeResult(request,signer);
  const submissionId = request.submission_id, contentSha256 = request.content_sha256;
  let metadata;
  try { metadata = await producer.send({topic,messages:[message]}); }
  catch { throw new KafkaResultError('KAFKA.PUBLISH_UNKNOWN'); }
  return {status:'BROKER_ACKED' as const,submissionId,contentSha256,positions:metadata.map(m=>({topic:m.topicName,partition:m.partition,offset:m.offset ?? m.baseOffset}))};
}

const permanentErrors = new Set([
  'KAFKA.CONTRACT_INVALID','KAFKA.VERSION_UNSUPPORTED','KAFKA.RECORD_INVALID_SIZE','KAFKA.SIGNATURE_INVALID',
  'KAFKA.CHANNEL_DENIED','KAFKA.PARTITION_KEY_MISMATCH',
  'CONTRACT.INVALID','CONTRACT.PAYLOAD_TOO_LARGE','AUTHZ.CHANNEL_DENIED','AUTHZ.EXECUTION_STALE',
  'DATA.VERSION_CONFLICT','OBJECT.HASH_MISMATCH','IDEMPOTENCY.PAYLOAD_CONFLICT','IDEMPOTENCY.BATCH_ALREADY_APPLIED',
  'INVALID_OBSERVATION','INVALID_COMMENT_PAGE','PAGE_FROM_FUTURE','OBSERVATION_TIME_CONFLICT',
  'CHANNEL_IDENTITY_CONFLICT','CONTENT_TYPE_CORRECTION_REQUIRED',
]);
function errorCode(error: unknown) { return typeof (error as {code?:unknown})?.code === 'string' ? (error as {code:string}).code : 'UNCLASSIFIED'; }
async function lockRecord(tx: FactsTransaction, record: ResultRecord, sha: string) {
  const identity = [record.streamId,record.topic,record.partition,record.offset];
  await tx.query('SELECT pg_advisory_xact_lock(245,hashtext($1))',[canonicalJson(identity)]);
  const existing = (await tx.query(`SELECT * FROM ingestion.kafka_record_outcomes
    WHERE stream_id=$1 AND topic=$2 AND partition_id=$3 AND record_offset=$4`,identity)).rows[0];
  if (!existing) return null;
  ensure(existing.record_sha256 === sha,'KAFKA.STREAM_IDENTITY_CONFLICT');
  return {disposition:existing.disposition as RecordOutcome['disposition'],receiptId:existing.receipt_id as string|null,errorCode:existing.error_code as string|null};
}
async function saveOutcome(tx: FactsTransaction, record: ResultRecord, sha: string, outcome: RecordOutcome) {
  await tx.query(`INSERT INTO ingestion.kafka_record_outcomes
    (stream_id,topic,partition_id,record_offset,record_sha256,disposition,receipt_id,error_code,key_bytes,raw_value)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
  [record.streamId,record.topic,record.partition,record.offset,sha,outcome.disposition,outcome.receiptId,outcome.errorCode,
    record.key,outcome.disposition === 'QUARANTINED' ? record.value : null]);
}

export async function processResultRecord(pool: Pool, record: ResultRecord, trust: TrustStore): Promise<RecordOutcome> {
  stableId(record.streamId); stableId(record.topic);
  ensure(Number.isInteger(record.partition) && record.partition >= 0 && /^(0|[1-9][0-9]{0,18})$/.test(record.offset) &&
    BigInt(record.offset) <= 9223372036854775807n,'KAFKA.COORDINATE_INVALID');
  ensure((record.value?.length ?? 0) <= 2097152 && (record.key?.length ?? 0) <= 4096,'KAFKA.QUARANTINE_LIMIT_EXCEEDED');
  // Buffer copies own the record across async work. Identity includes null versus empty bytes.
  const owned = {...record,key:record.key ? Buffer.from(record.key) : null,value:record.value ? Buffer.from(record.value) : null};
  const sha = digest(canonicalJson({key:owned.key?.toString('base64') ?? null,value:owned.value?.toString('base64') ?? null}));
  try {
    const decoded = decodeResult(owned,trust);
    const apply = prepareMetricsApply(decoded.request,decoded.principal);
    return await withContentFactsTransaction(pool,async tx=>{
      const existing = await lockRecord(tx,owned,sha); if(existing)return existing;
      const receipt = await apply(tx);
      const outcome: RecordOutcome = {disposition:'APPLIED',receiptId:String(receipt.receipt_id),errorCode:null};
      await saveOutcome(tx,owned,sha,outcome);
      return outcome;
    });
  } catch(error) {
    const code = errorCode(error);
    if(!permanentErrors.has(code))throw error; // DB/commit/unknown failures retain the Kafka offset for retry.
    return withContentFactsTransaction(pool,async tx=>{
      const existing = await lockRecord(tx,owned,sha); if(existing)return existing;
      const outcome: RecordOutcome = {disposition:'QUARANTINED',receiptId:null,errorCode:code};
      await saveOutcome(tx,owned,sha,outcome);
      return outcome;
    });
  }
}

export interface ConsumerOptions {
  kafka: Kafka;
  topic: string;
  groupId: string;
  streamId: string;
  partitionsConcurrent?: number;
  retryBaseMs?: number;
  handleRecord(record: ResultRecord): Promise<RecordOutcome>;
  onEvent?(event: {kind:'durable'|'retry';partition:number;offset:string;code?:string;disposition?:string}): void;
}

/** Read this consumer's own group via the consumer OffsetFetch path.
 * librdkafka 2.15.1 coordinator-targeted Admin requests can abort the process on
 * connection loss (upstream #5397). Consumer.committed avoids that Admin path;
 * errors still propagate, so offset validation remains fail-closed.
 */
export async function readCommittedOffsets(consumer: Consumer, topic: string, partitions: number[], timeout=5000) {
  const offsets=await consumer.committed(partitions.map(partition=>({topic,partition})),timeout);
  // SDK 1.10.1 maps both an unset native offset and numeric zero to null.
  // Both mean next=0 for this stream; the retained-low check still rejects gaps.
  ensure(partitions.every(partition=>offsets.some(p=>p.topic===topic&&p.partition===partition&&
    (p.offset===null||/^-?[0-9]+$/.test(p.offset)))),
    'KAFKA.COMMITTED_OFFSETS_INCOMPLETE');
  const normalized=offsets.map(p=>({...p,offset:p.offset??'0'}));
  ensure(normalized.every(p=>BigInt(p.offset)<=BigInt(Number.MAX_SAFE_INTEGER)),'KAFKA.OFFSET_SDK_RANGE');
  return normalized;
}
export async function startResultConsumer(options: ConsumerOptions) {
  const concurrency = options.partitionsConcurrent ?? 2;
  ensure(Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 16,'KAFKA.CONCURRENCY_INVALID');
  const retryBase = options.retryBaseMs ?? 1000;
  ensure(Number.isInteger(retryBase) && retryBase >= 10 && retryBase <= 30000,'KAFKA.RETRY_INVALID');
  const consumer: Consumer = options.kafka.consumer({'queued.max.messages.kbytes':4096,'js.consumer.max.batch.size':8,
    kafkaJS:{groupId:options.groupId,allowAutoTopicCreation:false,autoCommit:false,fromBeginning:true,
    sessionTimeout:30000,heartbeatInterval:3000,maxBytesPerPartition:1100000,maxBytes:2200000,
    minBytes:1,maxWaitTimeInMs:500,readUncommitted:false}});
  let stopping=false;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const attempts = new Map<number,number>();
  const admin=options.kafka.admin();
  // No implicit "jump to earliest" after retention or topic recreation. Check again per batch.
  const verifyOffsets=async()=>{
    const ranges=await admin.fetchTopicOffsets(options.topic);
    const committed=await readCommittedOffsets(consumer,options.topic,ranges.map(r=>r.partition));
    for(const range of ranges){
      const saved=committed.find(p=>p.partition===range.partition)?.offset ?? '-1';
      const next=BigInt(saved)<0n ? 0n : BigInt(saved);
      ensure(next>=BigInt(range.low) && next<=BigInt(range.high),'KAFKA.RETENTION_OR_STREAM_GAP');
    }
  };
  const emit = (event: Parameters<NonNullable<ConsumerOptions['onEvent']>>[0]) => {
    try { options.onEvent?.(event); } catch { /* Observability must not change acknowledgment semantics. */ }
  };
  try {
    await admin.connect();
    await consumer.connect();
    await verifyOffsets();
    await consumer.subscribe({topic:options.topic});
    await consumer.run({eachBatchAutoResolve:false,partitionsConsumedConcurrently:concurrency,
      eachBatch:async({batch,resolveOffset,isRunning,isStale,pause})=>{
        let checked=false;
        for(const message of batch.messages) {
          if(stopping || !isRunning() || isStale())break;
          try {
            if(!checked){await verifyOffsets();checked=true;}
            ensure(BigInt(message.offset) < BigInt(Number.MAX_SAFE_INTEGER),'KAFKA.OFFSET_SDK_RANGE');
            const outcome = await options.handleRecord({streamId:options.streamId,topic:batch.topic,partition:batch.partition,
              offset:message.offset,key:message.key,value:message.value});
            if(stopping || !isRunning() || isStale())break;
            // Store commit returns before offset+1 is committed. Offset is never converted to Number.
            await consumer.commitOffsets([{topic:batch.topic,partition:batch.partition,offset:(BigInt(message.offset)+1n).toString()}]);
            resolveOffset(message.offset);
            attempts.delete(batch.partition);
            emit({kind:'durable',partition:batch.partition,offset:message.offset,disposition:outcome.disposition});
          } catch(error) {
            if(stopping || !isRunning() || isStale())break;
            const attempt = Math.min((attempts.get(batch.partition) ?? 0)+1,10);attempts.set(batch.partition,attempt);
            const resume=pause();
            const delay=Math.floor(Math.random()*Math.min(30000,retryBase*2**attempt));
            const timer=setTimeout(()=>{timers.delete(timer);if(!stopping)resume();},delay);timers.add(timer);
            emit({kind:'retry',partition:batch.partition,offset:message.offset,code:errorCode(error)});
            break; // Do not process or commit a later offset from this partition.
          }
        }
      },
    });
  } catch(error) {await Promise.allSettled([consumer.disconnect(),admin.disconnect()]);throw error;}
  return {consumer,async stop(){stopping=true;for(const timer of timers)clearTimeout(timer);timers.clear();
    try{await consumer.disconnect();}finally{await admin.disconnect();}}};
}
