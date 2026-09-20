import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, copyFile, writeFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import pg from 'pg';
import { databaseConfig, migrate, migrationsDirectory } from '../../ops/db/migrate.mjs';

const config = databaseConfig();
// Integration tests must never run against crawler, postgres, Business or a merely renamed production DB.
assert.match(config.database, /^crawler_schema_test_[a-z0-9_]+$/);
assert.equal(process.env.DB_ALLOW_TEST_WRITES, config.database);
const client = new pg.Client(config);
const migrationFile = '0001_collection_facts.sql';
const migrationFiles = (await readdir(migrationsDirectory)).filter(f => f.endsWith('.sql')).sort();
let applied;
let upgradeEvidence;
before(async () => {
  await client.connect();
  const fresh = (await client.query("SELECT to_regclass('platform.schema_migrations') AS ledger")).rows[0].ledger === null;
  if (fresh) {
    const baseline = await mkdtemp(join(tmpdir(), 'crawl-upgrade-'));
    try {
      await copyFile(join(migrationsDirectory, migrationFile), join(baseline, migrationFile));
      await migrate(client, config.database, baseline);
      await client.query("INSERT INTO crawler.channels(channel_id,channel_url) VALUES ('migration-upgrade-probe','https://example.test/upgrade')");
      await client.query(`INSERT INTO crawler.contents(channel_id,resource_kind,source_content_id,content_type,comments_first_page)
        VALUES ('migration-upgrade-probe','youtube_video','migration-upgrade-probe','video',$1::jsonb)`, [JSON.stringify(page)]);
    } finally { await rm(baseline, { recursive: true, force: true }); }
  }
  applied = await migrate(client, config.database);
  if (fresh) {
    upgradeEvidence = (await client.query("SELECT * FROM crawler.contents WHERE source_content_id='migration-upgrade-probe'")).rows[0];
    await client.query("DELETE FROM crawler.contents WHERE source_content_id='migration-upgrade-probe'");
    await client.query("DELETE FROM crawler.channels WHERE channel_id='migration-upgrade-probe'");
  }
});
after(async () => { await client.end(); });

async function isolated(fn) {
  await client.query('BEGIN');
  try {
    await client.query("INSERT INTO crawler.channels(channel_id, channel_url) VALUES ('channel-a','https://example.test/a'),('channel-b','https://example.test/b')");
    await fn();
  } finally { await client.query('ROLLBACK'); }
}
async function fails(sql, values = [], code = '23514') {
  await client.query('SAVEPOINT invalid_input');
  try { await assert.rejects(client.query(sql, values), error => error.code === code); }
  finally {
    await client.query('ROLLBACK TO SAVEPOINT invalid_input');
    await client.query('RELEASE SAVEPOINT invalid_input');
  }
}
async function video(source = 'source-1') {
  return (await client.query(`INSERT INTO crawler.contents(channel_id, resource_kind, source_content_id, content_type)
    VALUES ('channel-a', 'youtube_video', $1, 'video') RETURNING *`, [source])).rows[0];
}
const page = {
  version: 1, sort: 'TOP_COMMENTS', collected_at: '2026-09-20T00:00:00.000Z',
  returned_count: 1, total_count: 10, comments: [{ id: 'comment-1', text: 'historical first page' }],
};

test('upgrade of populated 0001 preserves page and derives evidence in 0002', t => {
  if (!upgradeEvidence) return t.skip('Existing database already migrated; fresh CI database exercises upgrade');
  assert.deepEqual(upgradeEvidence.comments_first_page, page);
  assert.match(upgradeEvidence.comments_first_page_hash, /^[a-f0-9]{64}$/);
  assert.equal(upgradeEvidence.comments_first_page_observed_at.toISOString(), page.collected_at);
  assert.equal(upgradeEvidence.comments_page_checked_at.toISOString(), page.collected_at);
});

test('migration ledger, idempotency and database identity guard', async () => {
  assert.equal(applied.currentVersion, migrationFiles.at(-1).slice(0, 4));
  assert.deepEqual((await migrate(client, config.database)).applied, []);
  await assert.rejects(migrate(client, 'crawler'), /identity/);
  const { rows: [row] } = await client.query('SELECT * FROM platform.schema_migrations ORDER BY version');
  assert.equal(row.name, migrationFile);
  assert.match(row.checksum, /^[a-f0-9]{64}$/);
});

