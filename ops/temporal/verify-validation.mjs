#!/usr/bin/env node
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Client, Connection } from '@temporalio/client';
import { Worker, NativeConnection, Runtime, DefaultLogger } from '@temporalio/worker';
import { activityInfo } from '@temporalio/activity';

// Deliberately loopback-only: run through an operator-owned kubectl port-forward.
const address = process.env.TEMPORAL_ADDRESS ?? '127.0.0.1:17233';
assert.match(address, /^127\.0\.0\.1:[0-9]{1,5}$/);
const namespace = 'crawl-validation';
// SDK metadata may contain activity task tokens; report only level/message.
Runtime.install({ logger: new DefaultLogger('WARN', ({ level, message }) => {
  console.error(JSON.stringify({ level, message }));
}) });
const connection = await Connection.connect({ address, connectTimeout: '10 seconds' });
let native, worker, run, handle;
const deadline = setTimeout(() => { console.error('Temporal validation deadline exceeded'); process.exit(1); }, 180_000);
try {
  await connection.withDeadline(Date.now() + 10_000, async () => {
    try { await connection.workflowService.describeNamespace({ namespace }); }
    catch (error) {
      if (error.code !== 5) throw error;
      await connection.workflowService.registerNamespace({ namespace,
        description: 'Isolated orchestration validation; no crawler data',
        workflowExecutionRetentionPeriod: { seconds: 86400 } });
    }
  });
  const client = new Client({ connection, namespace });
  const marker = 'temporal-validation-' + randomUUID();
  const taskQueue = marker;
  // Namespace registration propagates through server caches asynchronously.
  // Retry only the definitive "not found" response, using the same workflow ID.
  for (let i = 0; i < 80; i++) {
    try {
      handle = await client.workflow.start('validationRoundTrip', {
        workflowId: marker, taskQueue, args: [marker], workflowExecutionTimeout: '2 minutes',
      });
      break;
    } catch (error) {
      if (error.name !== 'NamespaceNotFoundError' || i === 79) throw error;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  assert.equal((await handle.describe()).status.name, 'RUNNING');
  console.log(JSON.stringify({ phase: 'queued-without-worker', workflowId: marker }));

  native = await NativeConnection.connect({ address });
  let calls = 0, failures = 0;
  const options = {
    connection: native, namespace, taskQueue,
    workflowsPath: fileURLToPath(new URL('./validation-workflows.cjs', import.meta.url)),
    activities: { async echoValidation(value) {
      assert.equal(value, marker);
      calls++;
      if (activityInfo().attempt === 1) { failures++; throw new Error('Intentional validation retry'); }
      return value;
    } },
    maxConcurrentActivityTaskExecutions: 2, maxConcurrentWorkflowTaskExecutions: 2,
    shutdownGraceTime: '5 seconds',
  };
  worker = await Worker.create(options);
  run = worker.run();
  // Observe a persisted timer before stopping, rather than guessing from an activity callback.
  let sawTimer = false;
  for (let i = 0; i < 120; i++) {
    const history = await handle.fetchHistory();
    if (history.events.some(event => event.timerStartedEventAttributes)) { sawTimer = true; break; }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert.ok(sawTimer, 'Workflow did not persist its timer');
  worker.shutdown(); await run; worker = undefined; run = undefined;
  console.log(JSON.stringify({ phase: 'worker-stopped-with-persisted-timer', workflowId: marker }));
  worker = await Worker.create(options); run = worker.run();
  const result = await handle.result();
  assert.deepEqual(result, { marker, first: marker, kind: 'TEMPORAL_VALIDATION_ONLY' });
  assert.equal(calls, 4); assert.equal(failures, 2); // Completed first activity was not rerun on replay.
  const history = await handle.fetchHistory();
  assert.equal(history.events.filter(event => event.activityTaskCompletedEventAttributes).length, 2);
  assert.equal(history.events.filter(event => event.timerFiredEventAttributes).length, 1);
  assert.equal((await handle.describe()).status.name, 'COMPLETED');
  worker.shutdown(); await run; worker = undefined; run = undefined;
  // A new connection can retrieve the completed result with no polling worker.
  const reopened = await Connection.connect({ address, connectTimeout: '10 seconds' });
  try {
    const recovered = new Client({ connection: reopened, namespace });
    assert.deepEqual(await recovered.workflow.getHandle(marker).result(), result);
  } finally { await reopened.close(); }
  console.log(JSON.stringify({ phase: 'passed', namespace, workflowId: marker,
    activityInvocations: calls, intentionalFailures: failures, resultRecovered: true }));
} catch (error) {
  if (handle) await handle.terminate('Validation failed; cleanup own workflow only').catch(() => {});
  throw error;
} finally {
  clearTimeout(deadline);
  if (worker) { worker.shutdown(); await run; }
  await native?.close(); await connection.close();
}
