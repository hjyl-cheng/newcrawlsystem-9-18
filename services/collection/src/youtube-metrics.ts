import { Innertube } from 'youtubei.js';
import { CollectionError, type CollectorRuntime } from '../../collector-runtime/src/index.js';
import { snapshotContentObservation, type ContentObservation, type Metric } from '../../facts-store/src/content-store.js';
import { contentHash, targetHash, canonicalJson, type PlanDefinition, type Submission } from '../../ingestion/src/metrics-submission.js';
import { classifyHttpResponse, runReadStep, type ReadStepOptions } from './read-step.js';

type ObjectValue = Record<string, unknown>;
type MetricName = 'view' | 'like' | 'comment';
export interface MetricsTarget {
  channelId: string;
  /** External YouTube channel ID; it need not equal the platform's channelId. */
  sourceChannelId: string;
  sourceContentId: string;
  contentType: ContentObservation['contentType'];
}
export interface YoutubeMetricsEvidence {
  requestedVideoId: string;
  observedAt: string;
  player: unknown;
  next: unknown;
}
export interface MappedYoutubeMetrics {
  observation: ContentObservation;
  complete: boolean;
  issues: { field: MetricName; reason: 'missing' | 'not_exact' | 'out_of_range' | 'conflicting' }[];
}
export type MetricsWork = Pick<PlanDefinition, 'identity' | 'frozen_input' | 'targets' | 'scope' | 'execution_epoch'>;

function object(value: unknown): ObjectValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as ObjectValue : {};
}
function at(value: unknown, ...keys: string[]): unknown { for (const key of keys) value = object(value)[key]; return value; }
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function check(condition: unknown, code: string): asserts condition { if (!condition) throw new CollectionError(code); }
function validId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$/.test(value);
}
function validateTarget(target: MetricsTarget): void {
  check(target && validId(target.channelId) && /^UC[A-Za-z0-9_-]{22}$/.test(target.sourceChannelId) &&
    /^[A-Za-z0-9_-]{11}$/.test(target.sourceContentId) && ['video', 'short', 'live'].includes(target.contentType),
  'COLLECTION.TARGET_INVALID');
}

/** Node 24 exposes the original numeric token to the reviver. No rounded Number is re-serialized as a count. */
export function parseYoutubeEvidenceJson(text: string): unknown {
  check(Buffer.byteLength(text, 'utf8') <= 4 * 1024 * 1024, 'YOUTUBE.RESPONSE_TOO_LARGE');
  try {
    return JSON.parse(text, (_key: string, value: unknown, context?: { source?: string }) => {
      if (typeof value === 'number' && !Number.isSafeInteger(value)) {
        if (context?.source && /^(0|[1-9][0-9]*)$/.test(context.source)) return context.source;
        // Preserve a non-integral/negative large token as text as well; count validation will reject it.
        return context?.source ?? null;
      }
      return value;
    });
  } catch { throw new CollectionError('YOUTUBE.JSON_INVALID'); }
}

function rendered(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  const simple = at(value, 'simpleText');
  if (typeof simple === 'string') return simple;
  const content = at(value, 'content');
  if (typeof content === 'string') return content;
  const runs = list(at(value, 'runs'));
  return runs.length && runs.every(run => typeof at(run, 'text') === 'string')
    ? runs.map(run => at(run, 'text')).join('') : undefined;
}

type CountResult = { value: string } | { reason: MappedYoutubeMetrics['issues'][number]['reason'] };
function exactCount(value: unknown): CountResult {
  if (value === null || value === undefined || value === '') return { reason: 'missing' };
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || value < 0) return { reason: 'not_exact' };
    value = String(value);
  }
  if (typeof value !== 'string') return { reason: 'not_exact' };
  const text = value.trim();
  // Locale is explicitly English. Do not strip arbitrary characters from "1.2K", "-1" or unrelated text.
  if (!/^(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)$/.test(text)) return { reason: 'not_exact' };
  const digits = text.replaceAll(',', '');
  if (digits.length > 19 || BigInt(digits) > 9223372036854775807n) return { reason: 'out_of_range' };
  return { value: digits };
}

function likeLabel(value: unknown): unknown {
  if (typeof value !== 'string') return undefined;
  const match = /^(?:like this video along with ([0-9,]+) other people|([0-9,]+) likes?)\.?$/i.exec(value.trim());
  return match?.[1] ?? match?.[2];
}

function metricFromCandidates(field: MetricName, candidates: unknown[], source: string,
  issues: MappedYoutubeMetrics['issues']): Metric {
  const parsed = candidates.map(exactCount);
  const values = [...new Set(parsed.flatMap(result => 'value' in result ? [result.value] : []))];
  if (parsed.some(result => 'reason' in result && result.reason === 'out_of_range')) {
    issues.push({ field, reason: 'out_of_range' });
    return { value: null, status: 'unresolved', source };
  }
  // An exact accessibility label may accompany an abbreviated display value. Exact evidence wins;
  // conflicting exact evidence never silently selects a preferred number.
  if (values.length === 1) return { value: values[0]!, status: 'exact', source };
  const reason = values.length > 1 ? 'conflicting' : parsed.find(result => 'reason' in result && result.reason !== 'missing');
  issues.push({ field, reason: typeof reason === 'string' ? reason : reason && 'reason' in reason ? reason.reason : 'missing' });
  return { value: null, status: 'unresolved', source };
}

