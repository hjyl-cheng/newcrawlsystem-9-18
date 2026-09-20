// Test-only orchestration. No crawler tasks, network requests, or business writes.
const { proxyActivities, sleep } = require('@temporalio/workflow');
const { echoValidation } = proxyActivities({
  startToCloseTimeout: '10 seconds',
  scheduleToCloseTimeout: '60 seconds',
  retry: { initialInterval: '1 second', maximumAttempts: 2 },
});

exports.validationRoundTrip = async function validationRoundTrip(marker) {
  const first = await echoValidation(marker);
  await sleep('3 seconds'); // Durable timer, restored when the test worker restarts.
  const second = await echoValidation(marker);
  return { marker: second, first, kind: 'TEMPORAL_VALIDATION_ONLY' };
};
