const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');

async function main() {
  let app, stream, mounted, pendingControl;
  const requests = [];
  const snapshot = {task_id: 'task-one', mode: 'rich_content', status: 'running', active_runs: 1,
    items: [{query: 'a'}, {query: 'b'}], results: [{index: 0}], options: {concurrency: 4}};
  class Stream {
    constructor() { stream = this; this.handlers = {}; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
    emit(name, value) { return this.handlers[name]?.({data: JSON.stringify(value)}); }
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
    ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
    onMounted(fn) {mounted = fn;}, onUnmounted() {}, nextTick() {}, console,
    window: {setInterval() {}, clearInterval() {}}, document: {}, EventSource: Stream,
    createApp(options) {app = options.setup(); return {mount() {}};},
    fetch: async (url, options) => {
      if (options?.method === 'POST') {
        requests.push({url, options});
        if (pendingControl) return new Promise(resolve => {pendingControl.resolve = resolve;});
        if (url.endsWith('/pause')) snapshot.execution_control = {state: 'pausing'};
        if (url.endsWith('/resume')) {snapshot.status = 'queued'; snapshot.active_runs = 1; snapshot.execution_control = {state: 'queued', concurrency: 9};}
        return {ok: true, json: async () => ({status: snapshot.status})};
      }
      return {ok: true, json: async () => url.startsWith('/api/history/') ? {...snapshot}
        : url.startsWith('/api/history?') ? {items: []} : {}};
    },
  });
  await mounted();
  await app.loadHistoryTask('task-one');
  assert.equal(app.canPauseTask.value, true);
  await app.controlTask('pause');
  assert.equal(app.isPausing.value, true);
  assert.equal(app.canResumeTask.value, false);
  assert.equal(app.results.value.length, 1, 'pause must preserve rendered results');
  snapshot.status = 'paused'; snapshot.active_runs = 0; snapshot.execution_control = {state: 'paused'};
  stream.emit('paused', {execution_control: {state: 'paused'}, summary: {done: 1}});
  assert.equal(app.selectedTaskStatus.value, 'paused');
  assert.equal(app.canResumeTask.value, true);
  assert.equal(stream.closed, true);
  app.resumeConcurrency.value = 9; app.resumeFailed.value = true;
  await app.controlTask('resume');
  const body = JSON.parse(requests.at(-1).options.body);
  assert.equal(body.concurrency, 9);
  assert.equal(body.include_failed, true);
  assert.ok(body.idempotency_key);
  assert.equal(app.selectedTaskStatus.value, 'queued');
  assert.equal(app.results.value.length, 1);

  snapshot.status = 'paused'; snapshot.active_runs = 0;
  await app.loadHistoryTask('task-one');
  app.resumeConcurrency.value = 0;
  const count = requests.length;
  await app.controlTask('resume');
  assert.equal(requests.length, count, 'invalid concurrency must not send a request');
  assert.match(app.runError.value, /1–128/);

  app.resumeConcurrency.value = 5;
  pendingControl = {};
  const pending = app.controlTask('resume');
  await Promise.resolve();
  snapshot.task_id = 'task-two'; snapshot.status = 'done'; snapshot.results = [{index: 0}, {index: 1}];
  await app.loadHistoryTask('task-two');
  pendingControl.resolve({ok: true, json: async () => ({status: 'queued'})});
  await pending;
  assert.equal(app.taskId.value, 'task-two', 'late action response must not switch the selected task');
  assert.equal(app.selectedTaskStatus.value, 'done');
  assert.equal(app.controlSubmitting.value, false);
  console.log('Pause/resume UI: drain state, preserved results, concurrency, validation, SSE and navigation race passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
