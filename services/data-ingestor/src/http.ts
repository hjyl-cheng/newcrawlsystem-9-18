import { createHash, timingSafeEqual } from 'node:crypto';
import { createServer, type IncomingMessage } from 'node:http';

export interface QueryClient { tokenSha256: string; channels: readonly string[]; operations: boolean }
export interface HttpDependencies {
  clients: readonly QueryClient[];
  ready(): boolean;
  version: string;
  revision: string;
  metrics(): string;
  receipt(input: {submission_id: string; identity: {plan_id:string;channel_id:string;logical_batch_key:string}; content_sha256:string}, channel: string): Promise<unknown>;
  record(input: {partition:number;offset:string}): Promise<unknown>;
}
class HttpError extends Error { constructor(public status: number, public code: string) { super(code); } }
function shape(value: unknown, keys: string[]): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== keys.length || !keys.every(key=>Object.hasOwn(value,key))) throw new HttpError(400,'INVALID_REQUEST');
}
function stableId(value: unknown): asserts value is string {
  if(typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$/.test(value)) throw new HttpError(400,'INVALID_REQUEST');
}
async function body(req: IncomingMessage): Promise<unknown> {
  if(req.headers['content-type']?.split(';')[0] !== 'application/json' || req.headers['content-encoding']) throw new HttpError(415,'JSON_REQUIRED');
  const chunks: Buffer[]=[];let size=0;
  for await (const chunk of req) {
    size+=chunk.length;
    if(size>4096) throw new HttpError(413,'REQUEST_TOO_LARGE');
    chunks.push(chunk);
  }
  try { return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks))); }
  catch { throw new HttpError(400,'INVALID_JSON'); }
}
export function createIngestorHttp(d: HttpDependencies) {
  let draining=false, active=0;
  const server=createServer(async(req,res)=>{
    res.setHeader('Content-Type','application/json; charset=utf-8');res.setHeader('Cache-Control','no-store');
    const reply=(status:number,value:unknown)=>{res.statusCode=status;res.end(JSON.stringify(value));};
    let counted=false;
    try {
      if(req.method==='GET' && req.url==='/health/live') return reply(200,{status:'ok'});
      if(req.method==='GET' && req.url==='/health/ready') return reply(!draining&&d.ready()?200:503,{status:!draining&&d.ready()?'ready':'unavailable'});
      if(req.method==='GET' && req.url==='/version') return reply(200,{service:'data-ingestor',version:d.version,revision:d.revision});
      if(draining) throw new HttpError(503,'DRAINING');
      const authorization=req.headers.authorization ?? '';
      if(!/^Bearer [A-Za-z0-9_-]{43,128}$/.test(authorization)) throw new HttpError(401,'UNAUTHORIZED');
      const supplied=createHash('sha256').update(authorization.slice(7)).digest();
      const client=d.clients.find(c=>timingSafeEqual(supplied,Buffer.from(c.tokenSha256,'hex')));
      if(!client) throw new HttpError(401,'UNAUTHORIZED');
      if(active>=8) throw new HttpError(429,'QUERY_BUSY');
      active++;counted=true;
      if(req.method==='GET' && req.url==='/metrics') {
        if(!client.operations) throw new HttpError(403,'FORBIDDEN');
        res.setHeader('Content-Type','text/plain; version=0.0.4');return res.end(d.metrics());
      }
      if(req.method!=='POST' || !['/v1/receipts/lookup','/v1/records/lookup'].includes(req.url ?? '')) throw new HttpError(404,'NOT_FOUND');
      if(req.url==='/v1/records/lookup' && !client.operations) throw new HttpError(403,'FORBIDDEN');
      const input=await body(req);
      if(req.url==='/v1/receipts/lookup') {
        shape(input,['submission_id','identity','content_sha256']);stableId(input.submission_id);
        shape(input.identity,['plan_id','channel_id','logical_batch_key']);Object.values(input.identity).forEach(stableId);
        if(typeof input.content_sha256!=='string' || !/^[a-f0-9]{64}$/.test(input.content_sha256)) throw new HttpError(400,'INVALID_REQUEST');
        const channel=String(input.identity.channel_id);
        if(!client.channels.includes(channel)) throw new HttpError(403,'CHANNEL_DENIED');
        return reply(200,await d.receipt(input as Parameters<HttpDependencies['receipt']>[0],channel));
      }
      shape(input,['partition','offset']);
      if(!Number.isInteger(input.partition) || Number(input.partition)<0 || Number(input.partition)>2147483647 || typeof input.offset!=='string' ||
         !/^(0|[1-9][0-9]{0,18})$/.test(input.offset) || BigInt(input.offset)>9223372036854775807n) throw new HttpError(400,'INVALID_REQUEST');
      return reply(200,await d.record(input as Parameters<HttpDependencies['record']>[0]));
    } catch(error) {
      const code=(error as {code?:string}).code;
      if(error instanceof HttpError) reply(error.status,error.code);
      else if(code==='IDEMPOTENCY.PAYLOAD_CONFLICT') reply(409,{error:code});
      else if(code==='CONTRACT.INVALID') reply(400,{error:code});
      else reply(503,{error:'QUERY_UNAVAILABLE'});
    } finally {if(counted)active--;}
  });
  server.requestTimeout=10000;server.headersTimeout=5000;server.keepAliveTimeout=5000;
  server.maxConnections=64;server.maxRequestsPerSocket=100;
  return {server,drain(){draining=true;}};
}
