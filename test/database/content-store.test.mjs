import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { before, after, test } from 'node:test';
import pg from 'pg';
import { databaseConfig, migrate } from '../../ops/db/migrate.mjs';
import { withContentFactsTransaction } from '../../dist/services/facts-store/src/content-store.js';

const config = databaseConfig();
assert.match(config.database, /^crawler_schema_test_[a-z0-9_]+$/);
assert.equal(process.env.DB_ALLOW_TEST_WRITES, config.database);
const pool = new pg.Pool({ ...config, max: 4 });
const channel = `store-test-${randomUUID()}`;
const otherChannel = `${channel}-other`;
let sequence = 0;
const t0 = '2026-09-20T00:00:00.000Z';
const t1 = '2026-09-20T01:00:00.000Z';
const t2 = '2026-09-20T02:00:00.000Z';
const t3 = '2026-09-20T03:00:00.000Z';
function observation(extra = {}) {
  return { channelId: channel, sourceContentId: `${channel}-${++sequence}`, contentType: 'video', observedAt: t0, ...extra };
}
function metric(value, extra = {}) { return { value, status: 'exact', source: 'test-observation', ...extra }; }
function page(time, text = 'original') {
  return { version: 1, sort: 'TOP_COMMENTS', collected_at: time, returned_count: 1,
    total_count: 100, comments: [{ id: 'comment-1', text }] };
}
function emptyPage(time) { return { ...page(time), returned_count: 0, comments: [] }; }
function apply(input) { return withContentFactsTransaction(pool, tx => tx.mergeContent(input)); }
async function stored(source) {
  return (await pool.query('SELECT * FROM crawler.contents WHERE source_content_id=$1', [source])).rows[0];
}
function errorCode(code) { return error => error.code === code; }

before(async () => {
  const client = await pool.connect();
  try { await migrate(client, config.database); } finally { client.release(); }
  await pool.query('INSERT INTO crawler.channels(channel_id,channel_url) VALUES ($1,$3),($2,$3)', [channel, otherChannel, 'https://example.test/store']);
});
after(async () => {
  try {
    await pool.query('DELETE FROM crawler.contents WHERE channel_id=ANY($1::text[])', [[channel, otherChannel]]);
    await pool.query('DELETE FROM crawler.channels WHERE channel_id=ANY($1::text[])', [[channel, otherChannel]]);
  } finally { await pool.end(); }
});

test('new video stores bigint exactly and first page with hash/time evidence', async () => {
  const input = observation({ view: metric('9007199254740993'), comment: metric('100'), page: page(t0) });
  const result = await apply(input);
  const row = await stored(input.sourceContentId);
  assert.equal(row.content_key, result.contentKey);
  assert.equal(row.view_count, '9007199254740993');
  assert.equal(row.comment_count, '100');
  assert.deepEqual(row.comments_first_page, input.page);
  assert.match(row.comments_first_page_hash, /^[a-f0-9]{64}$/);
  assert.equal(row.comments_first_page_hash_profile, 'pg17-jsonb-text-sha256-v1');
  assert.equal(row.comments_first_page_observed_at.toISOString(), t0);
  assert.equal(row.first_seen_at.toISOString(), t0);
});

test('new count preserves nonempty page and its hash/time even when incoming page differs', async () => {
  const input = observation({ comment: metric('100'), page: page(t0) });
  await apply(input);
  const original = await stored(input.sourceContentId);
  const result = await apply({ ...input, observedAt: t1, comment: metric('120'), page: page(t1, 'replacement') });
  const row = await stored(input.sourceContentId);
  assert.equal(result.comment, 'applied'); assert.equal(result.page, 'retained');
  assert.equal(row.comment_count, '120');
  assert.equal(row.comment_count_observed_at.toISOString(), t1);
  assert.deepEqual(row.comments_first_page, original.comments_first_page);
  assert.equal(row.comments_first_page_hash, original.comments_first_page_hash);
  assert.equal(row.comments_first_page_observed_at.toISOString(), t0);
  assert.equal(row.comments_page_checked_at.toISOString(), t1);
});

