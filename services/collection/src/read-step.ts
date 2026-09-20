import { CollectionError, CollectorRuntime, type ReadContext } from '../../collector-runtime/src/index.js';

export interface ReadStepOptions {
  /** Explicit opt-in to repeating a replayable collection step; default is one attempt. */
  maxAttempts?: number;
  totalTimeoutMs?: number;
  signal?: AbortSignal;
}

/** Collection-layer policy. Neither Crawlee nor the IP extension retries this callback. */
export async function runReadStep<T>(runtime: CollectorRuntime, target: string,
  read: (context: ReadContext) => Promise<T>, options: ReadStepOptions = {}): Promise<T> {
  const maxAttempts = options.maxAttempts ?? 1;
  const totalTimeoutMs = options.totalTimeoutMs ?? 60_000;
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 3 ||
      !Number.isSafeInteger(totalTimeoutMs) || totalTimeoutMs < 1 || totalTimeoutMs > 600_000) {
    throw new CollectionError('COLLECTION.POLICY_INVALID');
  }
  const signal = AbortSignal.any([AbortSignal.timeout(totalTimeoutMs), ...(options.signal ? [options.signal] : [])]);
  const excludedProxyIds = new Set<string>();
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    if (signal.aborted) throw new CollectionError('REQUEST.ABORTED');
    try {
      return await runtime.runRead(target, context => {
        excludedProxyIds.add(context.proxyId);
        return read(context);
      }, { signal, excludedProxyIds });
    } catch (error) {
      const retryable = error instanceof CollectionError &&
        ['PROXY.NETWORK_FAILED', 'PROXY.UNAVAILABLE'].includes(error.code);
      if (!retryable || attempt === maxAttempts || signal.aborted) throw error;
    }
  }
  throw new CollectionError('COLLECTION.ATTEMPTS_EXHAUSTED');
}

/** Site-facing policy, deliberately outside the proxy extension and request transport. */
export function classifyHttpResponse(context: ReadContext, response: Response): Response {
  if (response.status === 403 || response.status === 429) {
    const retryAfter = response.headers.get('retry-after');
    const seconds = retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) : NaN;
    const delay = Number.isFinite(seconds) ? seconds * 1000 : retryAfter ? Date.parse(retryAfter) - Date.now() : 0;
    context.cooldownProxy(Math.max(60_000, Math.min(86_400_000, Number.isFinite(delay) ? delay : 0)));
    throw new CollectionError(response.status === 429 ? 'TARGET.RATE_LIMITED' : 'TARGET.FORBIDDEN');
  }
  if (!response.ok) throw new CollectionError('HTTP.STATUS_' + response.status);
  return response;
}
