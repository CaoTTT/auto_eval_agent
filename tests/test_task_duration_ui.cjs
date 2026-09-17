const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let app, mounted, stream;
  let now = 1800000000000;
  let queueRunning = null;
  let historyRows = [];
  const timers = [];
  class LocalDate extends Date { static now() { return now; } }
  const timing = (elapsed, running, measured = 1000) => ({ elapsed_s: elapsed, running,
    started_at: elapsed == null ? null : 100, finished_at: running || elapsed == null ? null : 100 + elapsed,
    measured_at: measured, incomplete: false });
  const snapshots = {
    live: { task_id: 'live', mode: 'compare', status: 'running', items: [], results: [], task_timing: timing(125, true) },
    queued: { task_id: 'queued', mode: 'compare', status: 'queued', items: [], results: [], task_timing: timing(null, false) },
    legacy: { task_id: 'legacy', mode: 'compare', status: 'done', items: [], results: [], created_at: 10, updated_at: 9000 },
    repair: { task_id: 'repair', mode: 'compare', status: 'done', repair_status: 'running', items: [], results: [], task_timing: timing(150, false) },
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
    async fetch(url) { return { ok: true, json: async () => url === '/api/queue' ? { running: queueRunning }
      : url.startsWith('/api/history?') ? { items: historyRows } : snapshots[url.replace('/api/history/', '')] || {} }; },
  });
  await mounted();
  await app.loadHistoryTask('live');
  assert.equal(app.formatTaskDuration(app.selectedTaskTiming.value), '2 分 5 秒');
  now += 5000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 130, 'live elapsed uses the received duration, not server/browser clock differences');
  stream.emit('replay_state', { results: [], item_progress: {}, progress: 0, task_timing: timing(132, true, 1001) });
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 132, 'reconnection restores server elapsed');
  stream.emit('done', { summary: { total: 0 }, task_timing: timing(150, false, 1002) });
  now += 60000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 150, 'completed duration remains frozen');

  await app.loadHistoryTask('queued');
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), null);
  now += 120000; timers[0]();
  stream.emit('replay_state', { status: 'running', results: [], item_progress: {}, progress: 0, task_timing: timing(0, true, 1100) });
  assert.equal(app.selectedTaskStatus.value, 'running', 'replay restores a start event missed while subscribing');
  now += 2000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 2, 'time queued is not execution time');
  stream.emit('replay_state', { results: [], item_progress: {}, progress: 0, task_timing: timing(null, false, 1000) });
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 2, 'a late older snapshot cannot reset a started timer');
  stream.emit('cancelled', { task_timing: timing(2.5, false, 1101) });
  now += 60000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 2.5);

  await app.loadHistoryTask('legacy');
  assert.equal(app.formatTaskDuration(app.selectedTaskTiming.value), '未记录', 'legacy timestamps must not manufacture execution time');
  await app.loadHistoryTask('repair');
  stream.emit('retry_start', { task_timing: timing(150, false) });
  now += 3600000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.selectedTaskTiming.value), 150, 'repair does not inflate original task time');
  assert.equal(app.formatTaskDuration({ ...timing(3661, false), incomplete: true }), '1 小时 1 分 1 秒（截至中断前）');
  assert.equal(app.formatTaskDuration(timing(0, false)), '0 秒');
  assert.equal(app.formatTaskDuration({ elapsed_s: NaN }), '未记录');
  for (let n = 0; n < 20; n++) await Promise.resolve();
  historyRows = [{ task_id: 'background', status: 'running', note: 'saved', task_timing: timing(10, true, 2000) }];
  queueRunning = { task_id: 'background', kind: 'initial', status: 'running', task_timing: timing(10, true, 2000) };
  await app.loadQueue();
  app.historyNoteEditing.value.background = true;
  app.historyNoteDrafts.value.background = 'unsaved note';
  now += 3000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.historyItems.value[0].task_timing), 13);
  historyRows = [{ task_id: 'background', status: 'done', task_timing: timing(14, false, 2001) }];
  queueRunning = null;
  await app.loadQueue();
  now += 60000; timers[0]();
  assert.equal(app.taskElapsedSeconds(app.historyItems.value[0].task_timing), 14, 'unselected tasks freeze in history when they leave the queue');
  assert.equal(app.historyNoteDrafts.value.background, 'unsaved note', 'automatic timing refresh preserves an in-progress note edit');
  console.log('Task duration UI: running, queue exclusion, completed/cancelled freeze, replay, legacy and repairs passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