/** Only the main video's own result surface is inspected. Recommendations/comment likes are excluded. */
export function mapYoutubeMetrics(target: MetricsTarget, evidence: YoutubeMetricsEvidence): MappedYoutubeMetrics {
  validateTarget(target);
  check(evidence.requestedVideoId === target.sourceContentId, 'YOUTUBE.REQUEST_IDENTITY_MISMATCH');
  const details = object(at(evidence.player, 'videoDetails'));
  check(details.videoId === target.sourceContentId, 'YOUTUBE.IDENTITY_MISMATCH');
  check(details.channelId === target.sourceChannelId, 'YOUTUBE.CHANNEL_MISMATCH');
  check(at(evidence.player, 'playabilityStatus', 'status') === 'OK', 'YOUTUBE.PLAYABILITY_UNSUPPORTED');
  check(at(evidence.next, 'currentVideoEndpoint', 'watchEndpoint', 'videoId') === target.sourceContentId,
    'YOUTUBE.NEXT_IDENTITY_MISMATCH');
  const contents = list(at(evidence.next, 'contents', 'twoColumnWatchNextResults', 'results', 'results', 'contents'));
  check(contents.length > 0, 'YOUTUBE.NEXT_SURFACE_UNSUPPORTED');
  const likes: unknown[] = [];
  const comments: unknown[] = [];
  let disabled = false;
  for (const item of contents) {
    const buttons = list(at(item, 'videoPrimaryInfoRenderer', 'videoActions', 'menuRenderer', 'topLevelButtons'));
    for (const button of buttons) {
      const legacy = at(button, 'segmentedLikeDislikeButtonRenderer', 'likeButton', 'toggleButtonRenderer') ??
        (at(button, 'toggleButtonRenderer', 'defaultIcon', 'iconType') === 'LIKE' ? at(button, 'toggleButtonRenderer') : undefined);
      if (legacy) {
        likes.push(rendered(at(legacy, 'defaultText')),
          likeLabel(at(legacy, 'defaultText', 'accessibility', 'accessibilityData', 'label')),
          likeLabel(at(legacy, 'accessibilityData', 'accessibilityData', 'label')),
          likeLabel(at(legacy, 'accessibility', 'label')));
      }
      const likeWrapper = at(button, 'segmentedLikeDislikeButtonViewModel', 'likeButtonViewModel');
      const likeData = at(likeWrapper, 'likeButtonViewModel') ?? likeWrapper;
      const toggleWrapper = at(likeData, 'toggleButtonViewModel');
      const toggleData = at(toggleWrapper, 'toggleButtonViewModel') ?? toggleWrapper;
      const defaultWrapper = at(toggleData, 'defaultButtonViewModel');
      const defaultButton = at(defaultWrapper, 'buttonViewModel') ?? defaultWrapper;
      likes.push(rendered(at(defaultButton, 'title')), likeLabel(at(defaultButton, 'accessibilityText')));
    }
    const section = at(item, 'itemSectionRenderer');
    if (!['comments-entry-point', 'comments-section'].includes(String(at(section, 'targetId'))) &&
        at(section, 'sectionIdentifier') !== 'comment-item-section') continue;
    for (const entry of list(at(section, 'contents'))) {
      comments.push(rendered(at(entry, 'commentsEntryPointHeaderRenderer', 'commentCount')));
      const message = rendered(at(entry, 'messageRenderer', 'text'));
      if (message === 'Comments are turned off.' || message === 'Comments are turned off. Learn more') disabled = true;
    }
  }
  const issues: MappedYoutubeMetrics['issues'] = [];
  const view = metricFromCandidates('view', [details.viewCount], 'youtube.player.videoDetails.viewCount', issues);
  const like = metricFromCandidates('like', likes, 'youtube.next.video_like', issues);
  let comment: Metric;
  if (disabled && comments.every(value => value === undefined)) {
    comment = { value: '0', status: 'disabled', source: 'youtube.next.comments_disabled_message', disabled: true };
  } else {
    comment = metricFromCandidates('comment', comments, 'youtube.next.comments_entry_count', issues);
    if (disabled) {
      comment = { value: null, status: 'unresolved', source: 'youtube.next.comments_conflict', disabled: null };
      if (!issues.some(issue => issue.field === 'comment')) issues.push({ field: 'comment', reason: 'conflicting' });
    } else comment.disabled = comment.value === null ? null : false;
  }
  const observation = snapshotContentObservation({ channelId: target.channelId, sourceContentId: target.sourceContentId,
    contentType: target.contentType, observedAt: evidence.observedAt, view, like, comment });
  return { observation, complete: issues.length === 0, issues };
}

