import { createHash } from 'node:crypto';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import pg from 'pg';

export const migrationsDirectory = fileURLToPath(new URL('../../database/migrations/', import.meta.url));

export function databaseConfig(env = process.env) {
  for (const key of ['PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'DB_EXPECTED_NAME']) {
    if (!env[key]) throw new Error(`Required environment variable: ${key}`);
  }
  if (env.PGDATABASE !== env.DB_EXPECTED_NAME) throw new Error('Database identity confirmation differs');
  return {
    host: env.PGHOST, port: Number(env.PGPORT), database: env.PGDATABASE,
    user: env.PGUSER, password: env.PGPASSWORD, connectionTimeoutMillis: 10000,
    application_name: 'newcrawlsystem-migration',
  };
}

export async function migrate(client, expectedDatabase, directory = migrationsDirectory) {
  const { rows: [server] } = await client.query(
    "SELECT current_database() AS name, pg_is_in_recovery() AS standby, current_setting('server_version_num')::int AS version",
  );
  if (!expectedDatabase || server.name !== expectedDatabase) throw new Error('Database identity confirmation differs');
  if (server.standby || server.version < 170000) throw new Error('Migration requires PostgreSQL 17+ primary');
  const files = (await readdir(directory)).filter(name => name.endsWith('.sql')).sort();
  if (!files.length || files.some(name => !/^\d{4}_[a-z0-9_]+\.sql$/.test(name))) throw new Error('Invalid migration filenames');
  const migrations = await Promise.all(files.map(async name => {
    const sql = await readFile(resolve(directory, name), 'utf8');
    return { name, version: name.slice(0, 4), sql, checksum: createHash('sha256').update(sql).digest('hex') };
  }));
  if (new Set(migrations.map(m => m.version)).size !== migrations.length) throw new Error('Duplicate migration version');
  // Dedicated direct connection: this lock must survive individual migration transactions.
  await client.query("SET lock_timeout = '10s'");
  await client.query("SET statement_timeout = '60s'");
  await client.query('SELECT pg_advisory_lock(243, 1)');
  try {
    await client.query('BEGIN');
    await client.query(`
      CREATE SCHEMA IF NOT EXISTS platform;
      CREATE TABLE IF NOT EXISTS platform.schema_migrations (
        version text PRIMARY KEY CHECK (version ~ '^[0-9]{4}$'),
        name text NOT NULL UNIQUE,
        checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
        applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
      )`);
    const applied = (await client.query('SELECT version, name, checksum FROM platform.schema_migrations ORDER BY version')).rows;
    for (let i = 0; i < applied.length; i++) {
      const actual = applied[i], source = migrations[i];
      if (!source || actual.version !== source.version || actual.name !== source.name || actual.checksum !== source.checksum) {
        throw new Error('Applied migration history differs from local files; add a new migration instead');
      }
    }
    await client.query('COMMIT');
    const added = [];
    for (const migration of migrations.slice(applied.length)) {
      await client.query('BEGIN');
      await client.query(migration.sql);
      await client.query('INSERT INTO platform.schema_migrations(version, name, checksum) VALUES ($1, $2, $3)',
        [migration.version, migration.name, migration.checksum]);
      await client.query('COMMIT');
      added.push(migration.name);
    }
    return { database: server.name, applied: added, currentVersion: migrations.at(-1).version };
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    await client.query('SELECT pg_advisory_unlock(243, 1)');
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let client;
  try {
    client = new pg.Client(databaseConfig());
    await client.connect();
    console.log(JSON.stringify(await migrate(client, process.env.DB_EXPECTED_NAME)));
  } catch (error) {
    console.error(`Migration failed (${error.code ?? 'validation'}). Check database identity, connectivity and migration history.`);
    process.exitCode = 1;
  } finally {
    if (client) await client.end();
  }
}
