import { randomUUID } from 'node:crypto';

export class CollectionError extends Error {
  constructor(public readonly code: string) { super(code); this.name = 'CollectionError'; }
}

export interface ProxyDefinition {
  id: string;
  /** Stable node/process assignment. The platform must prevent overlapping assignments. */
  holderId: string;
  url: string;
  enabled: boolean;
  expiresAt?: number;
  maxSessions?: number;
}
type Entry = {
  config: ProxyDefinition;
  healthyUntil: number;
  retryAt: number;
  failures: number;
  generation: number;
  active: number;
  probing: boolean;
  cooldowns: Map<string, number>;
};
export interface ProxyLease { readonly id: string; readonly proxyId: string; readonly target: string }
type Reservation = { lease: ProxyLease; entry: Entry };

/** Process-local ownership, deliberately not a distributed lease service. */
export class ProxyPool {
  private entries = new Map<string, Entry>();
  private leases = new Map<string, Reservation>();
  private revision = -1;
  private cursor = 0;

  constructor(
    readonly holderId: string,
    private readonly now: () => number = Date.now,
    readonly healthTtlMs = 300_000,
    readonly failureCooldownMs = 30_000,
  ) {
    if (!holderId || !Number.isSafeInteger(healthTtlMs) || healthTtlMs < 1 ||
        !Number.isSafeInteger(failureCooldownMs) || failureCooldownMs < 1) {
      throw new CollectionError('PROXY.CONFIG_INVALID');
    }
  }

  /** Atomic full snapshot; invalid input never partially changes the running pool. */
  configure(revision: number, definitions: readonly ProxyDefinition[]): void {
    if (!Number.isSafeInteger(revision) || revision <= this.revision || definitions.length > 10_000) {
      throw new CollectionError('PROXY.CONFIG_INVALID');
    }
    const parsed = new Map<string, ProxyDefinition>();
    const urls = new Set<string>();
    for (const definition of definitions) {
      let url: URL;
      try { url = new URL(definition.url); } catch { throw new CollectionError('PROXY.CONFIG_INVALID'); }
      const maxSessions = definition.maxSessions ?? 1;
      if (!/^[A-Za-z0-9._-]{1,100}$/.test(definition.id) || parsed.has(definition.id) ||
          definition.holderId !== this.holderId || typeof definition.enabled !== 'boolean' ||
          !['http:', 'https:'].includes(url.protocol) || !url.hostname || url.search || url.hash || url.pathname !== '/' ||
          !Number.isSafeInteger(maxSessions) || maxSessions < 1 || maxSessions > 100 ||
          (definition.expiresAt !== undefined && !Number.isSafeInteger(definition.expiresAt))) {
        throw new CollectionError('PROXY.CONFIG_INVALID');
      }
      // Decode now so malformed credential escapes cannot fail later with a secret-bearing error.
      try { decodeURIComponent(url.username); decodeURIComponent(url.password); }
      catch { throw new CollectionError('PROXY.CONFIG_INVALID'); }
      if (urls.has(url.toString())) throw new CollectionError('PROXY.CONFIG_INVALID');
      urls.add(url.toString());
      parsed.set(definition.id, { ...definition, url: url.toString(), maxSessions });
    }
    const next = new Map<string, Entry>();
    for (const [id, config] of parsed) {
      const existing = this.entries.get(id);
      if (existing && existing.config.url === config.url) {
        existing.config = config;
        existing.generation++; // Invalidate any older in-flight probe.
        next.set(id, existing);
      } else {
        next.set(id, { config, healthyUntil: 0, retryAt: 0, failures: 0,
          generation: 0, active: 0, probing: false, cooldowns: new Map() });
      }
    }
    this.entries = next;
    this.revision = revision;
  }

  private enabled(entry: Entry): boolean {
    return this.entries.get(entry.config.id) === entry && entry.config.enabled &&
      (entry.config.expiresAt === undefined || entry.config.expiresAt > this.now());
  }

