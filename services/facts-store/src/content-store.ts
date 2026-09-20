/** Trusted internal Store. No Worker entrypoint or authorization decision is implemented here. */
interface Client {
  query(sql: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  release(error?: Error): void;
}
export interface Pool { connect(): Promise<Client> }
type MetricStatus = 'exact' | 'estimated' | 'zero_from_empty' | 'zero_from_surface' |
  'zero_from_upcoming' | 'disabled' | 'unavailable' | 'unresolved';
export interface Metric {
  value: string | null;
  status: MetricStatus;
  source: string;
  disabled?: boolean | null;
  text?: string | null;
}
export interface ContentObservation {
  channelId: string;
  sourceContentId: string;
  contentType: 'video' | 'short' | 'live';
  observedAt: string;
  view?: Metric;
  like?: Metric;
  comment?: Metric;
  page?: Record<string, unknown> | null;
}
type Outcome = 'applied' | 'unknown' | 'older' | 'same' | 'retained' | 'absent';
export interface MergeResult {
  contentKey: string;
  view: Outcome;
  like: Outcome;
  comment: Outcome;
  page: Outcome;
}
export class FactsError extends Error {
  constructor(public readonly code: string) { super(code); }
}
export class CommitUnknownError extends Error {
  readonly code = 'DB.COMMIT_UNKNOWN';
  constructor(cause: unknown) { super('Commit acknowledgement unavailable; reconcile by original submission identity', { cause }); }
}
function ensure(condition: unknown, code = 'INVALID_OBSERVATION'): asserts condition {
  if (!condition) throw new FactsError(code);
}
function utcMillis(value: unknown): asserts value is string {
  ensure(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value));
  const date = new Date(value);
  ensure(Number.isFinite(date.getTime()) && date.toISOString() === value);
}
function identifier(value: unknown) {
  ensure(typeof value === 'string' && value.length > 0 && value.length <= 256 && value.trim() === value);
}
const statuses = {
  view: ['exact', 'estimated', 'unavailable', 'unresolved'],
  like: ['exact', 'zero_from_empty', 'unavailable', 'unresolved'],
  comment: ['exact', 'zero_from_empty', 'zero_from_surface', 'zero_from_upcoming', 'disabled', 'unavailable', 'unresolved'],
} as const;
type Kind = keyof typeof statuses;
const kinds: Kind[] = ['view', 'like', 'comment'];
function validateMetric(kind: Kind, metric: Metric) {
  ensure(metric && typeof metric === 'object' && !Array.isArray(metric));
  ensure(Object.keys(metric).every(key => ['value', 'status', 'source', ...(kind === 'comment' ? ['disabled'] : []), ...(kind === 'view' ? ['text'] : [])].includes(key)));
  ensure((statuses[kind] as readonly string[]).includes(metric.status));
  ensure(typeof metric.source === 'string' && metric.source.trim().length > 0 && metric.source.length <= 256);
  const unknown = ['unavailable', 'unresolved'].includes(metric.status);
  if (unknown) ensure(metric.value === null);
  else {
    ensure(typeof metric.value === 'string' && /^(0|[1-9][0-9]{0,18})$/.test(metric.value));
    ensure(BigInt(metric.value) <= 9223372036854775807n);
    if (metric.status.startsWith('zero_') || metric.status === 'disabled') ensure(metric.value === '0');
  }
  if (kind === 'comment') {
    ensure(metric.disabled === undefined || metric.disabled === null || typeof metric.disabled === 'boolean');
    ensure(metric.status === 'disabled' ? metric.disabled === true : metric.disabled !== true);
  }
  if (kind === 'view') ensure(metric.text === undefined || metric.text === null || typeof metric.text === 'string');
}
// Reject values JSON.stringify would silently omit, change, or round. Page item semantics follow later.
function jsonValue(value: unknown): void {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number') { ensure(Number.isSafeInteger(value)); return; }
  ensure(typeof value === 'object');
  ensure(Array.isArray(value) || Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  for (const item of Object.values(value)) jsonValue(item);
}
export function snapshotContentObservation(input: ContentObservation): ContentObservation {
  ensure(input && typeof input === 'object' && !Array.isArray(input));
  ensure(Object.keys(input).every(key => ['channelId','sourceContentId','contentType','observedAt','view','like','comment','page'].includes(key)));
  identifier(input.channelId); identifier(input.sourceContentId); utcMillis(input.observedAt);
  ensure(['video','short','live'].includes(input.contentType));
  for (const kind of kinds) if (input[kind] !== undefined) validateMetric(kind, input[kind]);
  if (input.page !== undefined && input.page !== null) {
    jsonValue(input.page);
    utcMillis(input.page.collected_at);
    ensure(input.page.collected_at <= input.observedAt, 'PAGE_FROM_FUTURE');
  }
  // Own the values before the first await: a caller cannot mutate a pending observation.
  return structuredClone(input);
}

export interface FactsTransaction {
  mergeContent(input: ContentObservation): Promise<MergeResult>;
  /** Same transaction for future facts/checkpoints/receipts. Trusted server code only. */
  query(sql: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

export async function withContentFactsTransaction<T>(pool: Pool, work: (tx: FactsTransaction) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  let active = true;
  let failed: unknown;
  let busy = false;
  let inFlight: Promise<MergeResult> | undefined;
  let broken: Error | undefined;
  const query = async (sql: string, values?: unknown[]) => {
    ensure(active, 'TRANSACTION_CLOSED');
    try { return await client.query(sql, values); }
    catch (error) { failed = error; throw error; }
  };
  const tx: FactsTransaction = {
    query,
    async mergeContent(input) {
      ensure(active, 'TRANSACTION_CLOSED');
      // One ordered operation per transaction. Concurrency uses separate pool connections.
      if (busy) { failed = new FactsError('CONCURRENT_TRANSACTION_USE'); throw failed; }
      busy = true;
      try {
        inFlight = merge(query, snapshotContentObservation(input));
        return await inFlight;
      }
      catch (error) { failed = error; throw error; }
      finally { busy = false; }
    },
  };
  try {
    await client.query('BEGIN ISOLATION LEVEL READ COMMITTED');
    await client.query("SET LOCAL lock_timeout = '10s'");
    await client.query("SET LOCAL statement_timeout = '30s'");
    const result = await work(tx);
    if (failed) throw failed; // Catching an application conflict cannot commit earlier partial merges.
    ensure(!busy, 'UNAWAITED_STORE_OPERATION');
    active = false;
    try { await client.query('COMMIT'); }
    catch (error) {
      const code = (error as {code?: string}).code;
      if (code === '40001' || code === '40P01') throw error;
      broken = new CommitUnknownError(error);
      throw broken;
    }
    return result;
  } catch (error) {
    active = false;
    // A callback that fails while a query is pending must not release a still-used connection.
    if (inFlight) await inFlight.catch(() => undefined);
    try { await client.query('ROLLBACK'); }
    catch (rollbackError) { broken = rollbackError instanceof Error ? rollbackError : new Error('Rollback failed'); }
    throw error;
  } finally {
    active = false;
    client.release(broken);
  }
}

async function merge(query: FactsTransaction['query'], input: ContentObservation): Promise<MergeResult> {
  if (input.page !== undefined && input.page !== null) {
    const validation = await query('SELECT crawler.valid_comment_page($1::jsonb) AS valid', [JSON.stringify(input.page)]);
    ensure(validation.rows[0]?.valid === true, 'INVALID_COMMENT_PAGE');
  }
  await query(`INSERT INTO crawler.contents(channel_id, platform, resource_kind, source_content_id, content_type, first_seen_at, last_seen_at)
    VALUES ($1, 'youtube', 'youtube_video', $2, $3, $4, $4)
    ON CONFLICT (platform, resource_kind, source_content_id) DO NOTHING`,
  [input.channelId, input.sourceContentId, input.contentType, input.observedAt]);
  const current = (await query(`SELECT content_key, channel_id, content_type FROM crawler.contents
    WHERE platform = 'youtube' AND resource_kind = 'youtube_video' AND source_content_id = $1 FOR UPDATE`, [input.sourceContentId])).rows[0];
  ensure(current, 'CONTENT_DISAPPEARED');
  ensure(current.channel_id === input.channelId, 'CHANNEL_IDENTITY_CONFLICT');
  // Type correction is a separate evidence policy, not silently inferred from metrics observations.
  ensure(current.content_type === input.contentType, 'CONTENT_TYPE_CORRECTION_REQUIRED');
  const key = String(current.content_key);
  const result: MergeResult = { contentKey: key, view: 'absent', like: 'absent', comment: 'absent', page: 'absent' };
  for (const kind of kinds) {
    const metric = input[kind];
    if (!metric) continue;
    if (metric.value === null) { result[kind] = 'unknown'; continue; }
    // All interpolated identifiers come exclusively from the fixed kinds list above.
    const column = `${kind}_count`;
    const row = (await query(`SELECT ${column}::text AS value, ${column}_status AS status, ${column}_source AS source,
      comments_disabled AS disabled, view_count_text AS text,
      CASE WHEN ${column}_observed_at IS NULL OR $2::timestamptz > ${column}_observed_at THEN 'newer'
        WHEN $2::timestamptz = ${column}_observed_at THEN 'same' ELSE 'older' END AS ordering
      FROM crawler.contents WHERE content_key = $1`, [key, input.observedAt])).rows[0]!;
    if (row.ordering === 'older') { result[kind] = 'older'; continue; }
    const disabled = metric.status === 'disabled' ? true : metric.disabled ?? null;
    if (row.ordering === 'same') {
      ensure(row.value === metric.value && row.status === metric.status && row.source === metric.source &&
        (kind !== 'comment' || row.disabled === disabled) &&
        (kind !== 'view' || row.text === (metric.text ?? null)), 'OBSERVATION_TIME_CONFLICT');
      result[kind] = 'same'; continue;
    }
    await query(`UPDATE crawler.contents SET ${column} = $2, ${column}_status = $3,
      ${column}_source = $4, ${column}_observed_at = $5
      ${kind === 'comment' ? ', comments_disabled = $6' : kind === 'view' ? ', view_count_text = $6' : ''}
      WHERE content_key = $1`, [key, metric.value, metric.status, metric.source, input.observedAt,
      ...(kind === 'comment' ? [disabled] : kind === 'view' ? [metric.text ?? null] : [])]);
    result[kind] = 'applied';
  }
  if (input.page !== undefined && input.page !== null) {
    const pageTime = input.page.collected_at;
    const row = (await query(`SELECT COALESCE((comments_first_page->>'returned_count')::numeric, 0) > 0 AS nonempty,
      comments_first_page IS NULL AS missing,
      COALESCE(comments_page_checked_at, comments_first_page_observed_at) > $2::timestamptz AS older,
      COALESCE(comments_page_checked_at, comments_first_page_observed_at) = $2::timestamptz AS same,
      comments_first_page = $3::jsonb AS equal
      FROM crawler.contents WHERE content_key = $1`, [key, pageTime, JSON.stringify(input.page)])).rows[0]!;
    if (row.older) result.page = 'older';
    else if (row.nonempty) result.page = 'retained';
    else {
      if (row.same) ensure(row.equal, 'OBSERVATION_TIME_CONFLICT');
      if (row.missing || Number(input.page.returned_count) > 0) {
        await query('UPDATE crawler.contents SET comments_first_page = $2::jsonb WHERE content_key = $1', [key, JSON.stringify(input.page)]);
        result.page = 'applied';
      } else result.page = 'retained';
    }
    if (!row.older) await query('UPDATE crawler.contents SET comments_page_checked_at = $2 WHERE content_key = $1', [key, pageTime]);
  }
  await query(`UPDATE crawler.contents SET first_seen_at = LEAST(first_seen_at, $2::timestamptz),
    last_seen_at = GREATEST(last_seen_at, $2::timestamptz) WHERE content_key = $1`, [key, input.observedAt]);
  return result;
}
