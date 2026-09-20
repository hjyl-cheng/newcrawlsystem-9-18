import { createHash, randomUUID } from 'node:crypto';
import { withContentFactsTransaction, snapshotContentObservation, type Pool, type FactsTransaction,
  type ContentObservation, type MergeResult } from '../../facts-store/src/content-store.js';

type Identity = { plan_id: string; channel_id: string; logical_batch_key: string };
type Frozen = { generation: string; input_revision: string; input_hash: string; target_hash: string; policy_version: string };
type Target = { source_content_id: string; content_type: 'video' | 'short' | 'live' };
export interface MetricsPayload {
  schema_id: 'content.metrics.batch/1-draft.1';
  identity: Identity;
  frozen_input: Frozen;
  producer_role: 'incremental';
  mode: 'delta';
  observations: ContentObservation[];
}
export interface Submission {
  submission_id: string;
  execution: { scope: string; execution_epoch: string };
  content_sha256: string;
  payload: MetricsPayload;
}
export interface Principal { channelId: string; holderId: string }
export interface PlanDefinition {
  identity: Identity;
  intent_key: string;
  scope: string;
  holder_id: string;
  execution_epoch: string;
  lease_seconds: number;
  frozen_input: Frozen;
  targets: Target[];
}
export class SubmissionError extends Error {
  constructor(public readonly code: string) { super(code); }
}
function check(value: unknown, code = 'CONTRACT.INVALID'): asserts value {
  if (!value) throw new SubmissionError(code);
}
function keys(value: unknown, names: string[]) {
  check(value && typeof value === 'object' && !Array.isArray(value));
  check(Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name)));
}
function id(value: unknown) { check(typeof value === 'string' && value.length <= 256 && /^[A-Za-z0-9][A-Za-z0-9:._/-]*$/.test(value)); }
function counter(value: unknown) { check(typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value)); }
function hash(value: unknown) { check(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)); }
function identity(value: Identity) { keys(value, ['plan_id','channel_id','logical_batch_key']); Object.values(value).forEach(id); }
function frozen(value: Frozen) {
  keys(value, ['generation','input_revision','input_hash','target_hash','policy_version']);
  counter(value.generation); counter(value.input_revision); hash(value.input_hash); hash(value.target_hash); id(value.policy_version);
}
function scalarString(value: string) {
  for (const char of value) { const cp = char.codePointAt(0)!; check(cp < 0xd800 || cp > 0xdfff); }
  return JSON.stringify(value);
}
/** RFC 8785 subset: JSON only, scalar Unicode, safe integer numbers. Counts/revisions use strings. */
export function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return scalarString(value);
  if (typeof value === 'boolean') return String(value);
  if (typeof value === 'number') { check(Number.isSafeInteger(value)); return JSON.stringify(value); }
  if (Array.isArray(value)) return '[' + Array.from(value, canonicalJson).join(',') + ']';
  check(value && typeof value === 'object' && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null));
  const object = value as Record<string, unknown>;
  return '{' + Object.keys(object).sort().map(key => scalarString(key) + ':' + canonicalJson(object[key])).join(',') + '}';
}
export function contentHash(value: unknown) { return createHash('sha256').update(canonicalJson(value), 'utf8').digest('hex'); }
function targetsSorted(targets: Target[]): Target[] {
  check(Array.isArray(targets) && targets.length > 0 && targets.length <= 100);
  for (const target of targets) {
    keys(target, ['source_content_id','content_type']); id(target.source_content_id);
    check(['video','short','live'].includes(target.content_type));
  }
  check(new Set(targets.map(t => t.source_content_id)).size === targets.length);
  return [...targets].sort((a,b) => a.source_content_id < b.source_content_id ? -1 : a.source_content_id > b.source_content_id ? 1 : 0);
}
export function targetHash(targets: Target[]) { return contentHash(targetsSorted(targets)); }
function lease(seconds: number) { check(Number.isInteger(seconds) && seconds > 0 && seconds <= 86400); }

