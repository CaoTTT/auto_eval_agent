const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let app;
const requests = [];
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
  ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console,
  createApp(options) {app = options.setup(); return {mount() {}};},
  fetch(url, options) {return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));},
  confirm: () => true, alert() {},
});
function respond(url, data, ok = true) {
  const index = requests.findIndex(request => request.url === url);
  assert.ok(index >= 0, `Missing request: ${url}`);
  requests.splice(index, 1)[0].resolve({ok, json: async () => data});
}
function pageData(page, total = 21) {
  const start = (page - 1) * 10;
  return {page, total, page_size: 10, items: Array.from({length: Math.min(10, total - start)}, (_, index) => ({
    task_id: `task-${start + index}`, status: 'done', note: 'saved',
  }))};
}
async function tick() {for (let index = 0; index < 10; index++) await Promise.resolve();}

(async () => {
  const first = app.loadHistory();
  respond('/api/history?page=1', pageData(1)); await first;
  assert.equal(app.historyItems.value.length, 10);
  assert.equal(app.historyTotal.value, 21);
  assert.equal(app.historyPageCount.value, 3);
  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value['task-0'] = 'unfinished note';
  app.taskId.value = 'selected-result';

  const second = app.loadHistory(2);
  // A queue refresh during navigation must refresh the requested page.
  const refresh = app.loadHistory();
  assert.equal(requests[0].url, '/api/history?page=2');
  assert.equal(requests[1].url, '/api/history?page=2');
  respond('/api/history?page=2', pageData(2)); await second;
  assert.equal(app.historyPage.value, 1, 'superseded requests cannot replace the displayed page');
  assert.equal(app.loadingHistory.value, true);
  respond('/api/history?page=2', pageData(2)); await refresh;
  assert.equal(app.historyPage.value, 2);
  assert.equal(app.historyItems.value[0].task_id, 'task-10');
  assert.equal(app.taskId.value, 'selected-result', 'turning pages keeps the loaded result selected');

  const back = app.loadHistory(1);
  respond('/api/history?page=1', pageData(1)); await back;
  assert.equal(app.historyNoteDrafts.value['task-0'], 'unfinished note');
  assert.equal(app.historyNoteEditing.value['task-0'], true);

  const stale = app.loadHistory(2);
  const last = app.loadHistory(3);
  respond('/api/history?page=3', pageData(3)); await last;
  respond('/api/history?page=2', pageData(2)); await stale;
  assert.equal(app.historyPage.value, 3);
  assert.equal(app.historyItems.value.length, 1);

  app.editHistoryNote(app.historyItems.value[0]);
  const deletion = app.delHistory('task-20');
  assert.equal(requests[0].options.method, 'DELETE');
  respond('/api/history/task-20', {ok: true}); await tick();
  respond('/api/history?page=3', pageData(2, 20)); await deletion;
  assert.equal(app.historyPage.value, 2, 'accept the server-adjusted page after deleting the last row');
  assert.equal(app.historyPageCount.value, 2);
  assert.equal(app.historyNoteDrafts.value['task-20'], undefined);

  const failure = app.loadHistory(1);
  respond('/api/history?page=1', {}, false); await failure;
  assert.match(app.historyError.value, /历史记录加载失败/);
  assert.equal(app.historyPage.value, 2, 'a failed request keeps the previous page and rows');
  assert.equal(app.loadingHistory.value, false);
  const retry = app.loadHistory();
  respond('/api/history?page=2', pageData(2, 20)); await retry;
  assert.equal(app.historyError.value, '');

  const empty = app.loadHistory();
  respond('/api/history?page=2', {items: [], total: 0, page: 1, page_size: 10}); await empty;
  assert.equal(app.historyItems.value.length, 0);
  assert.equal(app.historyPage.value, 1);
  assert.equal(app.historyTotal.value, 0);
  assert.equal(requests.length, 0);
  console.log('History pagination: navigation, refresh races, notes, deletion, errors and empty state passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
