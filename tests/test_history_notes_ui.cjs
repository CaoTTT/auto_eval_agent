const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  let app;
  const requests = [], alerts = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8')
    .replace(/^import[^\n]+\n/, ''), {
    ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
    onMounted() {}, onUnmounted() {}, nextTick() {}, console,
    createApp(options) { app = options.setup(); return { mount() {} }; },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
    confirm: () => true, alert: message => alerts.push(message),
  });
  function take(url) {
    const index = requests.findIndex(request => request.url === url);
    assert.ok(index >= 0, `Missing request: ${url}`);
    return requests.splice(index, 1)[0];
  }
  function respond(url, data, ok = true) { take(url).resolve({ ok, json: async () => data }); }
  async function load(rows, page = 1) {
    const pending = app.loadHistory(page);
    respond(`/api/history?page=${page}`, { page, total: 20, items: rows });
    await pending;
  }
  return { app, requests, alerts, take, respond, load };
}

async function main() {
  const { app, requests, alerts, take, respond, load } = harness();
  const rows = () => [{ task_id: 'active', note: '旧备注', status: 'running' },
    { task_id: 'other', note: '其他任务备注', status: 'done' }];
  await load(rows());
  let original = app.historyItems.value[0];
  app.editHistoryNote(original);
  assert.equal(app.historyNoteEditing.value.active, true);
  app.historyNoteDrafts.value.active = '  中文批跑备注  ';
  const saving = app.saveHistoryNote(original);
  assert.equal(app.historyNoteSaving.value.active, true);
  assert.equal(JSON.parse(requests[0].options.body).note, '中文批跑备注');
  assert.equal(requests[0].options.method, 'PATCH');
  await app.saveHistoryNote(original);
  assert.equal(requests.length, 1, 'Enter and button clicks cannot send duplicate saves');
  app.cancelHistoryNote(original);
  app.editHistoryNote(original);
  assert.equal(app.historyNoteDrafts.value.active, '  中文批跑备注  ', 'saving blocks cancel/edit from resetting the draft');
  assert.equal(app.historyNoteEditing.value.active, true);

  app.queueState.value = { running: { task_id: 'active', status: 'running' }, queued: [] };
  const refreshQueue = app.loadQueue();
  respond('/api/queue', { running: { task_id: 'active', status: 'running' }, queued: [] });
  await refreshQueue;
  assert.notEqual(app.historyItems.value[0], original, 'queue polling replaces the active row');
  respond('/api/history/active/note', { ok: true, note: '中文批跑备注' });
  await saving;
  assert.equal(app.historyItems.value[0].note, '中文批跑备注', 'save updates the visible row after queue replacement');
  assert.equal(app.historyItems.value[1].note, '其他任务备注');
  assert.equal(app.historyNoteDrafts.value.active, '中文批跑备注');
  assert.equal(app.historyNoteEditing.value.active, false);
  assert.equal(app.historyNoteSaving.value.active, undefined);

  original = app.historyItems.value[0];
  app.editHistoryNote(original);
  app.historyNoteDrafts.value.active = '第二次备注';
  const oldList = app.loadHistory();
  const secondSave = app.saveHistoryNote(original);
  respond('/api/history/active/note', { note: '第二次备注' });
  await secondSave;
  respond('/api/history?page=1', { page: 1, total: 20, items: rows() });
  await oldList;
  assert.equal(app.historyItems.value[0].note, '第二次备注', 'a pre-save list response cannot restore the old note');
  assert.equal(app.historyNoteDrafts.value.active, '第二次备注');
  assert.equal(app.historyNoteEditing.value.active, undefined);
  await load([{ ...rows()[0], note: '其他客户端更新' }, rows()[1]]);
  assert.equal(app.historyItems.value[0].note, '其他客户端更新', 'a post-save server read remains authoritative');

  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value.active = '  ';
  const clearing = app.saveHistoryNote(app.historyItems.value[0]);
  assert.equal(JSON.parse(requests[0].options.body).note, '');
  respond('/api/history/active/note', { note: '' });
  await clearing;
  assert.equal(app.historyItems.value[0].note, '', 'clearing a note is a valid save');

  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value.active = '未保存草稿';
  const networkFailure = app.saveHistoryNote(app.historyItems.value[0]);
  take('/api/history/active/note').reject(new Error('网络断开'));
  await networkFailure;
  assert.equal(app.historyNoteEditing.value.active, true);
  assert.equal(app.historyNoteDrafts.value.active, '未保存草稿');
  assert.equal(app.historyNoteSaving.value.active, undefined);
  assert.match(alerts.at(-1), /备注保存失败.*网络断开.*重试/);
  const rejected = app.saveHistoryNote(app.historyItems.value[0]);
  respond('/api/history/active/note', { detail: '备注保存失败' }, false);
  await rejected;
  assert.equal(app.historyNoteDrafts.value.active, '未保存草稿');
  assert.equal(app.historyNoteEditing.value.active, true);
  assert.match(alerts.at(-1), /备注保存失败/);
  const invalid = app.saveHistoryNote(app.historyItems.value[0]);
  respond('/api/history/active/note', {});
  await invalid;
  assert.equal(app.historyNoteEditing.value.active, true, 'invalid success bodies do not silently clear a draft');
  const retry = app.saveHistoryNote(app.historyItems.value[0]);
  respond('/api/history/active/note', { note: '未保存草稿' });
  await retry;
  assert.equal(app.historyItems.value[0].note, '未保存草稿');
  assert.equal(app.historyNoteEditing.value.active, false);

  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value.active = '翻页时保存';
  const pageSave = app.saveHistoryNote(app.historyItems.value[0]);
  const pageRequest = app.loadHistory(2);
  respond('/api/history?page=2', { page: 2, total: 20,
    items: [{ task_id: 'page-two', note: '第二页备注' }] });
  await pageRequest;
  assert.equal(app.historyNoteDrafts.value.active, '翻页时保存', 'pagination keeps an in-flight draft');
  respond('/api/history/active/note', { note: '翻页时保存' });
  await pageSave;
  assert.equal(app.historyItems.value[0].note, '第二页备注', 'saving an off-page task never updates another task');
  await load([{ ...rows()[0], note: '翻页时保存' }, rows()[1]]);
  assert.equal(app.historyItems.value[0].note, '翻页时保存');

  // A task id belongs in one URL path segment, even if imported with punctuation.
  app.historyItems.value = [{ task_id: 'batch #1', note: '' }];
  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value['batch #1'] = '特殊编号';
  const encoded = app.saveHistoryNote(app.historyItems.value[0]);
  respond('/api/history/batch%20%231/note', { note: '特殊编号' });
  await encoded;
  assert.equal(app.historyItems.value[0].note, '特殊编号');
  assert.equal(requests.length, 0);

  const html = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/index.html'), 'utf8');
  assert.match(html, /v-model="historyNoteDrafts\[h\.task_id\]"\s+:disabled="historyNoteSaving\[h\.task_id\]"/);
  assert.match(html, /<button[^>]*:disabled="historyNoteSaving\[h\.task_id\]"[^>]*@click="saveHistoryNote\(h\)"/);
  assert.match(html, /historyNoteSaving\[h\.task_id\] \? '保存中…'/);
  console.log('History notes: editing, row refresh races, stale list responses, duplicate saves, clear, failures/retry and pagination passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
