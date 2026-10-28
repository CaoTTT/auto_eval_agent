const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');

let app;
const requests = [];
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8')
  .replace(/^import[^\n]+\n/, ''), {
  ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console,
  createApp(options) { app = options.setup(); return { mount() {} }; },
  fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  confirm: () => true, alert(message) { throw Error(message); },
});
const params = request => new URL(request.url, 'http://test').searchParams;
function take(url) {
  const index = requests.findIndex(request => request.url === url);
  assert.ok(index >= 0, `Missing request: ${url}`);
  return requests.splice(index, 1)[0];
}
function respond(url, data, ok = true) { take(url).resolve({ ok, json: async () => data }); }
function historyData(id = 'matching', page = 1, total = 21, note = 'saved') {
  return { page, total, items: [{ task_id: id, status: 'done', note }] };
}
async function tick() { for (let index = 0; index < 12; index++) await Promise.resolve(); }

async function main() {
  const initial = app.loadHistory();
  assert.equal(requests[0].url, '/api/history?page=1', 'unfiltered legacy URL remains unchanged');
  respond('/api/history?page=1', historyData('first')); await initial;
  app.taskId.value = 'loaded-result';
  app.comparisonTaskIds.value = ['selected-A', 'selected-B'];
  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value.first = '跨页未保存的备注';

  const profile = 'qa_competitor_compare@0.2-simplified-thinking-exposure';
  Object.assign(app.historySearchDraft.value, {
    dataset_name: '  数据 A & 中文.xlsx  ', evaluation_profile: profile,
    start_time: '2026-10-28T09:30:01', end_time: '2026-10-28T19:30:59',
  });
  const unsent = app.loadHistory();
  assert.equal(requests[0].url, '/api/history?page=1', 'typing does not apply draft filters on automatic refresh');
  respond('/api/history?page=1', historyData('first')); await unsent;
  assert.equal(app.historyNoteDrafts.value.first, '跨页未保存的备注');

  const search = app.applyHistorySearch();
  const query = params(requests[0]);
  assert.equal(query.get('page'), '1');
  assert.equal(query.get('dataset_name'), '数据 A & 中文.xlsx');
  assert.equal(query.get('evaluation_profile'), profile, 'a disabled new-task profile can still filter historical results');
  assert.equal(Number(query.get('start_time')), new Date(2026, 9, 28, 9, 30, 1).getTime() / 1000);
  assert.equal(Number(query.get('end_time')), new Date(2026, 9, 28, 19, 30, 59).getTime() / 1000);
  const firstSearchUrl = requests[0].url;
  respond(firstSearchUrl, historyData('matching', 1, 200)); await search;
  assert.equal(app.historyHasFilters.value, true);
  assert.equal(app.historyPageCount.value, 20);
  assert.equal(app.historyNoteDrafts.value.first, '跨页未保存的备注', 'searching preserves unfinished off-page edits');
  assert.equal(app.taskId.value, 'loaded-result');
  assert.deepEqual(Array.from(app.comparisonTaskIds.value), ['selected-A', 'selected-B']);

  app.historySearchDraft.value.dataset_name = '未点击搜索的修改';
  const page = app.loadHistory(2);
  const pageUrl = requests[0].url;
  assert.equal(params(requests[0]).get('dataset_name'), '数据 A & 中文.xlsx');
  assert.equal(params(requests[0]).get('page'), '2');
  respond(pageUrl, historyData('page-two', 2, 200)); await page;
  const refresh = app.loadHistory();
  assert.equal(requests[0].url, pageUrl, 'refresh retains the applied query and page');
  respond(pageUrl, historyData('page-two', 2, 200)); await refresh;

  const queue = app.loadQueue();
  respond('/api/queue', { running: { task_id: 'active', status: 'running' }, queued: [] });
  await tick();
  assert.equal(requests[0].url, pageUrl, 'queue-triggered refresh also retains applied filters');
  respond(pageUrl, historyData('page-two', 2, 200)); await queue;

  const reset = app.resetHistorySearch();
  assert.equal(requests[0].url, '/api/history?page=1');
  assert.equal(app.historySearchDraft.value.dataset_name, '');
  assert.equal(app.historySearchDraft.value.evaluation_profile, '');
  assert.equal(app.historySearchDraft.value.start_time, '');
  assert.equal(app.historySearchDraft.value.end_time, '');
  respond('/api/history?page=1', historyData('first')); await reset;
  assert.equal(app.historyHasFilters.value, false);
  assert.equal(app.historyNoteDrafts.value.first, '跨页未保存的备注');

  app.historySearchDraft.value.dataset_name = '不存在的数据集';
  const empty = app.applyHistorySearch();
  respond(requests[0].url, { page: 1, total: 0, items: [] }); await empty;
  assert.equal(app.historyItems.value.length, 0);
  assert.equal(app.historyTotal.value, 0);
  assert.equal(app.historyHasFilters.value, true, 'empty filtered results are distinguishable from empty history');

  const savedFilters = JSON.stringify(app.historySearchFilters.value);
  app.historySearchDraft.value.start_time = 'not-a-date';
  await app.applyHistorySearch();
  assert.equal(requests.length, 0);
  assert.match(app.historySearchError.value, /开始时间无效/);
  assert.equal(JSON.stringify(app.historySearchFilters.value), savedFilters, 'invalid queries do not replace the current applied conditions');
  app.historySearchDraft.value.start_time = '2026-10-29T00:00:00';
  app.historySearchDraft.value.end_time = '2026-10-28T00:00:00';
  await app.applyHistorySearch();
  assert.equal(requests.length, 0);
  assert.match(app.historySearchError.value, /开始时间不能晚于结束时间/);

  app.historySearchDraft.value.dataset_name = '';
  app.historySearchDraft.value.start_time = '';
  const oneSided = app.applyHistorySearch();
  assert.equal(params(requests[0]).has('start_time'), false);
  assert.equal(params(requests[0]).has('end_time'), true);
  respond(requests[0].url, historyData()); await oneSided;
  assert.equal(app.historySearchError.value, '');

  app.historySearchDraft.value.end_time = '';
  app.historySearchDraft.value.dataset_name = 'first-query';
  const stale = app.applyHistorySearch();
  const staleUrl = requests[0].url;
  app.historySearchDraft.value.dataset_name = 'second-query';
  const latest = app.applyHistorySearch();
  const latestUrl = requests[1].url;
  respond(latestUrl, historyData('latest')); await latest;
  respond(staleUrl, historyData('stale')); await stale;
  assert.equal(app.historyItems.value[0].task_id, 'latest', 'late old responses cannot overwrite a newer search');
  assert.equal(app.historySearchFilters.value.dataset_name, 'second-query');
  assert.equal(app.loadingHistory.value, false);

  app.editHistoryNote(app.historyItems.value[0]);
  app.historyNoteDrafts.value.latest = '检索期间保存的备注';
  const save = app.saveHistoryNote(app.historyItems.value[0]);
  const duringSave = app.applyHistorySearch();
  const duringSaveUrl = requests.find(request => request.url.startsWith('/api/history?')).url;
  respond(duringSaveUrl, historyData('latest')); await duringSave;
  respond('/api/history/latest/note', { note: '检索期间保存的备注' }); await save;
  assert.equal(app.historyItems.value[0].note, '检索期间保存的备注', 'search replacement does not break saving a note');

  app.historySearchDraft.value.dataset_name = 'retry';
  const failed = app.applyHistorySearch();
  take(requests[0].url).reject(new Error('网络错误')); await failed;
  assert.match(app.historyError.value, /历史记录加载失败/);
  assert.equal(app.historySearchFilters.value.dataset_name, 'retry');
  const retry = app.loadHistory();
  assert.equal(params(requests[0]).get('dataset_name'), 'retry');
  respond(requests[0].url, historyData('retry')); await retry;
  assert.equal(app.historyError.value, '');
  assert.equal(requests.length, 0);

  const html = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/index.html'), 'utf8');
  assert.match(html, /role="search"[^>]*@submit.prevent="applyHistorySearch"/);
  assert.match(html, /没有符合检索条件的历史记录/);
  const standardSelect = html.match(/<select[^>]*aria-label="历史测评标准"[\s\S]*?<\/select>/)[0];
  assert.match(standardSelect, /v-for="profile in compareProfiles"/);
  assert.ok(!standardSelect.includes(':disabled'), 'disabled evaluation standards remain searchable');
  console.log('History search UI: applied filters, local time conversion, pagination/refresh, reset, races, errors and preserved note drafts passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
