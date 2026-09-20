import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { contentHash, targetHash } from '../dist/services/ingestion/src/metrics-submission.js';
const ajv = new Ajv2020({strict:true,allErrors:true});addFormats(ajv);
const read = path => JSON.parse(readFileSync(new URL(path,import.meta.url)));
const validate = ajv.compile(read('../contracts/data-plane/content-metrics-batch.v1-draft.schema.json'));
const fixture = read('../contracts/data-plane/fixtures/content-metrics-batch.json');
test('metrics payload fixture carries explicit identity, frozen targets and lossless counters',()=>{
  assert.equal(validate(fixture),true,JSON.stringify(validate.errors));
  assert.equal(targetHash(fixture.observations.map(o=>({source_content_id:o.sourceContentId,content_type:o.contentType}))),fixture.frozen_input.target_hash);
  assert.match(contentHash(fixture),/^[a-f0-9]{64}$/);
});
test('metrics payload schema rejects unsupported scope, ambiguous counts and invalid comment metadata',()=>{
  for(const change of [
    p=>p.producer_role='fullcrawl',p=>p.mode='repair',p=>p.observations=[],
    p=>p.observations=Array(101).fill(p.observations[0]),
    p=>p.frozen_input.input_revision=10000000000000000000,
    p=>p.observations[0].comment.value=120,
    p=>p.observations[0].comment.status='unavailable',
    p=>p.observations[0].comment.disabled=true,
    p=>p.observations[0].page.returned_count='1',
    p=>p.observations[0].page.comments=[null],
    p=>p.observations[0].observedAt='2026-02-31T01:00:00.000Z',
    p=>p.observations[0].title='not-yet-supported',
  ]) {const p=structuredClone(fixture);change(p);assert.equal(validate(p),false);}
});
