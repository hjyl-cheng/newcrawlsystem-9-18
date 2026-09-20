import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const read = name => JSON.parse(readFileSync(new URL(`../contracts/data-plane/${name}`, import.meta.url), 'utf8'));
const schema = read('v1-draft.schema.json');
const ajv = new Ajv2020({ strict: true, allErrors: true, coerceTypes: false, removeAdditional: false, useDefaults: false });
addFormats(ajv);
const validate = ajv.compile(schema);
const fixture = name => read(`fixtures/${name}.json`);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

for (const name of ['submission-stream', 'submission-object', 'receipt-query', 'receipt-not-observed', 'receipt-received', 'receipt-applied', 'problem-commit-unknown', 'frame']) {
  test(`contract fixture: ${name}`, () => {
    const message = fixture(name);
    const before = structuredClone(message);
    assert.equal(validate(message), true, JSON.stringify(validate.errors));
    assert.deepEqual(message, before, 'Validation must not rewrite confirmed messages');
  });
}

test('invalid or future submissions fail closed without normalization', () => {
  const cases = [
    ['unknown version', m => { m.contract_version = 'data-plane/2'; }],
    ['missing submission identity', m => { delete m.identity.submission_id; }],
    ['empty identity', m => { m.identity.logical_batch_key = ''; }],
    ['number epoch', m => { m.execution.execution_epoch = 17; }],
    ['noncanonical epoch', m => { m.execution.execution_epoch = '017'; }],
    ['negative epoch', m => { m.execution.execution_epoch = '-1'; }],
    ['lossy revision', m => { m.frozen_input.input_revision = 9007199254740993; }],
    ['missing frozen target', m => { delete m.frozen_input.target_hash; }],
    ['unknown fields', m => { m.success = true; }],
    ['unknown nested fields', m => { m.identity.hostname = 'a1'; }],
    ['ambiguous success flag', m => { m.payload.applied = true; }],
    ['invalid hash', m => { m.payload.content_sha256 = 'an-etag'; }],
    ['unsupported hash profile', m => { m.payload.hash_profile = 'sort-and-hope'; }],
    ['negative byte count', m => { m.payload.wire_bytes = -1; }],
    ['unsafe integer size', m => { m.payload.wire_bytes = Number.MAX_SAFE_INTEGER + 1; }],
    ['two payload locations', m => { m.payload.location.object_key = 'extra'; }],
    ['zero frames', m => { m.payload.location.frame_count = 0; }],
    ['old role spelling', m => { m.producer_role = 'increment'; }],
    ['discover needs its own context', m => { m.producer_role = 'discover'; }],
    ['API needs its request context', m => { m.producer_role = 'data-api'; }],
    ['timezone absent', m => { m.created_at = '2026-09-20T08:00:00.000'; }],
    ['invalid calendar day', m => { m.created_at = '2026-02-30T08:00:00.000Z'; }],
    ['null is not missing', m => { m.frozen_input = null; }],
  ];
  for (const [name, change] of cases) {
    const message = fixture('submission-stream');
    change(message);
    const before = structuredClone(message);
    assert.equal(validate(message), false, name);
    assert.deepEqual(message, before, name);
  }
});

test('a submission receipt cannot claim plan completion or business delivery', () => {
  for (const status of ['SUCCESS', 'DELIVERED', 'PLAN_SETTLED', 'NOT_FOUND', 'REJECTED']) {
    const message = fixture('receipt-applied');
    message.status = status;
    assert.equal(validate(message), false, status);
  }
  const missing = fixture('receipt-applied');
  delete missing.content_sha256;
  assert.equal(validate(missing), false, 'Receipt must identify the confirmed content');
});

test('problems are separate from durable receipts and retain the defined reason code', () => {
  const problem = fixture('problem-commit-unknown');
  assert.equal(validate(problem), true);
  problem.status = 'APPLIED';
  assert.equal(validate(problem), false);
  delete problem.status;
  problem.code = 'DB.TIMEOUT_MEANS_NOT_COMMITTED';
  assert.equal(validate(problem), false);
  // Malformed input may have no trustworthy identity; do not invent one for the response.
  problem.code = 'CONTRACT.INVALID';
  delete problem.identity;
  assert.equal(validate(problem), true);
});

test('receipt lookup preserves stable identities without requiring a new execution epoch', () => {
  const lookup = fixture('receipt-query');
  assert.equal(validate(lookup), true);
  assert.deepEqual(lookup.identity, fixture('submission-stream').identity);
  lookup.execution_epoch = '18';
  assert.equal(validate(lookup), false, 'Changing write authorization is not a receipt lookup');
});

test('a missing observation cannot masquerade as a negative durable receipt', () => {
  const message = fixture('receipt-not-observed');
  assert.equal(validate(message), true);
  message.status = 'NOT_COMMITTED';
  assert.equal(validate(message), false);
});

test('reason code subset exists verbatim in the 24.4 catalog', () => {
  const architecture = readFileSync(new URL('../24.4_爬虫平台完整重构方案_业务控制台与全链路追踪定案版.md', import.meta.url), 'utf8');
  const catalog = architecture.split('### 10.2 ')[1].split('### 10.3 ')[0];
  for (const code of schema.$defs.problem.properties.code.enum) {
    assert.ok(catalog.includes(`| \`${code}\` |`), `Unknown architecture reason: ${code}`);
  }
});

test('golden payload binds business identity and frozen inputs; byte size and hashes agree', () => {
  const bytes = readFileSync(new URL('../contracts/data-plane/fixtures/submission-content.jcs', import.meta.url));
  const content = JSON.parse(bytes);
  const manifest = fixture('submission-stream');
  assert.deepEqual(content.identity, manifest.identity);
  assert.deepEqual(content.frozen_input, manifest.frozen_input);
  assert.equal(content.producer_role, manifest.producer_role);
  assert.equal(content.mode, manifest.mode);
  assert.equal(bytes.length, manifest.payload.uncompressed_bytes);
  assert.equal(bytes.length, manifest.payload.wire_bytes);
  assert.equal(hash(bytes), manifest.payload.content_sha256);
  assert.equal(hash(bytes), manifest.payload.wire_sha256);
  const frame = fixture('frame');
  assert.equal(frame.submission_id, manifest.identity.submission_id);
  assert.equal(frame.wire_bytes, bytes.length);
  assert.equal(frame.wire_sha256, hash(bytes));
  const changed = Buffer.from(bytes);
  changed[changed.length - 2] ^= 1;
  assert.notEqual(hash(changed), manifest.payload.content_sha256);
});

test('portable encoding vectors preserve large integers, Unicode, null and missing', () => {
  const vectors = fixture('encoding-vectors');
  for (const vector of vectors) {
    assert.equal(hash(Buffer.from(vector.canonical_utf8, 'utf8')), vector.sha256, vector.name);
    assert.equal(Buffer.byteLength(vector.canonical_utf8, 'utf8'), vector.utf8_bytes);
  }
  assert.equal(JSON.parse(vectors.find(v => v.name === 'large-integer').canonical_utf8).sequence, '9007199254740993');
  const byName = Object.fromEntries(vectors.map(v => [v.name, v.sha256]));
  assert.notEqual(byName.missing, byName.null);
  assert.notEqual(byName['unicode-composed'], byName['unicode-decomposed']);
});