  private available(entry: Entry, target: string): boolean {
    return this.enabled(entry) && entry.healthyUntil > this.now() &&
      (entry.cooldowns.get(target) ?? 0) <= this.now();
  }

  acquire(target: string, excluded: ReadonlySet<string>): ProxyLease {
    const candidates = [...this.entries.values()];
    for (let i = 0; i < candidates.length; i++) {
      const index = (this.cursor + i) % candidates.length;
      const entry = candidates[index]!;
      if (excluded.has(entry.config.id) || !this.available(entry, target) ||
          entry.active >= entry.config.maxSessions!) continue;
      const lease = Object.freeze({ id: randomUUID(), proxyId: entry.config.id, target });
      this.leases.set(lease.id, { lease, entry });
      entry.active++;
      this.cursor = (index + 1) % candidates.length;
      return lease;
    }
    throw new CollectionError('PROXY.NO_CAPACITY'); // Never falls back to direct Internet access.
  }

  private reservation(lease: ProxyLease): Reservation {
    const reservation = this.leases.get(lease.id);
    if (!reservation || reservation.lease !== lease) throw new CollectionError('PROXY.LEASE_INVALID');
    return reservation;
  }

  assertUsable(lease: ProxyLease): void {
    if (!this.available(this.reservation(lease).entry, lease.target)) {
      throw new CollectionError('PROXY.UNAVAILABLE');
    }
  }

  connectionUrl(lease: ProxyLease): string {
    this.assertUsable(lease);
    return this.reservation(lease).entry.config.url;
  }

  release(lease: ProxyLease): void {
    const reservation = this.leases.get(lease.id);
    if (!reservation || reservation.lease !== lease) return;
    reservation.entry.active--;
    this.leases.delete(lease.id);
  }

  private fail(entry: Entry): void {
    entry.generation++;
    entry.failures++;
    entry.healthyUntil = 0;
    entry.retryAt = this.now() + Math.min(300_000, this.failureCooldownMs * 2 ** Math.min(entry.failures - 1, 4));
  }

  networkFailure(lease: ProxyLease): void {
    const { entry } = this.reservation(lease);
    if (this.enabled(entry)) this.fail(entry);
  }

  coolTarget(lease: ProxyLease, durationMs: number): void {
    const { entry } = this.reservation(lease);
    entry.cooldowns.set(lease.target, Math.max(entry.cooldowns.get(lease.target) ?? 0, this.now() + durationMs));
  }

  /** Probe concurrency belongs to the runtime; stale successes cannot undo newer failure/config changes. */
  beginProbe(id: string): { url: string; finish: (success: boolean) => void } | undefined {
    const entry = this.entries.get(id);
    if (!entry || !this.enabled(entry) || entry.probing || entry.retryAt > this.now()) return;
    entry.probing = true;
    const generation = entry.generation;
    let finished = false;
    return {
      url: entry.config.url,
      finish: (success) => {
        if (finished) return;
        finished = true;
        entry.probing = false;
        if (!this.enabled(entry) || generation !== entry.generation) return;
        if (success) {
          entry.healthyUntil = this.now() + this.healthTtlMs;
          entry.failures = 0;
          entry.retryAt = 0;
        } else this.fail(entry);
      },
    };
  }

  /** No URLs, passwords or cookies in operational snapshots. */
  snapshot() {
    return { revision: this.revision, proxies: [...this.entries.values()].map(entry => ({
      id: entry.config.id,
      enabled: this.enabled(entry),
      healthy: entry.healthyUntil > this.now(),
      healthyUntil: entry.healthyUntil,
      retryAt: entry.retryAt,
      failures: entry.failures,
      activeSessions: entry.active,
      targetCooldowns: Object.fromEntries([...entry.cooldowns].filter(([, until]) => until > this.now())),
    })) };
  }
}
