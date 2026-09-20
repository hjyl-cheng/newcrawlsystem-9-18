import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createServer as createHttpsServer } from 'node:https';
import { connect } from 'node:net';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { getCACertificates, setDefaultCACertificates } from 'node:tls';
import { ProxyPool, CollectorRuntime } from '../dist/services/collector-runtime/src/index.js';
import { readYoutubeVideo, withYoutubeRead } from '../dist/services/collection/src/youtube.js';
import { runReadStep, classifyHttpResponse } from '../dist/services/collection/src/read-step.js';
import { readYoutubeVideoMetrics } from '../dist/services/collection/src/youtube-metrics.js';
import { target as metricsTarget, metricsEvidence } from './helpers/youtube-metrics-fixture.mjs';

const probeSpec = { url: 'http://fixture.test/probe', validate: async response => (await response.text()) === 'proxy-ok' };
const definition = (id, url) => ({ id, holderId: 'worker-1', url, enabled: true });
const rejectsCode = (promise, code) => assert.rejects(promise, error => error.code === code);

/** All origin and CONNECT traffic terminates on loopback. No external proxies or YouTube traffic. */
async function fixture(t, options = {}) {
  let now = 1_000_000;
  const pool = new ProxyPool('worker-1', () => now, 300_000, 100);
  const sockets = new Set();
  const servers = [];
  let handler = (_request, response) => response.end('ok');
  const observed = [];
  const origin = createServer((request, response) => {
    observed.push({ path: request.url, cookie: request.headers.cookie, headers: request.headers });
    if (request.url === '/probe') return response.end('proxy-ok');
    return handler(request, response);
  });
  async function listen(server) {
    servers.push(server);
    server.on('connection', socket => {
      sockets.add(socket);
      socket.on('error', () => {});
      socket.on('close', () => sockets.delete(socket));
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    return server.address().port;
  }
  const port = await listen(origin);
  let tlsPort;
  async function proxy(id) {
    const state = { id, fail: false, connections: 0, auth: [], destinations: [] };
    const server = createServer((_request, response) => { response.writeHead(500); response.end(); });
    server.on('connect', (request, socket, head) => {
      state.connections++;
      state.auth.push(request.headers['proxy-authorization']);
      state.destinations.push(request.url);
      if (state.fail) return socket.destroy();
      // No arbitrary destination forwarding is possible in this test proxy.
      if (!['fixture.test:80', 'www.youtube.com:443'].includes(request.url)) return socket.destroy();
      const upstream = connect(request.url.endsWith(':443') ? tlsPort : port, '127.0.0.1');
      sockets.add(upstream);
      upstream.on('close', () => { sockets.delete(upstream); socket.destroy(); });
      upstream.on('error', () => socket.destroy());
      socket.on('close', () => upstream.destroy());
      upstream.on('connect', () => {
        socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        if (head.length) upstream.write(head);
        upstream.pipe(socket);
        socket.pipe(upstream);
      });
    });
    const proxyPort = await listen(server);
    state.url = `http://user:fixture-password@127.0.0.1:${proxyPort}`;
    return state;
  }
  const a = await proxy('a');
  const b = await proxy('b');
  const definitions = [definition('a', a.url), definition('b', b.url)];
  pool.configure(1, definitions);
  const runtime = await CollectorRuntime.create(pool, { targets: {
    fixture: ['fixture.test'], youtube: ['www.youtube.com'],
  }, requestTimeoutMs: 1000, ...options });
  t.after(async () => {
    await runtime.close();
    for (const socket of sockets) socket.destroy();
    await Promise.all(servers.map(server => new Promise(resolve => server.close(resolve))));
  });
  return { pool, runtime, a, b, definitions, observed, advance: ms => { now += ms; },
    handler: fn => { handler = fn; },
    addTlsOrigin: async (tlsOptions, fn) => { tlsPort = await listen(createHttpsServer(tlsOptions, fn)); },
    ready: () => runtime.probeOnce(probeSpec) };
}

test('only probed, assigned, enabled, unexpired proxies are selectable; rejected snapshots are atomic', async t => {
  const f = await fixture(t);
  let called = false;
  await rejectsCode(f.runtime.runRead('fixture', async () => { called = true; }), 'PROXY.NO_CAPACITY');
  assert.equal(called, false);
  assert.equal(f.a.connections + f.b.connections, 0);
  assert.throws(() => f.pool.configure(2, [f.definitions[0], { ...f.definitions[1], holderId: 'other' }]),
    { code: 'PROXY.CONFIG_INVALID' });
  assert.equal(f.pool.snapshot().revision, 1);
  await f.ready();
  assert.equal(f.pool.snapshot().proxies.filter(p => p.healthy).length, 2);
  f.pool.configure(2, [{ ...f.definitions[0], enabled: false }, { ...f.definitions[1], expiresAt: 999_999 }]);
  await rejectsCode(f.runtime.runRead('fixture', async () => true), 'PROXY.NO_CAPACITY');
  assert.ok(!JSON.stringify(f.pool.snapshot()).includes('fixture-password'));
});

test('real CONNECT failure switches to another proxy and a fresh Crawlee session; empty data does not retry', async t => {
  const f = await fixture(t);
  await f.ready();
  f.a.fail = true;
  const attempts = [];
  f.handler((_request, response) => { response.setHeader('content-type', 'application/json'); response.end('[]'); });
  const result = await runReadStep(f.runtime, 'fixture', async context => {
    attempts.push([context.proxyId, context.sessionId]);
    return (await context.fetch('http://fixture.test/empty')).json();
  }, { maxAttempts: 2 });
  assert.deepEqual(result, []);
  assert.deepEqual(attempts.map(a => a[0]), ['a', 'b']);
  assert.notEqual(attempts[0][1], attempts[1][1]);
  assert.equal(f.observed.filter(r => r.path === '/empty').length, 1);
  assert.equal(f.pool.snapshot().proxies.find(p => p.id === 'a').healthy, false);
  const before = f.a.connections;
  await f.runtime.runRead('fixture', async context => (await context.fetch('http://fixture.test/again')).text());
  assert.equal(f.a.connections, before);
  assert.ok(f.b.auth.every(value => value === 'Basic ' + Buffer.from('user:fixture-password').toString('base64')));
  assert.ok(f.observed.every(r => !r.headers['proxy-authorization']));
  assert.ok(f.pool.snapshot().proxies.every(p => p.activeSessions === 0));
});

test('429 cools only the target, does not rotate immediately, and successful network probes do not clear it', async t => {
  const f = await fixture(t);
  await f.ready();
  let attempts = 0;
  f.handler((_request, response) => { response.writeHead(429, { 'retry-after': '120' }); response.end('limited'); });
  await rejectsCode(f.runtime.runRead('fixture', async context => {
    attempts++;
    classifyHttpResponse(context, await context.fetch('http://fixture.test/limited'));
  }), 'TARGET.RATE_LIMITED');
  assert.equal(attempts, 1);
  await f.ready();
  const a = f.pool.snapshot().proxies.find(p => p.id === 'a');
  assert.equal(a.healthy, true);
  assert.equal(a.targetCooldowns.fixture, 1_120_000);
  const lease = f.pool.acquire('fixture', new Set());
  assert.equal(lease.proxyId, 'b');
  f.pool.release(lease);
  const otherTarget = f.pool.acquire('youtube', new Set(['b']));
  assert.equal(otherTarget.proxyId, 'a');
  f.pool.release(otherTarget);
});

test('cookies and connections stay bound within an operation and isolated across concurrent operations', async t => {
  const f = await fixture(t);
  await f.ready();
  f.handler((request, response) => {
    if (request.url.startsWith('/set/')) response.setHeader('set-cookie', `identity=${request.url.slice(5)}; Path=/`);
    response.end(request.headers.cookie ?? 'none');
  });
  const sessions = [];
  const results = await Promise.all([1, 2].map(value => f.runtime.runRead('fixture', async context => {
    sessions.push(context.sessionId);
    assert.equal(await (await context.fetch('http://fixture.test/initial')).text(), 'none');
    await context.fetch(`http://fixture.test/set/${value}`);
    return (await context.fetch('http://fixture.test/check')).text();
  })));
  assert.deepEqual(results, ['identity=1', 'identity=2']);
  assert.equal(new Set(sessions).size, 2);
  assert.equal(await f.runtime.runRead('fixture', async context => (await context.fetch('http://fixture.test/new')).text()), 'none');
});

test('in-flight successful probe cannot resurrect a newer failed or revoked proxy; recovery requires fresh probe', async t => {
  const f = await fixture(t);
  await f.ready();
  const lease = f.pool.acquire('fixture', new Set(['b']));
  const oldProbe = f.pool.beginProbe('a');
  f.pool.networkFailure(lease);
  oldProbe.finish(true);
  assert.equal(f.pool.snapshot().proxies[0].healthy, false);
  f.pool.release(lease);
  f.advance(101);
  await f.ready();
  assert.equal(f.pool.snapshot().proxies[0].healthy, true);
  const active = f.pool.acquire('fixture', new Set(['b']));
  f.pool.configure(2, [{ ...f.definitions[0], enabled: false }, f.definitions[1]]);
  assert.throws(() => f.pool.assertUsable(active), { code: 'PROXY.UNAVAILABLE' });
  f.pool.release(active);
});

test('bounded body, non-network errors, and redirects do not cause proxy rotation', async t => {
  const f = await fixture(t, { maxResponseBytes: 64 });
  await f.ready();
  for (const [path, expected] of [['/large', 'HTTP.BODY_TOO_LARGE'], ['/redirect', 'HTTP.REDIRECT_UNSUPPORTED'], ['/forbidden', 'TARGET.FORBIDDEN']]) {
    let attempts = 0;
    f.handler((request, response) => {
      if (request.url === '/redirect') { response.writeHead(302, { location: 'http://elsewhere.invalid/' }); response.end(); }
      else if (request.url === '/forbidden') { response.writeHead(403); response.end('forbidden'); }
      else response.end('x'.repeat(65));
    });
    await rejectsCode(f.runtime.runRead('fixture', async context => {
      attempts++;
      return classifyHttpResponse(context, await context.fetch(`http://fixture.test${path}`));
    }), expected);
    assert.equal(attempts, 1);
  }
  assert.ok(f.pool.snapshot().proxies.every(p => p.healthy));
});

test('operation cancellation releases capacity and does not mark a healthy proxy dead', async t => {
  const f = await fixture(t, { maxSessions: 1 });
  await f.ready();
  const controller = new AbortController();
  let entered;
  const ready = new Promise(resolve => { entered = resolve; });
  let retained;
  const work = f.runtime.runRead('fixture', async context => {
    retained = context;
    entered();
    return new Promise(() => {});
  }, { signal: controller.signal });
  await ready;
  await rejectsCode(f.runtime.runRead('fixture', async () => true), 'RUNTIME.AT_CAPACITY');
  controller.abort();
  await rejectsCode(work, 'REQUEST.ABORTED');
  assert.ok(f.pool.snapshot().proxies.every(p => p.activeSessions === 0 && p.healthy));
  await rejectsCode(retained.fetch('http://fixture.test/stale'), 'SESSION.CLOSED');
});

test('parser/business errors and empty results are not interpreted as proxy failure', async t => {
  const f = await fixture(t);
  await f.ready();
  let attempts = 0;
  const parserError = new Error('fixture parser mismatch');
  await assert.rejects(f.runtime.runRead('fixture', async () => { attempts++; throw parserError; }), error => error === parserError);
  assert.equal(attempts, 1);
  assert.deepEqual(await f.runtime.runRead('fixture', async () => []), []);
  assert.ok(f.pool.snapshot().proxies.every(p => p.healthy));
});

test('all proxies failing terminates within the retry budget and never falls back to a direct request', async t => {
  const f = await fixture(t);
  await f.ready();
  f.a.fail = true; f.b.fail = true;
  let attempts = 0;
  await rejectsCode(runReadStep(f.runtime, 'fixture', async context => {
    attempts++;
    return context.fetch('http://fixture.test/unreachable');
  }, { maxAttempts: 2 }), 'PROXY.NETWORK_FAILED');
  assert.equal(attempts, 2);
  assert.equal(f.observed.filter(r => r.path === '/unreachable').length, 0);
  await rejectsCode(f.runtime.runRead('fixture', async () => true), 'PROXY.NO_CAPACITY');
  assert.ok(f.pool.snapshot().proxies.every(p => !p.healthy && p.activeSessions === 0));
});

test('timeout covers response body consumption, retires the proxy, and releases its reservation', async t => {
  const f = await fixture(t, { requestTimeoutMs: 150 });
  await f.ready();
  f.handler((_request, response) => { response.writeHead(200); response.write('partial'); });
  await rejectsCode(f.runtime.runRead('fixture', async context => context.fetch('http://fixture.test/stalled')),
    'PROXY.NETWORK_FAILED');
  assert.equal(f.pool.snapshot().proxies[0].healthy, false);
  assert.ok(f.pool.snapshot().proxies.every(p => p.activeSessions === 0));
});

test('actual YouTube.js getBasicInfo uses injected fetch through CONNECT with TLS and parses fixture video', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'crawl-youtube-fixture-'));
  const previousRoots = getCACertificates('default');
  t.after(async () => { setDefaultCACertificates(previousRoots); await rm(directory, { recursive: true, force: true }); });
  const keyPath = join(directory, 'key.pem');
  const certPath = join(directory, 'cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', keyPath,
    '-out', certPath, '-days', '1', '-subj', '/CN=www.youtube.com', '-addext', 'subjectAltName=DNS:www.youtube.com'], { stdio: 'ignore' });
  const key = await readFile(keyPath);
  const cert = await readFile(certPath);
  setDefaultCACertificates([...previousRoots, cert.toString()]);
  const f = await fixture(t);
  const requests = [];
  let returnedId = 'abcdefghijk';
  let metricsMode = false;
  await f.addTlsOrigin({ key, cert }, async (request, response) => {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    requests.push({ url: request.url, body: JSON.parse(raw) });
    response.setHeader('content-type', 'application/json');
    if (metricsMode) {
      const fixture = metricsEvidence();
      const json = JSON.stringify(request.url.startsWith('/youtubei/v1/next') ? fixture.next : fixture.player);
      return response.end(json.replace('"9007199254740993"', '9007199254740993'));
    }
    response.end(JSON.stringify({ playabilityStatus: { status: 'OK' }, videoDetails: {
      videoId: returnedId, channelId: 'UC_fixture', title: 'Local fixture video', lengthSeconds: '10',
      viewCount: '123', thumbnail: { thumbnails: [] }, author: 'Fixture', shortDescription: '',
    } }));
  });
  await f.ready();
  f.a.fail = true;
  const sessions = [];
  const info = await withYoutubeRead(f.runtime, async (youtube, context) => {
    sessions.push(context.sessionId);
    return youtube.getBasicInfo('abcdefghijk');
  }, { maxAttempts: 2 });
  assert.equal(info.basic_info.title, 'Local fixture video');
  assert.equal(info.basic_info.view_count, 123);
  assert.equal(sessions.length, 2);
  assert.notEqual(sessions[0], sessions[1]);
  assert.ok(requests[0].url.startsWith('/youtubei/v1/player'));
  assert.equal(requests[0].body.videoId, 'abcdefghijk');
  assert.ok(f.b.destinations.includes('www.youtube.com:443'));
  returnedId = 'zzzzzzzzzzz';
  await rejectsCode(readYoutubeVideo(f.runtime, 'abcdefghijk'), 'YOUTUBE.IDENTITY_MISMATCH');
  metricsMode = true;
  const firstMetricsRequest = requests.length;
  const mapped = await readYoutubeVideoMetrics(f.runtime, metricsTarget);
  assert.equal(mapped.complete, true);
  assert.equal(mapped.observation.view.value, '9007199254740993');
  assert.equal(mapped.observation.like.value, '1234');
  assert.equal(mapped.observation.comment.value, '5678');
  assert.equal(Object.hasOwn(mapped.observation, 'page'), false);
  const metricRequests = requests.slice(firstMetricsRequest);
  assert.equal(metricRequests.length, 2);
  assert.deepEqual(metricRequests.map(r => new URL(r.url, 'https://www.youtube.com').pathname).sort(),
    ['/youtubei/v1/next', '/youtubei/v1/player']);
  assert.ok(metricRequests.every(r => r.body.videoId === metricsTarget.sourceContentId && !r.body.continuation));
});