/** Trusted control-plane primitive, not a Worker self-authorization API. Creates no Temporal work. */
export async function prepareMetricsPlan(pool: Pool, definition: PlanDefinition) {
  keys(definition, ['identity','intent_key','scope','holder_id','execution_epoch','lease_seconds','frozen_input','targets']);
  identity(definition.identity); frozen(definition.frozen_input);
  [definition.intent_key, definition.scope, definition.holder_id].forEach(id);
  counter(definition.execution_epoch); lease(definition.lease_seconds);
  const d = structuredClone(definition), targets = targetsSorted(d.targets);
  check(targetHash(targets) === d.frozen_input.target_hash, 'DATA.VERSION_CONFLICT');
  const intentHash = contentHash({ ...d, targets });
  return withContentFactsTransaction(pool, async tx => {
    const i = d.identity, f = d.frozen_input;
    await tx.query('INSERT INTO control.channel_coordination(channel_id) VALUES ($1) ON CONFLICT DO NOTHING', [i.channel_id]);
    const coordination = (await tx.query('SELECT * FROM control.channel_coordination WHERE channel_id=$1 FOR UPDATE', [i.channel_id])).rows[0]!;
    const existing = (await tx.query('SELECT * FROM control.plans WHERE plan_id=$1 OR intent_key=$2', [i.plan_id,d.intent_key])).rows;
    if (existing.length) {
      check(existing.length === 1 && existing[0]!.plan_id === i.plan_id && existing[0]!.intent_hash === intentHash, 'IDEMPOTENCY.PAYLOAD_CONFLICT');
      return { planId: i.plan_id, created: false }; // Does not renew/revive execution rights.
    }
    check(coordination.active_plan_id === null, 'CONTROL.ACTIVE_PLAN_EXISTS');
    await tx.query(`INSERT INTO control.plans(plan_id,channel_id,intent_key,intent_hash,kind,policy_version)
      VALUES ($1,$2,$3,$4,'INCREMENTAL',$5)`, [i.plan_id,i.channel_id,d.intent_key,intentHash,f.policy_version]);
    await tx.query('UPDATE control.channel_coordination SET active_plan_id=$2 WHERE channel_id=$1', [i.channel_id,i.plan_id]);
    await tx.query(`INSERT INTO control.execution_authorizations(scope,plan_id,channel_id,execution_epoch,holder_id,expires_at)
      VALUES ($1,$2,$3,$4,$5,clock_timestamp()+$6*interval '1 second')`, [d.scope,i.plan_id,i.channel_id,d.execution_epoch,d.holder_id,d.lease_seconds]);
    const values = [i.plan_id,i.channel_id,f.generation,f.input_revision,f.input_hash,f.target_hash,f.policy_version];
    await tx.query(`INSERT INTO control.domain_obligations(plan_id,channel_id,domain,generation,input_revision,input_hash,target_hash,policy_version,work_sealed)
      VALUES ($1,$2,'VIDEO_METRICS',$3,$4,$5,$6,$7,true)`, values);
    await tx.query(`INSERT INTO ingestion.logical_batches(logical_batch_key,plan_id,channel_id,domain,generation,input_revision,input_hash,target_hash,policy_version)
      VALUES ($8,$1,$2,'VIDEO_METRICS',$3,$4,$5,$6,$7)`, [...values,i.logical_batch_key]);
    for (const [ordinal,target] of targets.entries()) await tx.query(`INSERT INTO control.domain_items(logical_batch_key,source_content_id,content_type,ordinal)
      VALUES ($1,$2,$3,$4)`, [i.logical_batch_key,target.source_content_id,target.content_type,ordinal]);
    return { planId: i.plan_id, created: true };
  });
}

/** Explicit takeover with compare-and-swap; normal retry does not advance epoch. */
export async function takeOverExecution(pool: Pool, scope: string, expectedEpoch: string, holderId: string, leaseSeconds: number) {
  id(scope); counter(expectedEpoch); id(holderId); lease(leaseSeconds);
  return withContentFactsTransaction(pool, async tx => {
    const initial = (await tx.query('SELECT channel_id FROM control.execution_authorizations WHERE scope=$1', [scope])).rows[0];
    check(initial, 'AUTHZ.EXECUTION_STALE');
    const coordination = (await tx.query('SELECT * FROM control.channel_coordination WHERE channel_id=$1 FOR UPDATE', [initial.channel_id])).rows[0]!;
    const auth = (await tx.query(`SELECT a.*,p.lifecycle_state FROM control.execution_authorizations a
      JOIN control.plans p ON p.plan_id=a.plan_id WHERE a.scope=$1 FOR UPDATE OF a,p`, [scope])).rows[0]!;
    check(auth.execution_epoch === expectedEpoch && auth.state === 'active' && auth.lifecycle_state === 'active' &&
      coordination.active_plan_id === auth.plan_id, 'AUTHZ.EXECUTION_STALE');
    const epoch = (BigInt(expectedEpoch) + 1n).toString(); counter(epoch);
    await tx.query(`UPDATE control.execution_authorizations SET execution_epoch=$2,holder_id=$3,
      expires_at=clock_timestamp()+$4*interval '1 second' WHERE scope=$1`, [scope,epoch,holderId,leaseSeconds]);
    return epoch;
  });
}

