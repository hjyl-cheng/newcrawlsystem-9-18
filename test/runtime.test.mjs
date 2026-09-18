import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { createRuntimeServer } from '../dist/services/runtime-smoke/src/server.js';

test('health distinguishes draining from liveness and exposes build identity', async (t) => {
  const { server, drain } = createRuntimeServer('abc123', '0.1.0');
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(`${base}/health/ready`)).status, 200);
  assert.deepEqual(await (await fetch(`${base}/version`)).json(), {
    service: 'runtime-smoke', version: '0.1.0', revision: 'abc123'
  });
  drain();
  assert.equal((await fetch(`${base}/health/ready`)).status, 503);
  assert.equal((await fetch(`${base}/health/live`)).status, 200);
  assert.equal((await fetch(`${base}/health/live`, { method: 'POST' })).status, 405);
  assert.equal((await fetch(`${base}/not-an-api`)).status, 404);
});

test('invalid port fails startup', async () => {
  const child = spawn(process.execPath, ['dist/services/runtime-smoke/src/main.js'], {
    env: { ...process.env, PORT: '0' }, stdio: 'ignore'
  });
  assert.equal((await once(child, 'exit'))[0], 1);
});

test('SIGTERM withdraws readiness, drains and exits successfully', { timeout: 15000 }, async (t) => {
  const socket = createServer().listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const child = spawn(process.execPath, ['dist/services/runtime-smoke/src/main.js'], {
    env: { ...process.env, PORT: String(port) }, stdio: ['ignore', 'pipe', 'pipe']
  });
  t.after(() => { if (child.exitCode === null) child.kill('SIGKILL'); });
  const exited = once(child, 'exit');
  await once(child.stdout, 'data');
  const draining = once(child.stdout, 'data');
  child.kill('SIGTERM');
  await draining;
  assert.equal((await fetch(`http://127.0.0.1:${port}/health/ready`)).status, 503);
  assert.equal((await fetch(`http://127.0.0.1:${port}/health/live`)).status, 200);
  assert.equal((await exited)[0], 0);
});