test('unknown or absent metric preserves value, source, disabled state and observation time', async () => {
  const input = observation({ comment: metric('100'), view: metric('50') });
  await apply(input);
  const result = await apply({ ...input, observedAt: t2, view: undefined,
    comment: metric(null, { status: 'unavailable', source: 'network-failure' }) });
  let row = await stored(input.sourceContentId);
  assert.equal(result.comment, 'unknown'); assert.equal(result.view, 'absent');
  assert.equal(row.comment_count, '100'); assert.equal(row.view_count, '50');
  assert.equal(row.comment_count_source, 'test-observation');
  assert.equal(row.comment_count_observed_at.toISOString(), t0);
  // Unknown t2 did not invent a valid-observation watermark that blocks a valid t1 result.
  await apply({ ...input, observedAt: t1, comment: metric('110') });
  row = await stored(input.sourceContentId);
  assert.equal(row.comment_count, '110');
});

test('disabled sets zero without deleting page; a later numeric observation can reopen comments', async () => {
  const input = observation({ comment: metric('100'), page: page(t0) });
  await apply(input);
  await apply({ ...input, observedAt: t1, page: null,
    comment: metric('0', { status: 'disabled', disabled: true }) });
  let row = await stored(input.sourceContentId);
  assert.equal(row.comments_disabled, true); assert.equal(row.comment_count, '0');
  assert.deepEqual(row.comments_first_page, input.page);
  await apply({ ...input, observedAt: t2, page: undefined,
    comment: metric(null, { status: 'unresolved' }) });
  row = await stored(input.sourceContentId);
  assert.equal(row.comments_disabled, true); assert.equal(row.comment_count_status, 'disabled');
  await apply({ ...input, observedAt: t3, page: undefined, comment: metric('130', { disabled: false }) });
  row = await stored(input.sourceContentId);
  assert.equal(row.comments_disabled, false); assert.equal(row.comment_count, '130');
});

test('missing/empty pages fill; an older page cannot supersede a later page observation', async () => {
  const input = observation();
  await apply(input);
  await apply({ ...input, observedAt: t1, page: emptyPage(t1) });
  let result = await apply({ ...input, observedAt: t2, page: page(t0) });
  assert.equal(result.page, 'older');
  assert.equal((await stored(input.sourceContentId)).comments_first_page.returned_count, 0);
  await apply({ ...input, observedAt: t2, page: emptyPage(t2) });
  let row = await stored(input.sourceContentId);
  assert.equal(row.comments_first_page_observed_at.toISOString(), t1);
  assert.equal(row.comments_page_checked_at.toISOString(), t2);
  result = await apply({ ...input, observedAt: t3, page: page(t3) });
  row = await stored(input.sourceContentId);
  assert.equal(result.page, 'applied'); assert.equal(row.comments_first_page.returned_count, 1);
  assert.equal(row.comments_first_page_observed_at.toISOString(), t3);
  const missing = observation(); await apply(missing);
  await apply({ ...missing, observedAt: t1, page: page(t1) });
  assert.equal((await stored(missing.sourceContentId)).comments_first_page.returned_count, 1);
});

test('observation ordering is per metric and does not prevent a genuine decrease', async () => {
  const input = observation({ view: metric('100'), like: metric('50') });
  await apply(input);
  await apply({ ...input, observedAt: t2, view: metric('90'), like: undefined });
  const result = await apply({ ...input, observedAt: t1, view: metric('200'), like: metric('55') });
  const row = await stored(input.sourceContentId);
  assert.equal(result.view, 'older'); assert.equal(result.like, 'applied');
  assert.equal(row.view_count, '90'); assert.equal(row.like_count, '55');
  assert.equal(row.view_count_observed_at.toISOString(), t2);
  assert.equal(row.like_count_observed_at.toISOString(), t1);
  assert.equal(row.last_seen_at.toISOString(), t2);
});

test('equal-time equal-value replay is stable; conflicting equal-time data rolls back the entire batch', async () => {
  const input = observation({ view: metric('10'), like: metric('20') });
  const first = await apply(input);
  const replay = await apply(input);
  assert.equal(first.contentKey, replay.contentKey); assert.equal(replay.view, 'same');
  await apply({ ...input, observedAt: t1, view: undefined, like: metric('21') });
  await assert.rejects(apply({ ...input, observedAt: t1, view: metric('11'), like: metric('99') }), errorCode('OBSERVATION_TIME_CONFLICT'));
  let row = await stored(input.sourceContentId);
  assert.equal(row.view_count, '10'); assert.equal(row.like_count, '21');
  const another = observation({ view: metric('88') });
  await assert.rejects(withContentFactsTransaction(pool, async tx => {
    await tx.mergeContent(another);
    try { await tx.mergeContent({ ...input, observedAt: t1, like: metric('99') }); } catch { /* swallowed by caller */ }
  }), errorCode('OBSERVATION_TIME_CONFLICT'));
  row = await stored(another.sourceContentId); assert.equal(row, undefined);
});