test('changed history is rejected; failed migration leaves no DDL or ledger row', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'crawl-migrations-'));
  try {
    for (const file of migrationFiles) await copyFile(join(migrationsDirectory, file), join(directory, file));
    await writeFile(join(directory, migrationFile), '\n-- altered\n', { flag: 'a' });
    await assert.rejects(migrate(client, config.database, directory), /history differs/);
    await copyFile(join(migrationsDirectory, migrationFile), join(directory, migrationFile));
    await writeFile(join(directory, '9999_failure_probe.sql'), 'CREATE TABLE crawler.failure_probe(id int); SELECT 1/0;');
    await assert.rejects(migrate(client, config.database, directory), error => error.code === '22012');
    assert.equal((await client.query("SELECT to_regclass('crawler.failure_probe') AS name")).rows[0].name, null);
    assert.equal((await client.query('SELECT count(*)::int AS count FROM platform.schema_migrations')).rows[0].count, migrationFiles.length);
    assert.deepEqual((await migrate(client, config.database)).applied, []);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('separate migration connections share one advisory lock', async () => {
  const second = new pg.Client(config);
  await second.connect();
  try {
    const results = await Promise.all([migrate(client, config.database), migrate(second, config.database)]);
    assert.deepEqual(results.map(r => r.applied), [[], []]);
    assert.equal((await client.query("SELECT count(*)::int AS count FROM pg_locks WHERE locktype = 'advisory' AND pid IN (pg_backend_pid(), $1)", [second.processID])).rows[0].count, 0);
  } finally { await second.end(); }
});

test('unknown values remain NULL; numeric observations require matching quality', async () => isolated(async () => {
  const row = await video();
  assert.equal(row.view_count, null);
  assert.equal(row.comment_count, null);
  assert.equal(row.comments_disabled, null);
  assert.equal(row.is_members_only, null);
  await fails("UPDATE crawler.contents SET view_count_status = 'exact'");
  await fails('UPDATE crawler.contents SET view_count = 0');
  await fails("UPDATE crawler.contents SET view_count = -1, view_count_status = 'exact'");
  await fails("UPDATE crawler.contents SET like_count = 1, like_count_status = 'zero_from_empty'");
  await client.query("UPDATE crawler.contents SET view_count = $1, view_count_status = 'exact'", ['9223372036854775807']);
  assert.equal((await client.query('SELECT view_count FROM crawler.contents')).rows[0].view_count, '9223372036854775807');
  await fails("UPDATE crawler.channels SET subscriber_count_status = 'exact'");
  await fails("UPDATE crawler.channels SET subscriber_count = -1, subscriber_count_status = 'exact'");
}));

test('video/short/live corrections preserve identity; wrong channel duplicates are rejected', async () => isolated(async () => {
  const row = await video();
  await client.query("UPDATE crawler.contents SET content_type = 'short'");
  assert.equal((await client.query('SELECT content_key FROM crawler.contents')).rows[0].content_key, row.content_key);
  await client.query("UPDATE crawler.contents SET content_type = 'live'");
  await fails(`INSERT INTO crawler.contents(channel_id, resource_kind, source_content_id, content_type)
    VALUES ('channel-b','youtube_video','source-1','video')`, [], '23505');
  await fails("UPDATE crawler.contents SET channel_id = 'channel-b'");
  await fails("UPDATE crawler.contents SET source_content_id = 'different'");
  await fails("UPDATE crawler.contents SET content_key = 'replacement'");
  await fails("UPDATE crawler.contents SET resource_kind = 'youtube_post', content_type = 'post'");
  await fails(`INSERT INTO crawler.contents(channel_id, resource_kind, source_content_id, content_type)
    VALUES ('missing-channel','youtube_video','source-2','video')`, [], '23503');
  await client.query(`INSERT INTO crawler.contents(channel_id, resource_kind, source_content_id, content_type)
    VALUES ('channel-a','youtube_post','source-1','post')`);
  assert.equal((await client.query('SELECT count(*)::int AS count FROM crawler.contents')).rows[0].count, 2);
  await fails("DELETE FROM crawler.channels WHERE channel_id = 'channel-a'", [], '23503');
}));

test('comment page rejects missing, NULL and malformed fields without unsafe casts', async () => isolated(async () => {
  await video();
  const invalidPages = [null, {}, [], { ...page, version: '1' }, { ...page, sort: null },
    { ...page, comments: {} }, { ...page, comments: [null] }, { ...page, returned_count: 2 },
    { ...page, returned_count: '1' }, { ...page, returned_count: -1 },
    { ...page, returned_count: 1e100 }, { ...page, collected_at: 'not-a-date' },
    { ...page, collected_at: '2026-02-31T00:00:00Z' },
    ...Object.keys(page).filter(k => k !== 'total_count').map(key => {
      const copy = { ...page }; delete copy[key]; return copy;
    })];
  for (const value of invalidPages) {
    await fails('UPDATE crawler.contents SET comments_first_page = $1::jsonb', [JSON.stringify(value)]);
  }
  await client.query('UPDATE crawler.contents SET comments_first_page = $1::jsonb', [JSON.stringify(page)]);
  await client.query('UPDATE crawler.contents SET comments_first_page = NULL');
  await client.query('UPDATE crawler.contents SET comments_first_page = $1::jsonb',
    [JSON.stringify({ ...page, returned_count: 0, comments: [] })]);
}));

test('current comment count and disabled state can change while historical page stays intact', async () => isolated(async () => {
  await video();
  await client.query(`UPDATE crawler.contents SET comments_first_page = $1::jsonb,
    comment_count = 10, comment_count_status = 'exact', comment_count_observed_at = '2026-09-20T00:00:00Z'`, [JSON.stringify(page)]);
  await client.query(`UPDATE crawler.contents SET comment_count = 99,
    comment_count_observed_at = '2026-09-20T01:00:00Z'`);
  let row = (await client.query('SELECT * FROM crawler.contents')).rows[0];
  assert.deepEqual(row.comments_first_page, page);
  assert.equal(row.comment_count, '99');
  await fails("UPDATE crawler.contents SET comments_disabled = true, comment_count = NULL, comment_count_status = 'disabled'");
  await fails("UPDATE crawler.contents SET comments_disabled = false, comment_count = 0, comment_count_status = 'disabled'");
  await client.query("UPDATE crawler.contents SET comments_disabled = true, comment_count = 0, comment_count_status = 'disabled'");
  row = (await client.query('SELECT * FROM crawler.contents')).rows[0];
  assert.deepEqual(row.comments_first_page, page);
  assert.equal(row.comment_count, '0');
  // This proves storage separation, not the future Store's incoming-page merge policy.
}));

test('channel verification, business email, dates and lifecycle require coherent evidence', async () => isolated(async () => {
  await fails("UPDATE crawler.channels SET is_verified_status = 'verified'");
  await fails('UPDATE crawler.channels SET youtube_business_email_available = false');
  await fails("UPDATE crawler.channels SET joined_at = '2020-01-01'");
  await fails("UPDATE crawler.channels SET lifecycle_status = 'removed'");
  await fails("UPDATE crawler.channels SET lifecycle_status = 'dormant', dormant_since = now(), dormant_recheck_day = current_date, dormant_last_probe_at = now(), dormant_cycle = 1");
  await client.query(`UPDATE crawler.channels SET is_verified = false, is_verified_status = 'not_verified',
    youtube_business_email_available = false, youtube_business_email_observed_at = now(),
    joined_at = '2020-01-01', joined_at_precision = 'date_only'`);
}));

test('access evidence, publication hash and publication time keep their distinct semantics', async () => isolated(async () => {
  await video();
  await fails("UPDATE crawler.contents SET access_status = 'members_only'");
  await client.query("UPDATE crawler.contents SET access_status = 'members_only', is_members_only = true");
  await fails("UPDATE crawler.contents SET access_status = 'public'");
  await client.query("UPDATE crawler.contents SET access_status = 'public', is_members_only = false");
  await fails("UPDATE crawler.contents SET publication_item_hash = $1", ['a'.repeat(64)]);
  await client.query('UPDATE crawler.contents SET publication_item_hash = $1', ['sha256:' + 'a'.repeat(64)]);
  await fails("UPDATE crawler.contents SET published_at_status = 'exact'");
  await client.query("UPDATE crawler.contents SET published_at = now(), published_at_status = 'exact', published_at_precision = 'second'");
}));

test('facts are transactional and comments use TOAST-compatible extended storage', async () => {
  await isolated(async () => {
    await video('rolled-back-source');
    const result = await client.query(`SELECT attstorage FROM pg_attribute
      WHERE attrelid = 'crawler.contents'::regclass AND attname = 'comments_first_page'`);
    assert.equal(result.rows[0].attstorage, 'x');
  });
  assert.equal((await client.query("SELECT count(*)::int AS count FROM crawler.contents WHERE source_content_id = 'rolled-back-source'")).rows[0].count, 0);
});
