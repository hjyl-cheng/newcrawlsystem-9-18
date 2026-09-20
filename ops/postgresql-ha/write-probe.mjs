/** Bounded synthetic writes during an operator-triggered database role change. */
import pg from 'pg';
import assert from 'node:assert/strict';
import {setTimeout as delay} from 'node:timers/promises';
const schema=process.argv[2];
assert.match(schema,/^ha_verify_[0-9a-f]{16}$/);
const pool=new pg.Pool({max:1,connectionTimeoutMillis:2000,statement_timeout:30000,idle_in_transaction_session_timeout:45000});
let errors=0,acknowledged=0,last=Date.now(),maxGapMs=0;
pool.on('error',()=>errors++);
const duration=Number(process.argv[3]??70000);
assert.ok(Number.isInteger(duration)&&duration>=10000&&duration<=120000);
const started=Date.now(),deadline=started+duration;
console.log(JSON.stringify({phase:'started',at:new Date().toISOString()}));
try {
 while(Date.now()<deadline){
  try {
   await pool.query({name:'ha-write-probe',text:`INSERT INTO ${schema}.probe(seq) VALUES($1) ON CONFLICT DO NOTHING`,values:[acknowledged+1]});
   acknowledged++;
   const now=Date.now();maxGapMs=Math.max(maxGapMs,now-last);last=now;
  }catch{errors++;}
  await delay(250);
 }
 const {rows}=await pool.query(`SELECT count(*)::int AS count,min(seq) AS first,max(seq) AS last FROM ${schema}.probe WHERE seq<=$1`,[acknowledged]);
 assert.equal(rows[0].count,acknowledged);assert.ok(acknowledged>0);
 console.log(JSON.stringify({phase:'passed',acknowledged,errors,maxGapMs,elapsedMs:Date.now()-started,allAcknowledgedPresent:true}));
}finally {await pool.end();}
