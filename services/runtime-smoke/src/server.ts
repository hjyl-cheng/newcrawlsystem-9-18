import { createServer } from 'node:http';

export function createRuntimeServer(revision: string, version: string) {
  let ready = true;
  const server = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    const path = req.url?.split('?')[0];
    let body: unknown;
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.statusCode = 405;
      res.setHeader('Allow', 'GET, HEAD');
      body = { error: 'method_not_allowed' };
    } else if (path === '/health/live') {
      body = { status: 'ok' };
    } else if (path === '/health/ready') {
      res.statusCode = ready ? 200 : 503;
      body = { status: ready ? 'ready' : 'draining' };
    } else if (path === '/version') {
      body = { service: 'runtime-smoke', version, revision };
    } else {
      res.statusCode = 404;
      body = { error: 'not_found' };
    }
    res.end(req.method === 'HEAD' ? undefined : JSON.stringify(body));
  });
  server.requestTimeout = 10_000;
  server.headersTimeout = 5_000;
  server.keepAliveTimeout = 5_000;
  return { server, drain: () => { ready = false; } };
}
