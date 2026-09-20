import { createPublicKey } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { TrustStore } from '../../kafka-results/src/transport.js';
import type { QueryClient } from './http.js';

export function readCredentials(directory: string): {trust:TrustStore;clients:QueryClient[]} {
  const keys: unknown=JSON.parse(readFileSync(directory+'/trusted-keys.json','utf8'));
  const clients: unknown=JSON.parse(readFileSync(directory+'/query-clients.json','utf8'));
  const id=(x:unknown)=>typeof x==='string'&&/^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$/.test(x);
  const channels=(x:unknown): x is string[]=>Array.isArray(x)&&x.length>0&&x.length<=1000&&x.every(id)&&new Set(x).size===x.length;
  if(!Array.isArray(keys)||!keys.length||keys.length>100)throw new Error('INVALID_TRUST_REGISTRY');
  const trust=new Map();
  for(const key of keys){
    if(!key||!id(key.keyId)||!id(key.holderId)||!channels(key.channels)||typeof key.publicKeyPem!=='string'||trust.has(key.keyId))throw new Error('INVALID_TRUST_REGISTRY');
    const publicKey=createPublicKey(key.publicKeyPem);
    if(publicKey.asymmetricKeyType!=='ed25519')throw new Error('INVALID_TRUST_REGISTRY');
    trust.set(key.keyId,{publicKey,holderId:key.holderId,allowedChannels:new Set(key.channels)});
  }
  if(!Array.isArray(clients)||!clients.length||clients.length>100||clients.some(c=>!c||typeof c.tokenSha256!=='string'||
    !/^[a-f0-9]{64}$/.test(c.tokenSha256)||!channels(c.channels)||typeof c.operations!=='boolean')||new Set(clients.map(c=>c.tokenSha256)).size!==clients.length)throw new Error('INVALID_QUERY_CLIENTS');
  return {trust,clients};
}
