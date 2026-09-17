const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let app, stream, mounted;
  let now = Date.now();
  const timers = [];
  class LocalDate extends Date { static now() { return now; } }
  const timing = { total_s: 1200, model_s: 70, request_wait_s: 1010, media_s: 10,
    media_queue_s: 100, retry_wait_s: 5, other_s: 5, attempts: 2,
    active_stage: null, finished: true, measured_at: now - 1200000 };
  const snapshot = { task_id: 'timing', mode: 'compare', status: 'running',
    items: [{ id: 'q0', query: 'Timing example' }], results: [],
    item_progress: { 0: { item_index: 0, request_id: 'run0', status: 'running', sequence: 1,
      started_at: now - 1200000, timings: { ...timing, active_stage: 'request_wait', finished: false } } },
  };
  class FakeStream {
    constructor() { stream = this; this.handlers = {}; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() {}
    emit(name, data) { return this.handlers[name]?.({ data: JSON.stringify(data) }); }
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
    ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
    onMounted(fn) { mounted = fn; }, onUnmounted() {}, nextTick() {}, console, Date: LocalDate,
    window: { setInterval(fn) { timers.push(fn); return timers.length; }, clearInterval() {} },
    document: { hidden: false },
    createApp(options) { app = options.setup(); return { mount() {} }; }, EventSource: FakeStream,
    async fetch(url) { return { ok: true, json: async () => url === '/api/history/timing' ? snapshot : {} }; },
  });
  await mounted();
  await app.loadHistoryTask('timing');
  assert.equal(app.pagedProgressRows.value[0].elapsedSeconds, 1200, 'server clock skew cannot inflate total duration');
  assert.equal(app.timingEntries(app.pagedProgressRows.value[0].timings, true).find(row => row.key === 'request_wait').seconds, 1010);
  now += 5000;
  timers[0]();
  const live = app.timingEntries(app.pagedProgressRows.value[0].timings, true);
  assert.equal(live.find(row => row.key === 'request_wait').seconds, 1015);
  assert.equal(app.pagedProgressRows.value[0].elapsedSeconds, 1205);
  assert.equal(live.find(row => row.key === 'model').seconds, 70, 'live queue does not inflate model duration');
  const before = app.progressEvents.value[0]?.length || 0;
  stream.emit('item_progress', { ...snapshot.item_progress[0], sequence: 2, timing_update: true });
  assert.equal(app.progressEvents.value[0]?.length || 0, before, 'timing-only updates must not duplicate log rows');
  const completed = { index: 0, latency_s: 1090, total_s: 1200, timings: timing };
  stream.emit('result', { progress: 1, result: completed });
  assert.equal(app.cell(completed, { key: 'latency_s' }), '1200秒');
  assert.equal(app.cell({ latency_s: 9 }, { key: 'latency_s' }), '9秒（旧记录）');
  assert.equal(app.timingEntries(timing, true).find(row => row.key === 'request_wait').seconds, 1010,
    'finished timing remains frozen even after time passes');
  assert.equal(app.timingStageLabel('request_wait'), '等待调用额度');
  assert.equal(app.columnWidth({ key: 'latency_s' }), 120);
  assert.ok(app.resultCols.value.some(column => column.key === 'latency_s' && column.label === '总耗时'));
  console.log('Stage timing UI: live waiting, stable completed times, legacy fallback and log deduplication passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