/** Fetch player + next only. No comments continuation, getComments, or first-page collection. */
export async function readYoutubeVideoMetrics(runtime: CollectorRuntime, target: MetricsTarget,
  options: ReadStepOptions = {}): Promise<MappedYoutubeMetrics> {
  validateTarget(target);
  const owned = structuredClone(target);
  return runReadStep(runtime, 'youtube', async context => {
    const responses = new Map<string, unknown>();
    const requested = new Set<string>();
    const observedAt = new Date().toISOString(); // Conservative common timestamp: start of this attempt.
    const youtube = await Innertube.create({ lang: 'en', location: 'US', retrieve_player: false,
      generate_session_locally: true, retrieve_innertube_config: false, enable_session_cache: false,
      fetch: async (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        const endpoint = url.pathname;
        check(request.method === 'POST' && ['/youtubei/v1/player', '/youtubei/v1/next'].includes(endpoint),
          'YOUTUBE.METRICS_REQUEST_UNSUPPORTED');
        const requestBody = object(JSON.parse(await request.clone().text()));
        check(requestBody.videoId === owned.sourceContentId && requestBody.continuation === undefined,
          'YOUTUBE.REQUEST_IDENTITY_MISMATCH');
        check(!requested.has(endpoint), 'YOUTUBE.DUPLICATE_SURFACE');
        requested.add(endpoint);
        const response = classifyHttpResponse(context, await context.fetch(request));
        const raw = parseYoutubeEvidenceJson(await response.clone().text());
        responses.set(endpoint, raw);
        return response;
      },
    });
    await youtube.getInfo(owned.sourceContentId);
    check(responses.size === 2, 'YOUTUBE.METRICS_SURFACE_MISSING');
    return mapYoutubeMetrics(owned, { requestedVideoId: owned.sourceContentId, observedAt,
      player: responses.get('/youtubei/v1/player'), next: responses.get('/youtubei/v1/next') });
  }, options);
}

/** Pure assembly, not authorization or publishing. Submission identity must be supplied and retained by the Worker. */
export function buildMetricsSubmission(work: MetricsWork, results: readonly MappedYoutubeMetrics[], submissionId: string): Submission {
  check(work && typeof work === 'object' && work.identity && work.frozen_input && Array.isArray(work.targets) && Array.isArray(results),
    'COLLECTION.WORK_INVALID');
  check(validId(submissionId) && validId(work.scope) && /^(0|[1-9][0-9]{0,19})$/.test(work.execution_epoch), 'COLLECTION.WORK_INVALID');
  const i = work.identity, f = work.frozen_input;
  check(Object.keys(i).sort().join() === ['channel_id', 'logical_batch_key', 'plan_id'].join() &&
    Object.values(i).every(validId), 'COLLECTION.WORK_INVALID');
  check(Object.keys(f).sort().join() === ['generation', 'input_hash', 'input_revision', 'policy_version', 'target_hash'].join() &&
    /^(0|[1-9][0-9]{0,19})$/.test(f.generation) && /^(0|[1-9][0-9]{0,19})$/.test(f.input_revision) &&
    /^[a-f0-9]{64}$/.test(f.input_hash) && /^[a-f0-9]{64}$/.test(f.target_hash) && validId(f.policy_version), 'COLLECTION.WORK_INVALID');
  check(targetHash(work.targets) === f.target_hash, 'DATA.VERSION_CONFLICT');
  check(results.length > 0 && results.length <= 100 && results.length === work.targets.length, 'COLLECTION.BATCH_INCOMPLETE');
  const observations = results.map(result => {
    // Revalidate actual values, never trust a caller-supplied complete flag alone.
    const observation = snapshotContentObservation(result.observation);
    check(observation.channelId === i.channel_id && observation.page === undefined, 'COLLECTION.OBSERVATION_INVALID');
    check(result.complete && result.issues.length === 0 && ['view', 'like', 'comment'].every(name => {
      const metric = observation[name as MetricName];
      return metric && metric.value !== null && (metric.status === 'exact' || (name === 'comment' && metric.status === 'disabled'));
    }), 'COLLECTION.METRICS_INCOMPLETE');
    return observation;
  }).sort((a, b) => a.sourceContentId < b.sourceContentId ? -1 : a.sourceContentId > b.sourceContentId ? 1 : 0);
  check(targetHash(observations.map(o => ({ source_content_id: o.sourceContentId, content_type: o.contentType }))) === f.target_hash,
    'DATA.VERSION_CONFLICT');
  const payload = { schema_id: 'content.metrics.batch/1-draft.1' as const, identity: structuredClone(i),
    frozen_input: structuredClone(f), producer_role: 'incremental' as const, mode: 'delta' as const, observations };
  check(Buffer.byteLength(canonicalJson(payload), 'utf8') <= 1048576, 'CONTRACT.PAYLOAD_TOO_LARGE');
  return { submission_id: submissionId, execution: { scope: work.scope, execution_epoch: work.execution_epoch },
    content_sha256: contentHash(payload), payload };
}
