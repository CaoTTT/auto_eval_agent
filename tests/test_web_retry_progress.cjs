// Exercise actual UI handlers with fake HTTP/SSE; no browser or model API required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, '');

async function check(evidenceMode, productCount, legacy = false) {
  let app;
  const streams = [];
  const ticks = [];
  const old = {item_index: 0, request_id: 'old', status: 'error', stage_rank: 3, started_at: 1000, finished_at: 9000, updated_at: '2026-09-10T10:00:00', sequence: 100};
  const snapshot = {
    task_id: 'task', mode: legacy ? 'rich_content' : 'compare', status: 'done',
    items: [{id: 'q0', query: 'query', evidence_mode: evidenceMode, product_count: productCount}],
    results: [{index: 0, error: 'old failure', latency_s: 300}],
    item_progress: {0: old}, progress_events: {0: [old, old]},
  };
  class FakeStream {
    constructor(url) { this.url = url; this.handlers = {}; streams.push(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
    emit(name, data) { return this.handlers[name]?.({data: JSON.stringify(data)}); }
  }
  vm.runInNewContext(source, {
    ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
    onMounted() {}, onUnmounted() {}, nextTick(fn) {if (fn) ticks.push(fn);},
    createApp(options) {app = options.setup(); return {mount() {}};}, EventSource: FakeStream,
    alert(message) {throw Error(message);}, console,
    async fetch(url) {
      const data = url === '/api/history/task' ? snapshot
        : url.endsWith('/retries') ? {selected: 1, accepted_indexes: [0], status: 'queued', retry_id: 'retry'} : {};
      return {ok: true, json: async () => data};
    },
  });
  await app.loadHistoryTask('task');
  assert.equal(app.progressEvents.value[0].length, 1, 'loaded history is keyed and deduplicated');
  assert.equal(app.progressEvents.value[0][0]._key, 'seq:100');
  await app.retryFailedCases();
  const stream = streams.at(-1);
  assert.match(stream.url, /compact=true/);
  const current = {...old, request_id: 'retry', status: 'running', module: '任务', sequence: 101, started_at: Date.now() - 1000};
  delete current.stage_rank;
  delete current.finished_at;
  stream.emit('item_progress', current);
  assert.equal(app.itemProgress.value[0].stage_rank, 0);
  assert.equal(app.itemProgress.value[0].finished_at, undefined);
  assert.ok(app.pagedProgressRows.value[0].elapsedSeconds < 5, 'retry clock must not show the old 300 seconds');
  stream.emit('item_progress', old);
  assert.equal(app.itemProgress.value[0].request_id, 'retry', 'late old events cannot roll progress back');
  stream.emit('replay_state', {results: snapshot.results, item_progress: {0: current}, progress: 1, repair_status: 'running'});
  assert.equal(app.pagedProgressRows.value[0].status, 'running', 'old failure result cannot overwrite live retry state');
  const events = Array.from({length: 100}, (_,i) => ({...old, sequence: i + 1}));
  stream.emit('progress_history', {item_index: 0, events});
  for (const event of events) stream.emit('progress_event', event);
  assert.equal(app.progressEvents.value[0].length, 100);
  assert.equal(new Set(app.progressEvents.value[0].map(e => e._key)).size, 100);
  for (let seq = 102; seq <= 110; seq++) stream.emit('item_progress', {...current, sequence: seq});
  assert.equal(app.progressEvents.value[0].length, 100);
  stream.emit('retry_result', {index: 0, status: 'failed'});
  assert.equal(app.itemProgress.value[0].status, 'error');
  assert.equal(app.results.value[0].error, 'old failure', 'failed retry must retain original result');

  // A closed connection to the SAME task must not terminate the new attempt.
  await app.retryFailedCases();
  assert.equal(stream.closed, true);
  stream.emit('done', {retry: {status: 'completed'}});
  assert.equal(app.repairStatus.value, 'queued');
  streams.at(-1).emit('result', {progress: 1, result: {index: 0, answer: 'success'}});
  assert.equal(app.results.value.length, 1);
  assert.equal(app.results.value[0].answer, 'success');

  // Reading one page may touch only its 10 queries, even with a large task.
  let reads = 0;
  app.items.value = Array.from({length: 2000}, (_,i) => ({id: `q${i}`, get query() {reads++; return 'q';}}));
  app.progressPage.value = 2;
  assert.equal(app.progressPageCount.value, 200);
  assert.equal(app.pagedProgressRows.value[0].index, 10);
  assert.equal(reads, 10);

  // DOM event.currentTarget is cleared before Vue's nextTick callback executes.
  const panel = {scrollHeight: 100, scrollTop: 0};
  const domEvent = {currentTarget: {open: true, querySelector: () => panel}};
  app.scrollProgressLog(domEvent, 0);
  domEvent.currentTarget = null;
  while (ticks.length) ticks.shift()();
  assert.equal(app.expandedProgressLogs.value[0], true);
  assert.equal(panel.scrollTop, 100);
}

(async () => {
  for (const mode of ['long_screenshot', 'video_frames']) for (const count of [2, 3]) await check(mode, count);
  await check('video_frames', 1, true);
  console.log('5 retry UI scenarios passed (2/3 products, screenshots/video, legacy task)');
})().catch(error => {console.error(error); process.exitCode = 1;});
