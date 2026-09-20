import {test} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {canonicalJson,contentHash} from '../dist/services/ingestion/src/metrics-submission.js';
import {encodeResult,decodeResult,MAX_RECORD_BYTES} from '../dist/services/kafka-results/src/transport.js';

const pair=generateKeyPairSync('ed25519');
const payload=JSON.parse(readFileSync('contracts/data-plane/fixtures/content-metrics-batch.json'));
const request={submission_id:'transport:example:1',execution:{scope:'transport:scope',execution_epoch:'1'},content_sha256:contentHash(payload),payload};
const signer={keyId:'worker-test-key',privateKey:pair.privateKey,principal:{holderId:'worker-test',channelId:payload.identity.channel_id}};
const trust=new Map([[signer.keyId,{publicKey:pair.publicKey,holderId:signer.principal.holderId,allowedChannels:new Set([payload.identity.channel_id])}]]);
test('signed Kafka record preserves submission and derives holder from trusted key registration',()=>{
  const encoded=encodeResult(request,signer);const decoded=decodeResult(encoded,trust);
  assert.deepEqual(decoded.request,request);assert.deepEqual(decoded.principal,signer.principal);
  assert.equal(encoded.key.toString(),payload.identity.channel_id);
});
test('tampering, wrong channel key, unknown signer, noncanonical JSON and invalid UTF-8 are rejected',()=>{
  const encoded=encodeResult(request,signer);
  const edited=JSON.parse(encoded.value);edited.signed.submission.payload.observations[0].comment.value='999';
  for(const record of [
    {...encoded,value:Buffer.from(canonicalJson(edited))},
    {...encoded,key:Buffer.from('wrong-channel')},
    {...encoded,value:Buffer.from(JSON.stringify(JSON.parse(encoded.value),null,2))},
    {...encoded,value:Buffer.from([0xff])},
    {...encoded,value:null},
    {...encoded,value:Buffer.alloc(MAX_RECORD_BYTES+1)},
  ])assert.throws(()=>decodeResult(record,trust));
  assert.throws(()=>decodeResult(encoded,new Map()));
  const restricted=new Map([[signer.keyId,{...trust.get(signer.keyId),allowedChannels:new Set()}]]);
  assert.throws(()=>decodeResult(encoded,restricted));
});
test('producer refuses an invalid content hash before publishing',()=>{
  assert.throws(()=>encodeResult({...request,content_sha256:'0'.repeat(64)},signer),e=>e.code==='OBJECT.HASH_MISMATCH');
});
