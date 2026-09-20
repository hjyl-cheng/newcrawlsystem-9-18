import {generateKeyPairSync,randomBytes,createHash} from 'node:crypto';
import {mkdir,writeFile,access} from 'node:fs/promises';
const dir=new URL('../../secrets/data-ingestor/',import.meta.url);
await mkdir(dir,{recursive:true,mode:0o700});
try{await access(new URL('runtime.env',dir));throw new Error('Credentials already exist; use an explicit rotation procedure');}catch(e){if(e.code!=='ENOENT')throw e;}
const {privateKey,publicKey}=generateKeyPairSync('ed25519');
const token=randomBytes(32).toString('base64url'),password=randomBytes(32).toString('hex');
const channels=['ingestor-validation:channel-a','ingestor-validation:channel-b','ingestor-validation:channel-c'];
const files={
  'worker-private.pem':privateKey.export({format:'pem',type:'pkcs8'}),
  'trusted-keys.json':JSON.stringify([{keyId:'validation-worker-1',holderId:'validation-worker',channels,publicKeyPem:publicKey.export({format:'pem',type:'spki'})}]),
  'query-clients.json':JSON.stringify([{tokenSha256:createHash('sha256').update(token).digest('hex'),channels,operations:true}]),
  'operator.json':JSON.stringify({token,channels,keyId:'validation-worker-1',holderId:'validation-worker'}),
  'runtime.env':`PGHOST=10.4.4.2\nPGPORT=5432\nPGDATABASE=crawler_validation_ingestor\nDB_EXPECTED_NAME=crawler_validation_ingestor\nPGUSER=crawler_ingestor_validation\nPGPASSWORD=${password}\n`,
};
for(const [name,data] of Object.entries(files))await writeFile(new URL(name,dir),data,{mode:0o600,flag:'wx'});
console.log('Created local ignored validation credentials; no secret values printed.');