function receipt(row: Record<string, unknown>) {
  return { contract_version: 'data-plane/1-draft.1', kind: 'SUBMISSION_RECEIPT',
    identity: { plan_id: row.plan_id, channel_id: row.channel_id, logical_batch_key: row.logical_batch_key, submission_id: row.submission_id },
    content_sha256: row.content_sha256, receipt_id: row.receipt_id, status: 'APPLIED',
    recorded_at: (row.recorded_at as Date).toISOString() };
}
async function findReceipt(tx: FactsTransaction, submissionId: string, i: Identity, expectedHash: string) {
  const row = (await tx.query('SELECT * FROM ingestion.receipts WHERE submission_id=$1', [submissionId])).rows[0];
  if (!row) return null;
  return matchedReceipt(row, i, expectedHash);
}
function matchedReceipt(row: Record<string, unknown>, i: Identity, expectedHash: string) {
  check(row.plan_id === i.plan_id && row.channel_id === i.channel_id && row.logical_batch_key === i.logical_batch_key &&
    row.content_sha256 === expectedHash, 'IDEMPOTENCY.PAYLOAD_CONFLICT');
  return receipt(row);
}

/** Principal comes from a trusted authentication adapter, never from the submitted payload. */
export async function applyMetricsSubmission(pool: Pool, request: Submission, principal: Principal) {
  return withContentFactsTransaction(pool, prepareMetricsApply(request, principal));
}