test('same-time empty/nonempty page conflict is explicit and future page timestamps are rejected', async () => {
  const input = observation({ page: emptyPage(t0) });
  await apply(input);
  await assert.rejects(apply({ ...input, page: page(t0) }), errorCode('OBSERVATION_TIME_CONFLICT'));
  await assert.rejects(apply({ ...input, page: page(t1) }), errorCode('PAGE_FROM_FUTURE'));
  assert.equal((await stored(input.sourceContentId)).comments_first_page.returned_count, 0);
});

test('wrong-channel writes and unimplemented type corrections cannot change facts', async () => {
  const input = observation({ view: metric('10') }); await apply(input);
  await assert.rejects(apply({ ...input, channelId: otherChannel, view: metric('20') }), errorCode('CHANNEL_IDENTITY_CONFLICT'));
  await assert.rejects(apply({ ...input, contentType: 'short' }), errorCode('CONTENT_TYPE_CORRECTION_REQUIRED'));
  const row = await stored(input.sourceContentId);
  assert.equal(row.channel_id, channel); assert.equal(row.view_count, '10');
});

test('invalid integers, quality, dates, unknown fields and pages fail before creating content', async () => {
  const invalid = [
    { view: metric(9007199254740992) }, { view: metric('9223372036854775808') },
    { view: metric('-1') }, { like: metric('2', { status: 'zero_from_empty' }) },
    { comment: metric('0', { status: 'disabled' }) }, { comment: metric(null, { status: 'unavailable', disabled: true }) },
    { observedAt: '2026-02-31T00:00:00.000Z' }, { unexpected: true },
    { page: { ...page(t0), returned_count: 2 } },
    { page: { ...page(t0), comments: [null] } },
    { page: { ...page(t0), total_count: 9007199254740992 } },
  ];
  for (const extra of invalid) {
    const input = observation(extra);
    await assert.rejects(apply(input));
    assert.equal(await stored(input.sourceContentId), undefined);
  }
});

test('facts share the caller transaction and rollback when a subsequent statement fails', async () => {
  const input = observation({ view: metric('100') });
  await assert.rejects(withContentFactsTransaction(pool, async tx => {
    await tx.mergeContent(input);
    await tx.query('SELECT 1/0');
  }), errorCode('22012'));
  assert.equal(await stored(input.sourceContentId), undefined);
  // Even a swallowed SQL error must not report a successful COMMIT of an aborted transaction.
  await assert.rejects(withContentFactsTransaction(pool, async tx => {
    await tx.mergeContent(input);
    try { await tx.query('SELECT 1/0'); } catch {}
  }), errorCode('22012'));
  assert.equal(await stored(input.sourceContentId), undefined);
});

test('concurrent initial writes reuse one identity and newest metric wins', async () => {
  const input = observation({ comment: metric('100'), page: page(t0) });
  const results = await Promise.all([
    apply(input), apply({ ...input, observedAt: t1, comment: metric('120'), page: page(t1, 'second') }),
  ]);
  assert.equal(results[0].contentKey, results[1].contentKey);
  const row = await stored(input.sourceContentId);
  assert.equal(row.comment_count, '120');
  assert.equal(row.comment_count_observed_at.toISOString(), t1);
  assert.ok(['original', 'second'].includes(row.comments_first_page.comments[0].text));
  // The first committed nonempty page is retained; it is not promised to be the earliest capture.
  assert.equal((await pool.query('SELECT count(*)::int AS n FROM crawler.contents WHERE source_content_id=$1', [input.sourceContentId])).rows[0].n, 1);
});

test('retained page metadata cannot be overwritten independently and closed transactions reject reuse', async () => {
  const input = observation({ page: page(t0) }); await apply(input);
  const before = await stored(input.sourceContentId);
  await pool.query(`UPDATE crawler.contents SET comments_first_page_hash=$2, comments_first_page_hash_profile='wrong',
    comments_first_page_observed_at=$3 WHERE source_content_id=$1`, [input.sourceContentId, '0'.repeat(64), t3]);
  const after = await stored(input.sourceContentId);
  assert.equal(after.comments_first_page_hash, before.comments_first_page_hash);
  assert.equal(after.comments_first_page_observed_at.toISOString(), t0);
  let saved;
  await withContentFactsTransaction(pool, async tx => { saved = tx; });
  await assert.rejects(saved.mergeContent(input), errorCode('TRANSACTION_CLOSED'));
});
