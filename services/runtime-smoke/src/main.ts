import { createRuntimeServer } from './server.js';

const portText = process.env.PORT ?? '8080';
if (!/^\d+$/.test(portText) || Number(portText) < 1 || Number(portText) > 65535) {
  throw new Error('PORT must be an integer between 1 and 65535');
}
const revision = process.env.APP_REVISION ?? 'development';
const version = process.env.APP_VERSION ?? 'development';
const { server, drain } = createRuntimeServer(revision, version);
const log = (event: string) => console.log(JSON.stringify({ event, service: 'runtime-smoke', revision }));
server.listen(Number(portText), '0.0.0.0', () => log('listening'));

let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  drain();
  log('draining');
  // Allow endpoint removal to propagate while finishing accepted requests.
  setTimeout(() => {
    server.close(() => {
      log('stopped');
      process.exit(0);
    });
  }, 3_000);
  setTimeout(() => { server.closeAllConnections(); process.exit(1); }, 10_000).unref();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