/** Validate/hash before acquiring a PG connection; closure owns immutable input snapshots. */
export function prepareMetricsApply(request: Submission, principal: Principal) {
  keys(request, ['submission_id','execution','content_sha256','payload']); id(request.submission_id); hash(request.content_sha256);
  keys(request.execution, ['scope','execution_epoch']); id(request.execution.scope); counter(request.execution.execution_epoch);
  id(principal.holderId); id(principal.channelId);
  const payload = request.payload;
  keys(payload, ['schema_id','identity','frozen_input','producer_role','mode','observations']);
  identity(payload.identity); frozen(payload.frozen_input);
  check(payload.schema_id === 'content.metrics.batch/1-draft.1' && payload.producer_role === 'incremental' && payload.mode === 'delta');
  check(principal.channelId === payload.identity.channel_id, 'AUTHZ.CHANNEL_DENIED');
  check(Array.isArray(payload.observations) && payload.observations.length > 0 && payload.observations.length <= 100);
  for (const observation of payload.observations) {
    snapshotContentObservation(observation);
    check(observation.channelId === payload.identity.channel_id, 'DATA.VERSION_CONFLICT');
  }
  const targets = targetsSorted(payload.observations.map(o => ({source_content_id:o.sourceContentId,content_type:o.contentType})));
  check(targetHash(targets) === payload.frozen_input.target_hash, 'DATA.VERSION_CONFLICT');
  const bytes = Buffer.from(canonicalJson(payload), 'utf8');
  check(bytes.length <= 1048576, 'CONTRACT.PAYLOAD_TOO_LARGE');
  check(createHash('sha256').update(bytes).digest('hex') === request.content_sha256, 'OBJECT.HASH_MISMATCH');
  const r = structuredClone(request), actor = structuredClone(principal), p = r.payload, i = p.identity, f = p.frozen_input;
  return async (tx: FactsTransaction) => {
    // Global submission identity is serialized even for simultaneous requests targeting different channels.
    await tx.query('SELECT pg_advisory_xact_lock(244,hashtext($1))', [r.submission_id]);
    const previous = await findReceipt(tx, r.submission_id, i, r.content_sha256);
    if (previous) return previous; // A durable result remains readable after takeover/cancellation.
    const coordination = (await tx.query('SELECT * FROM control.channel_coordination WHERE channel_id=$1 FOR UPDATE', [i.channel_id])).rows[0];
    check(coordination?.active_plan_id === i.plan_id, 'AUTHZ.EXECUTION_STALE');
    const auth = (await tx.query(`SELECT a.*,p.lifecycle_state FROM control.execution_authorizations a
      JOIN control.plans p ON p.plan_id=a.plan_id WHERE a.scope=$1 FOR UPDATE OF a,p`, [r.execution.scope])).rows[0];
    check(auth && auth.plan_id === i.plan_id && auth.channel_id === i.channel_id && auth.holder_id === actor.holderId &&
      auth.execution_epoch === r.execution.execution_epoch && auth.state === 'active' && auth.lifecycle_state === 'active', 'AUTHZ.EXECUTION_STALE');
    const validLease = (await tx.query('SELECT expires_at > clock_timestamp() AS valid FROM control.execution_authorizations WHERE scope=$1', [r.execution.scope])).rows[0];
    check(validLease?.valid, 'AUTHZ.EXECUTION_STALE');
    const batch = (await tx.query('SELECT * FROM ingestion.logical_batches WHERE logical_batch_key=$1 FOR UPDATE', [i.logical_batch_key])).rows[0];
    check(batch && batch.plan_id === i.plan_id && batch.channel_id === i.channel_id &&
      Object.entries(f).every(([key,value]) => batch[key] === value), 'DATA.VERSION_CONFLICT');
    const frozenTargets = (await tx.query('SELECT source_content_id,content_type FROM control.domain_items WHERE logical_batch_key=$1 ORDER BY ordinal', [i.logical_batch_key])).rows;
    check(canonicalJson(frozenTargets) === canonicalJson(targets), 'DATA.VERSION_CONFLICT');
    check(!(await tx.query('SELECT 1 FROM ingestion.receipts WHERE logical_batch_key=$1', [i.logical_batch_key])).rows.length, 'IDEMPOTENCY.BATCH_ALREADY_APPLIED');
    await tx.query(`INSERT INTO ingestion.submissions(submission_id,logical_batch_key,plan_id,channel_id,original_scope,original_execution_epoch,
      original_holder_id,payload_schema_id,hash_profile,content_sha256,canonical_payload)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'jcs-sha256-v1',$9,$10)`,
    [r.submission_id,i.logical_batch_key,i.plan_id,i.channel_id,r.execution.scope,r.execution.execution_epoch,actor.holderId,p.schema_id,r.content_sha256,bytes]);
    const outcomes: (MergeResult & {sourceContentId: string})[] = [];
    const ordered = [...p.observations].sort((a,b) => a.sourceContentId < b.sourceContentId ? -1 : a.sourceContentId > b.sourceContentId ? 1 : 0);
    for (const observation of ordered) outcomes.push({ sourceContentId: observation.sourceContentId, ...await tx.mergeContent(observation) });
    const row = (await tx.query(`INSERT INTO ingestion.receipts(receipt_id,submission_id,logical_batch_key,plan_id,channel_id,content_sha256,status)
      VALUES ($1,$2,$3,$4,$5,$6,'APPLIED') RETURNING *`, ['receipt:'+randomUUID(),r.submission_id,i.logical_batch_key,i.plan_id,i.channel_id,r.content_sha256])).rows[0]!;
    await tx.query(`INSERT INTO ingestion.batch_checkpoints(logical_batch_key,submission_id,receipt_id,item_count,item_outcomes)
      VALUES ($1,$2,$3,$4,$5::jsonb)`, [i.logical_batch_key,r.submission_id,row.receipt_id,outcomes.length,JSON.stringify(outcomes)]);
    return receipt(row);
  };
}

export async function querySubmissionReceipt(pool: Pool, submissionId: string, batchIdentity: Identity, expectedHash: string, authorizedChannelId: string) {
  id(submissionId); identity(batchIdentity); hash(expectedHash);
  check(authorizedChannelId === batchIdentity.channel_id, 'AUTHZ.CHANNEL_DENIED');
  return withContentFactsTransaction(pool, async tx => {
    const row = (await tx.query(`SELECT r.*,statement_timestamp() AS observed_at FROM (VALUES(1)) AS anchor(id)
      LEFT JOIN ingestion.receipts r ON r.submission_id=$1`, [submissionId])).rows[0]!;
    if (row.receipt_id !== null) return matchedReceipt(row,batchIdentity,expectedHash);
    return { contract_version:'data-plane/1-draft.1', kind:'RECEIPT_OBSERVATION',
      identity:{...batchIdentity,submission_id:submissionId}, expected_content_sha256:expectedHash,
      status:'NOT_OBSERVED', observed_at:(row.observed_at as Date).toISOString() };
  });
}