test('transport makes only one attempt; collection must explicitly enable retry', async t => {
  const f = await fixture(t);
  await f.ready();
  f.a.fail = true;
  const bBefore = f.b.connections;
  let calls = 0;
  await rejectsCode(f.runtime.runRead('fixture', async context => {
    calls++;
    return context.fetch('http://fixture.test/single');
  }), 'PROXY.NETWORK_FAILED');
  assert.equal(calls, 1);
  assert.equal(f.b.connections, bBefore);
  f.b.fail = true;
  await rejectsCode(runReadStep(f.runtime, 'fixture', async context => {
    calls++;
    return context.fetch('http://fixture.test/default-single');
  }), 'PROXY.NETWORK_FAILED');
  assert.equal(calls, 2);
});

test('transport returns HTTP status without deciding target cooldown; collection owns that decision', async t => {
  const f = await fixture(t);
  await f.ready();
  f.handler((_request, response) => { response.writeHead(429); response.end('limited'); });
  assert.equal(await f.runtime.runRead('fixture', async context => {
    const response = await context.fetch('http://fixture.test/raw-limited');
    assert.deepEqual(f.pool.snapshot().proxies[0].targetCooldowns, {});
    assert.throws(() => classifyHttpResponse(context, response), { code: 'TARGET.RATE_LIMITED' });
    assert.ok(f.pool.snapshot().proxies[0].targetCooldowns.fixture);
    return response.status;
  }), 429);
});
