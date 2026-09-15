const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function tick() { for (let i = 0; i < 12; i++) await Promise.resolve(); }

async function main() {
  let app, cleanup, resolvePacing;
  let queueRunning = null, pacingCalls = 0, failPacing = false;
  const context = {
    ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
    onMounted() {}, onUnmounted(fn) { cleanup = fn; }, nextTick() {}, console,
    document: { hidden: false }, window: { clearInterval() {} },
    createApp(options) { app = options.setup(); return { mount() {} }; },
    async fetch(url) {
      if (url === '/api/queue') return { ok: true, json: async () => ({ running: queueRunning }) };
      assert.equal(url, '/api/request-pacing');
      pacingCalls++;
      if (failPacing) return { ok: false };
      return new Promise(resolve => { resolvePacing = resolve; });
    },
  };
  const code = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8')
    .replace(/^import[^\n]*\n/gm, '');
  vm.runInNewContext(code, context);
  app.judges.value = [{ name: 'bailian', request_pacing: true }];
  app.selectedJudges.value = ['bailian'];
  app.concurrency.value = 128;

  await app.loadQueue();
  assert.equal(pacingCalls, 0, 'idle pages do not poll request pacing');
  queueRunning = { task_id: 'current', status: 'running' };
  context.document.hidden = true;
  await app.loadQueue();
  assert.equal(pacingCalls, 0, 'hidden pages do not poll request pacing');
  context.document.hidden = false;

  const first = app.loadQueue();
  await tick();
  await app.loadQueue();
  assert.equal(pacingCalls, 1, 'slow pacing requests are not duplicated by queue refreshes');
  resolvePacing({ ok: true, json: async () => ({ enabled: true, active: true,
    controller: { requests_last_second: 2, inflight: 60, target_rps: 8, token_budget: 800000 } }) });
  await first;
  assert.equal(app.pacingStatus.value.requests_last_second, 2);
  assert.equal(app.pacingStatus.value.inflight, 60);
  assert.equal(app.concurrency.value, 128, 'monitoring must not change Case capacity');
  assert.equal(app.pacingNumber(0), '0');
  assert.equal(app.pacingNumber(null), '—', 'unknown metrics cannot be shown as zero');
  assert.equal(app.pacingNumber(800000), '800,000');
  assert.equal(app.pacingWaitLabel('token_budget'), 'Token 预算');
  assert.equal(app.pacingLimitLabel('token'), 'Token 额度');

  failPacing = true;
  await app.loadQueue();
  assert.equal(app.pacingStatus.value, null, 'failed refresh clears misleading old metrics');
  assert.match(app.pacingError.value, /暂不可用/);
  assert.equal(app.queueState.value.running.task_id, 'current', 'monitor failure preserves task state');
  failPacing = false;

  const pending = app.loadQueue();
  await tick();
  queueRunning = null;
  await app.loadQueue();
  resolvePacing({ ok: true, json: async () => ({ enabled: true, active: true,
    controller: { requests_last_second: 9 } }) });
  await pending;
  assert.equal(app.pacingStatus.value, null, 'late metrics cannot revive an idle display');
  assert.equal(app.pacingError.value, '');

  queueRunning = { task_id: 'current', status: 'running' };
  app.judges.value = [{ name: 'other', request_pacing: false }];
  const before = pacingCalls;
  await app.loadQueue();
  assert.equal(pacingCalls, before, 'unsupported providers do not fetch pacing status');
  app.judges.value = [{ name: 'bailian', request_pacing: true }];
  const closing = app.loadQueue();
  await tick();
  cleanup();
  resolvePacing({ ok: true, json: async () => ({ enabled: true, active: true,
    controller: { requests_last_second: 3 } }) });
  await closing;
  assert.equal(app.pacingStatus.value, null, 'unmounted UI ignores late status responses');
  console.log('Pacing polling, deduplication, error recovery, separate capacity and stale-response checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
