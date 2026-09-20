import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { generateKeyPairSync } from 'node:crypto';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { parseYoutubeEvidenceJson, mapYoutubeMetrics, buildMetricsSubmission } from '../dist/services/collection/src/youtube-metrics.js';
import { targetHash, contentHash, prepareMetricsApply } from '../dist/services/ingestion/src/metrics-submission.js';
import { encodeResult, decodeResult } from '../dist/services/kafka-results/src/transport.js';
import { target, metricsEvidence, mainContents, likeButton, commentsSection } from './helpers/youtube-metrics-fixture.mjs';

function work() {
  const targets = [{ source_content_id: target.sourceContentId, content_type: target.contentType }];
  return {
    identity: { plan_id: 'plan:metrics-map', channel_id: target.channelId, logical_batch_key: 'batch:metrics-map' },
    frozen_input: { generation: '1', input_revision: '1', input_hash: contentHash({ sourceChannelId: target.sourceChannelId }),
      target_hash: targetHash(targets), policy_version: 'metrics-exact-v1' },
    targets, scope: 'scope:metrics-map', execution_epoch: '1',
  };
}

test('raw exact counts preserve bigint precision and map only metrics with explicit identity', () => {
  const evidence = metricsEvidence();
  const result = mapYoutubeMetrics(target, evidence);
  assert.equal(result.complete, true);
  assert.equal(result.observation.view.value, '9007199254740993');
  assert.equal(result.observation.like.value, '1234');
  assert.equal(result.observation.comment.value, '5678');
  assert.equal(result.observation.channelId, target.channelId);
  assert.equal(result.observation.comment.disabled, false);
  assert.equal(Object.hasOwn(result.observation, 'page'), false);
  evidence.player.videoDetails.viewCount = '99';
  assert.equal(result.observation.view.value, '9007199254740993');
});

test('numeric JSON tokens above Number safe range retain original digits; pre-rounded Numbers are rejected', () => {
  const evidence = metricsEvidence();
  const raw = JSON.stringify(evidence.player).replace('"9007199254740993"', '9007199254740993');
  evidence.player = parseYoutubeEvidenceJson(raw);
  assert.equal(mapYoutubeMetrics(target, evidence).observation.view.value, '9007199254740993');
  evidence.player.videoDetails.viewCount = Number('9007199254740993');
  assert.equal(mapYoutubeMetrics(target, evidence).observation.view.value, null);
  assert.throws(() => parseYoutubeEvidenceJson('{broken'), { code: 'YOUTUBE.JSON_INVALID' });
});

test('wrong request, video, source channel, next-video identity and invalid observation time are rejected', () => {
  for (const [mutate, code] of [
    [e => { e.requestedVideoId = 'zzzzzzzzzzz'; }, 'YOUTUBE.REQUEST_IDENTITY_MISMATCH'],
    [e => { e.player.videoDetails.videoId = 'zzzzzzzzzzz'; }, 'YOUTUBE.IDENTITY_MISMATCH'],
    [e => { e.player.videoDetails.channelId = 'UC' + 'b'.repeat(22); }, 'YOUTUBE.CHANNEL_MISMATCH'],
    [e => { e.next.currentVideoEndpoint.watchEndpoint.videoId = 'zzzzzzzzzzz'; }, 'YOUTUBE.NEXT_IDENTITY_MISMATCH'],
    [e => { e.observedAt = '2026-02-31T01:00:00.000Z'; }, 'INVALID_OBSERVATION'],
    [e => { e.player.playabilityStatus.status = 'LOGIN_REQUIRED'; }, 'YOUTUBE.PLAYABILITY_UNSUPPORTED'],
  ]) {
    const evidence = metricsEvidence(); mutate(evidence);
    assert.throws(() => mapYoutubeMetrics(target, evidence), { code });
  }
});

test('absent, abbreviated, negative, hidden and overflowing counts never turn into zero or exact guesses', () => {
  for (const value of [undefined, null, '', '1.2K', '-1', '1,2', 'Like', '9223372036854775808']) {
    const evidence = metricsEvidence();
    evidence.player.videoDetails.viewCount = value;
    likeButton(evidence).defaultText = { simpleText: value };
    commentsSection(evidence).contents[0].commentsEntryPointHeaderRenderer.commentCount = { simpleText: value };
    const result = mapYoutubeMetrics(target, evidence);
    assert.equal(result.complete, false);
    for (const field of ['view', 'like', 'comment']) {
      assert.equal(result.observation[field].value, null);
      assert.equal(result.observation[field].status, 'unresolved');
    }
    assert.equal(result.observation.comment.disabled, null);
  }
});

test('explicit zero and PostgreSQL bigint maximum are retained as exact decimal strings', () => {
  for (const value of ['0', '9223372036854775807']) {
    const evidence = metricsEvidence();
    evidence.player.videoDetails.viewCount = value;
    likeButton(evidence).defaultText = { simpleText: value };
    commentsSection(evidence).contents[0].commentsEntryPointHeaderRenderer.commentCount = { simpleText: value };
    const result = mapYoutubeMetrics(target, evidence);
    assert.equal(result.complete, true);
    for (const field of ['view', 'like', 'comment']) assert.equal(result.observation[field].value, value);
    assert.equal(result.observation.comment.status, 'exact');
  }
});

