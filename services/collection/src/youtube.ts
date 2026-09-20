import { Innertube } from 'youtubei.js';
import { CollectionError, CollectorRuntime, type ReadContext } from '../../collector-runtime/src/index.js';
import { runReadStep, classifyHttpResponse, type ReadStepOptions } from './read-step.js';

/** Recreate the YouTube client for each attempt; never share visitor/cookie/connection state across identities. */
export async function withYoutubeRead<T>(runtime: CollectorRuntime,
  read: (youtube: Innertube, context: ReadContext) => Promise<T>,
  options: ReadStepOptions = {}): Promise<T> {
  return runReadStep(runtime, 'youtube', async context => {
    const youtube = await Innertube.create({ fetch: async (input, init) =>
      classifyHttpResponse(context, await context.fetch(input, init)),
      retrieve_player: false, generate_session_locally: true,
      retrieve_innertube_config: false, enable_session_cache: false });
    return read(youtube, context);
  }, options);
}

/** First read-only integration slice; not a complete metrics submission or comment collector. */
export async function readYoutubeVideo(runtime: CollectorRuntime, videoId: string,
  options: ReadStepOptions = {}) {
  if (!/^[A-Za-z0-9_-]{11}$/.test(videoId)) throw new CollectionError('YOUTUBE.VIDEO_ID_INVALID');
  return withYoutubeRead(runtime, async youtube => {
    const info = await youtube.getBasicInfo(videoId);
    if (info.basic_info.id !== videoId) throw new CollectionError('YOUTUBE.IDENTITY_MISMATCH');
    return info;
  }, options);
}
