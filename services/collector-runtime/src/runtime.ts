import { randomUUID } from 'node:crypto';
import { Configuration, ProxyConfiguration, Session, SessionPool } from '@crawlee/core';
import { ProxyAgent, fetch as proxyFetch } from 'undici';
import { CollectionError, ProxyPool, type ProxyLease } from './proxy-pool.js';

export interface ReadContext {
  readonly sessionId: string;
  readonly proxyId: string;
  readonly fetch: typeof globalThis.fetch;
  /** The collection layer decides when/why this target needs a cooldown. */
  cooldownProxy(durationMs: number): void;
}
export interface RuntimeOptions {
  maxSessions?: number;
  requestTimeoutMs?: number;
  operationTimeoutMs?: number;
  maxResponseBytes?: number;
  targets?: Record<string, readonly string[]>;
}
export interface ProbeSpec {
  url: string;
  /** Must verify the response content, not merely accept any HTTP 200. */
  validate(response: Response): Promise<boolean>;
}

function agentFor(urlString: string): ProxyAgent {
  const url = new URL(urlString);
  const token = url.username || url.password
    ? 'Basic ' + Buffer.from(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`).toString('base64')
    : undefined;
  url.username = ''; url.password = '';
  return new ProxyAgent({ uri: url.toString(), token, connections: 2, pipelining: 1, proxyTunnel: true });
}

interface ByteStream {
  getReader(): {
    read(): Promise<{ done: boolean; value?: Uint8Array }>;
    cancel(): Promise<void>;
    releaseLock(): void;
  };
}

async function boundedBody(body: ByteStream | null, limit: number, signal?: AbortSignal): Promise<Uint8Array<ArrayBuffer>> {
  if (!body) return new Uint8Array();
  const reader = body.getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', cancel, { once: true });
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      if (signal?.aborted) throw new CollectionError('REQUEST.ABORTED');
      const { value, done } = await reader.read();
      if (signal?.aborted) throw new CollectionError('REQUEST.ABORTED');
      if (done || !value) break;
      total += value.byteLength;
      if (total > limit) throw new CollectionError('HTTP.BODY_TOO_LARGE');
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { signal?.removeEventListener('abort', cancel); reader.releaseLock(); }
  return new Uint8Array(Buffer.concat(chunks, total));
}

async function abortable<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  let onAbort: () => void = () => {};
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => reject(new CollectionError('REQUEST.ABORTED'));
    signal.addEventListener('abort', onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
  try { return await Promise.race([work, aborted]); }
  finally { signal.removeEventListener('abort', onAbort); }
}

function networkError(error: unknown): boolean {
  const codes = new Set(['ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EHOSTUNREACH', 'ENETUNREACH',
    'ENOTFOUND', 'EAI_AGAIN', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT',
    'UND_ERR_BODY_TIMEOUT', 'UND_ERR_SOCKET', 'UND_ERR_PRX_TLS', 'UND_ERR_ABORTED']);
  let current = error;
  for (let i = 0; i < 5 && current && typeof current === 'object'; i++) {
    const candidate = current as { code?: string; cause?: unknown };
    if (candidate.code && codes.has(candidate.code)) return true;
    current = candidate.cause;
  }
  return false;
}

/** Reusable library, not a server, task queue, or gateway. Each operation is an isolated read attempt. */
export class CollectorRuntime {
  private readonly lifetime = new AbortController();
  private readonly agents = new Set<ProxyAgent>();
  private readonly intervals = new Set<ReturnType<typeof setInterval>>();
  private probeRun?: Promise<void>;
  private active = 0;
  private readonly maxSessions: number;
  private readonly requestTimeoutMs: number;
  private readonly operationTimeoutMs: number;
  private readonly maxResponseBytes: number;
  private readonly targets: Record<string, readonly string[]>;

  private constructor(readonly proxies: ProxyPool, private readonly sessions: SessionPool, options: RuntimeOptions) {
    this.maxSessions = options.maxSessions ?? 8;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
    this.operationTimeoutMs = options.operationTimeoutMs ?? 60_000;
    this.maxResponseBytes = options.maxResponseBytes ?? 4 * 1024 * 1024;
    this.targets = structuredClone(options.targets ?? { youtube: ['www.youtube.com', 'youtube.com', 'youtubei.googleapis.com'] });
  }

  static async create(proxies: ProxyPool, options: RuntimeOptions = {}): Promise<CollectorRuntime> {
    for (const [name, value] of Object.entries(options)) {
      if (name !== 'targets' && (!Number.isSafeInteger(value) || Number(value) < 1)) {
        throw new CollectionError('RUNTIME.CONFIG_INVALID');
      }
    }
    if ((options.maxSessions ?? 8) > 100 || (options.maxResponseBytes ?? 4 * 1024 * 1024) > 16 * 1024 * 1024 ||
        (options.operationTimeoutMs ?? 60_000) > 600_000 || (options.requestTimeoutMs ?? 15_000) > 60_000) {
      throw new CollectionError('RUNTIME.CONFIG_INVALID');
    }
    if (options.targets && (Object.keys(options.targets).length > 32 || Object.entries(options.targets).some(([target, hosts]) =>
      !/^[a-z0-9_-]{1,64}$/.test(target) || !Array.isArray(hosts) || hosts.length === 0 || hosts.length > 32 ||
      hosts.some(host => typeof host !== 'string' || !/^[a-z0-9.-]+$/.test(host))))) {
      throw new CollectionError('RUNTIME.CONFIG_INVALID');
    }
    const sessions = new SessionPool({ maxPoolSize: options.maxSessions ?? 8,
      persistenceOptions: { enable: false } }, new Configuration({ persistStorage: false }));
    await sessions.initialize();
    return new CollectorRuntime(proxies, sessions, options);
  }

  private trackAgent(url: string): ProxyAgent {
    const agent = agentFor(url);
    this.agents.add(agent);
    return agent;
  }

  private async disposeAgent(agent: ProxyAgent): Promise<void> {
    try { await agent.destroy(); } finally { this.agents.delete(agent); }
  }

  /** One attempt only. Retry policy and business response classification belong to the collection layer. */
  async runRead<T>(target: string, read: (context: ReadContext) => Promise<T>,
    options: { excludedProxyIds?: ReadonlySet<string>; signal?: AbortSignal } = {}): Promise<T> {
    const hosts = this.targets[target];
    if (!hosts) {
      throw new CollectionError('RUNTIME.CONFIG_INVALID');
    }
    if (this.lifetime.signal.aborted) throw new CollectionError('RUNTIME.CLOSED');
    if (this.active >= this.maxSessions) throw new CollectionError('RUNTIME.AT_CAPACITY');
    this.active++;
    const signal = AbortSignal.any([this.lifetime.signal, AbortSignal.timeout(this.operationTimeoutMs),
      ...(options.signal ? [options.signal] : [])]);
    try {
      if (signal.aborted) throw new CollectionError('REQUEST.ABORTED');
      const lease = this.proxies.acquire(target, options.excludedProxyIds ?? new Set());
      const session = new Session({ id: randomUUID(), sessionPool: this.sessions,
        maxAgeSecs: Math.ceil(this.operationTimeoutMs / 1000) + 1, maxUsageCount: 1000 });
      let agent: ProxyAgent | undefined;
      let valid = true;
      let inflightRequests = 0;
      let requestCount = 0;
      const attemptLifetime = new AbortController();
      try {
        // Named sessions avoid SessionPool.getSession() randomly sharing a busy session.
        await this.sessions.addSession(session);
        const configuration = new ProxyConfiguration({ newUrlFunction: () => this.proxies.connectionUrl(lease) });
        const url = await configuration.newUrl(session.id);
        if (!url) throw new CollectionError('PROXY.UNAVAILABLE');
        agent = this.trackAgent(url);
        const requestAgent = agent;
        const context: ReadContext = {
          sessionId: session.id, proxyId: lease.proxyId,
          cooldownProxy: durationMs => {
            if (!valid) throw new CollectionError('SESSION.CLOSED');
            if (!Number.isSafeInteger(durationMs) || durationMs < 1 || durationMs > 86_400_000) {
              throw new CollectionError('PROXY.COOLDOWN_INVALID');
            }
            this.proxies.coolTarget(lease, durationMs);
          },
          fetch: async (input, init) => {
            if (!valid) throw new CollectionError('SESSION.CLOSED');
            this.proxies.assertUsable(lease);
            if (!session.isUsable()) throw new CollectionError('SESSION.EXPIRED');
            if (inflightRequests >= 2) throw new CollectionError('HTTP.AT_CAPACITY');
            if (++requestCount > 100) throw new CollectionError('HTTP.REQUEST_BUDGET_EXCEEDED');
            inflightRequests++;
            try { return await this.request(input, init, requestAgent, hosts, signal, attemptLifetime.signal, session, lease); }
            finally { inflightRequests--; }
          },
        };
        const result = await abortable(Promise.resolve().then(() => read(context)), signal);
        if (signal.aborted) throw new CollectionError('REQUEST.ABORTED');
        session.markGood();
        return result;
      } catch (error) {
        if (signal.aborted) throw new CollectionError('REQUEST.ABORTED');
        throw error;
      } finally {
        valid = false;
        attemptLifetime.abort();
        session.retire();
        this.proxies.release(lease);
        if (agent) await this.disposeAgent(agent);
      }
    } finally { this.active--; }
  }

  private async request(input: RequestInfo | URL, init: RequestInit | undefined, agent: ProxyAgent,
    hosts: readonly string[], operationSignal: AbortSignal, attemptSignal: AbortSignal, session: Session, lease: ProxyLease): Promise<Response> {
    const request = new Request(input, init);
    const url = new URL(request.url);
    if (!['https:', 'http:'].includes(url.protocol) || !hosts.includes(url.hostname) || url.username || url.password) {
      throw new CollectionError('HTTP.TARGET_DENIED');
    }
    if (!['GET', 'HEAD', 'POST'].includes(request.method) || request.headers.has('cookie') ||
        request.headers.has('authorization') || request.headers.has('proxy-authorization')) {
      throw new CollectionError('HTTP.REQUEST_UNSUPPORTED'); // First slice: anonymous collection only.
    }
    const timeout = AbortSignal.timeout(this.requestTimeoutMs);
    const signal = AbortSignal.any([operationSignal, attemptSignal, request.signal, timeout]);
    const headers = new Headers(request.headers);
    const cookies = session.getCookieString(url.toString());
    if (cookies) headers.set('cookie', cookies);
    try {
      const body = request.body ? await boundedBody(request.body, 1024 * 1024, signal) : undefined;
      const response = await proxyFetch(url, { method: request.method, headers: Object.fromEntries(headers), body, dispatcher: agent,
        signal, redirect: 'manual' });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        throw new CollectionError('HTTP.REDIRECT_UNSUPPORTED');
      }
      const bytes = await boundedBody(response.body, this.maxResponseBytes, signal);
      for (const cookie of response.headers.getSetCookie()) {
        session.cookieJar.setCookieSync(cookie, url.toString(), { ignoreError: true });
      }
      const responseHeaders = new Headers(Array.from(response.headers.entries()));
      responseHeaders.delete('content-encoding');
      responseHeaders.delete('content-length');
      const result = new Response(request.method === 'HEAD' || [204, 205].includes(response.status) ? null : bytes,
        { status: response.status, statusText: response.statusText, headers: responseHeaders });
      Object.defineProperty(result, 'url', { value: response.url });
      return result;
    } catch (error) {
      if (operationSignal.aborted || attemptSignal.aborted || request.signal.aborted) throw new CollectionError('REQUEST.ABORTED');
      if (timeout.aborted || (!(error instanceof CollectionError) && networkError(error))) {
        this.proxies.networkFailure(lease);
        throw new CollectionError('PROXY.NETWORK_FAILED');
      }
      if (error instanceof CollectionError) throw error;
      throw new CollectionError('HTTP.TRANSPORT_FAILED'); // Do not leak proxy URLs or credentials in nested errors.
    }
  }

  /** No probes are sent on import/create. The caller supplies a bounded, content-validated endpoint. */
  async probeOnce(spec: ProbeSpec): Promise<void> {
    if (this.lifetime.signal.aborted) throw new CollectionError('RUNTIME.CLOSED');
    if (!this.probeRun) this.probeRun = this.performProbes(spec).finally(() => { this.probeRun = undefined; });
    return this.probeRun;
  }

  private async performProbes(spec: ProbeSpec): Promise<void> {
    const pending = this.proxies.snapshot().proxies.map(proxy => proxy.id);
    const worker = async () => {
      while (pending.length && !this.lifetime.signal.aborted) {
        const probe = this.proxies.beginProbe(pending.shift()!);
        if (!probe) continue;
        const agent = this.trackAgent(probe.url);
        let success = false;
        try {
          const signal = AbortSignal.any([this.lifetime.signal, AbortSignal.timeout(this.requestTimeoutMs)]);
          const response = await proxyFetch(spec.url, { dispatcher: agent, redirect: 'manual',
            signal });
          const bytes = await boundedBody(response.body, Math.min(this.maxResponseBytes, 64 * 1024), signal);
          success = response.ok && await abortable(spec.validate(new Response([204, 205].includes(response.status) ? null : bytes,
            { status: response.status, headers: new Headers(Array.from(response.headers.entries())) })), signal);
        } catch { /* Only a boolean enters health state; upstream error details can contain credentials. */ }
        finally { probe.finish(success); await this.disposeAgent(agent); }
      }
    };
    await Promise.all([worker(), worker()]);
  }

  startProbes(spec: ProbeSpec, intervalMs = 60_000): () => void {
    if (this.lifetime.signal.aborted) throw new CollectionError('RUNTIME.CLOSED');
    if (!Number.isSafeInteger(intervalMs) || intervalMs < 1000) throw new CollectionError('RUNTIME.CONFIG_INVALID');
    let running = false;
    const tick = () => {
      if (running || this.lifetime.signal.aborted) return;
      running = true;
      void this.probeOnce(spec).catch(() => {}).finally(() => { running = false; });
    };
    const timer = setInterval(tick, intervalMs);
    timer.unref();
    this.intervals.add(timer);
    tick();
    return () => { clearInterval(timer); this.intervals.delete(timer); };
  }

  async close(): Promise<void> {
    this.lifetime.abort();
    for (const timer of this.intervals) clearInterval(timer);
    this.intervals.clear();
    await Promise.all([...this.agents].map(agent => this.disposeAgent(agent)));
    await this.sessions.teardown({ persistState: false });
  }
}