test('only an explicit disabled comment message produces disabled zero; contradictory evidence stays unknown', () => {
  const evidence = metricsEvidence();
  commentsSection(evidence).contents = [{ messageRenderer: { text: { simpleText: 'Comments are turned off.' } } }];
  const result = mapYoutubeMetrics(target, evidence);
  assert.equal(result.complete, true);
  assert.equal(result.observation.comment.value, '0');
  assert.equal(result.observation.comment.disabled, true);
  assert.equal(result.observation.comment.status, 'disabled');
  commentsSection(evidence).contents.push({ commentsEntryPointHeaderRenderer: { commentCount: { simpleText: '5' } } });
  assert.equal(mapYoutubeMetrics(target, evidence).observation.comment.value, null);
  commentsSection(evidence).contents = [];
  assert.equal(mapYoutubeMetrics(target, evidence).observation.comment.value, null);
});

test('recommendation metrics and comment likes are ignored; conflicting exact evidence is rejected', () => {
  const evidence = metricsEvidence();
  evidence.next.contents.twoColumnWatchNextResults.secondaryResults.secondaryResults.results = [
    { videoDetails: { viewCount: '999' }, commentsEntryPointHeaderRenderer: { commentCount: { simpleText: '999' } } },
  ];
  commentsSection(evidence).contents.push({ commentThreadRenderer: { comment: { likeCount: '999' } } });
  assert.equal(mapYoutubeMetrics(target, evidence).observation.comment.value, '5678');
  likeButton(evidence).defaultText.simpleText = '1,235';
  const result = mapYoutubeMetrics(target, evidence);
  assert.equal(result.observation.like.value, null);
  assert.deepEqual(result.issues, [{ field: 'like', reason: 'conflicting' }]);
});

test('modern like view-model exact accessibility count is read without rounding', () => {
  const evidence = metricsEvidence();
  mainContents(evidence)[0].videoPrimaryInfoRenderer.videoActions.menuRenderer.topLevelButtons = [{
    segmentedLikeDislikeButtonViewModel: { likeButtonViewModel: { likeButtonViewModel: {
      toggleButtonViewModel: { toggleButtonViewModel: { defaultButtonViewModel: { buttonViewModel: {
        title: '9B', accessibilityText: 'like this video along with 9,007,199,254,740,993 other people',
      } } } },
    } } },
  }];
  assert.equal(mapYoutubeMetrics(target, evidence).observation.like.value, '9007199254740993');
});

test('complete mapped batch passes existing schema, Apply prevalidation and signed Kafka codec', () => {
  const plan = work();
  const result = mapYoutubeMetrics(target, metricsEvidence());
  const submission = buildMetricsSubmission(plan, [result], 'submission:metrics-map');
  const ajv = new Ajv2020({ strict: true }); addFormats(ajv);
  const schema = JSON.parse(readFileSync(new URL('../contracts/data-plane/content-metrics-batch.v1-draft.schema.json', import.meta.url)));
  const validate = ajv.compile(schema);
  assert.equal(validate(submission.payload), true, JSON.stringify(validate.errors));
  const principal = { holderId: 'worker:map-test', channelId: target.channelId };
  assert.equal(typeof prepareMetricsApply(submission, principal), 'function'); // No PG connection or authorization claim.
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const message = encodeResult(submission, { keyId: 'key:map-test', privateKey, principal });
  const decoded = decodeResult(message, new Map([['key:map-test', { publicKey, holderId: principal.holderId,
    allowedChannels: new Set([principal.channelId]) }]]));
  assert.deepEqual(decoded.request, submission);
  assert.equal(decoded.request.payload.observations[0].view.value, '9007199254740993');
  plan.identity.plan_id = 'mutated'; result.observation.view.value = '1';
  assert.equal(submission.payload.identity.plan_id, 'plan:metrics-map');
  assert.equal(submission.payload.observations[0].view.value, '9007199254740993');
});

test('batch assembly rejects incomplete metrics, wrong frozen targets, duplicates, channel mixing and comment pages', () => {
  const result = mapYoutubeMetrics(target, metricsEvidence());
  for (const mutate of [
    r => { r.observation.like.value = null; r.observation.like.status = 'unresolved'; },
    r => { r.observation.comment.status = 'zero_from_empty'; r.observation.comment.value = '0'; },
    r => { r.observation.channelId = 'other'; },
    r => { r.observation.page = null; },
    r => { r.observation.sourceContentId = 'zzzzzzzzzzz'; },
    r => { r.observation.contentType = 'short'; },
  ]) {
    const changed = structuredClone(result); mutate(changed);
    assert.throws(() => buildMetricsSubmission(work(), [changed], 'submission:test'));
  }
  assert.throws(() => buildMetricsSubmission(work(), [], 'submission:test'), { code: 'COLLECTION.BATCH_INCOMPLETE' });
  const bad = work(); bad.frozen_input.target_hash = '0'.repeat(64);
  assert.throws(() => buildMetricsSubmission(bad, [result], 'submission:test'), { code: 'DATA.VERSION_CONFLICT' });
  const two = work(); two.targets.push({ source_content_id: 'zzzzzzzzzzz', content_type: 'video' });
  two.frozen_input.target_hash = targetHash(two.targets);
  assert.throws(() => buildMetricsSubmission(two, [result, result], 'submission:test'));
});
