// Run the real import, reuse and submission functions without a network or model.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const strip = code => code.replace(/^import[^\n]*\n/gm, '')
  .replace(/^export\s*\{[^\n]*\};?\s*$/gm, '').replace(/^export /gm, '');
const response = (data, ok = true) => ({ok, json: async () => data});
const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
async function tick() {for (let i = 0; i < 12; i++) await Promise.resolve();}
const compareItem = {id: 'case', query: 'original question', screenshot1: 'a.png', screenshot2: 'b.png', context1: '产品1背景', context2: '产品2背景'};
const richItem = {id: 'rich', query: 'rich question', video_path: 'video.mp4', answer_text: 'answer'};

function harness() {
  let app, cleanup;
  const calls = [], bodies = [];
  const state = {parsed: {items: [compareItem], errors: []}, allowed: true, uploadId: 'source-1'};
  const sandbox = {
    ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
    onMounted() {}, onUnmounted(fn) {cleanup = fn;}, nextTick: async () => {},
    AbortController, FormData, console: {log() {}, warn() {}, error() {}},
    confirm: () => state.allowed, alert(message) {throw Error(message);},
    createApp(options) {app = options.setup(); return {mount() {}};},
    async fetch(url, options) {
      calls.push({url, options});
      if (url === '/api/parse') return state.parseWait ? state.parseWait.promise : response(state.parsed, !state.parseError);
      if (url === '/api/upload/dataset-source') return state.uploadWait ? state.uploadWait.promise
        : response(state.uploadError ? {detail: 'disk full'} : {id: state.uploadId}, !state.uploadError);
      if (url === '/api/eval') {bodies.push(JSON.parse(options.body)); return response({detail: 'test stop'}, false);}
      if (url.startsWith('/api/history?page=')) return response({items: state.historyRows || [], total: 1, page: 1});
      if (url.startsWith('/api/history/')) return state.historyWait ? state.historyWait.promise : response(state.history);
      throw Error(`Unexpected request: ${url}`);
    },
  };
  vm.runInNewContext(strip(read('compare-cases.js')), sandbox);
  vm.runInNewContext(strip(read('app.js')), sandbox);
  const upload = file => app.onOpManifestFile({target: {value: 'selected', files: file ? [file] : []}});
  return {app, state, calls, bodies, upload, cleanup: () => cleanup()};
}

async function main() {
  const h = harness(), {app, state, calls, bodies, upload} = h;
  app.switchMode('compare');
  app.useComparisonDataset({task_id: 'old-task', dataset_name: 'old.json', items: [compareItem]});
  const originalBytes = new Uint8Array([239, 187, 191, 91, 32, 123, 125, 32, 93, 13, 10]);
  const file = new File([originalBytes], '测评 原始.JSON', {type: 'application/json'});
  await upload(file);
  const sentFile = calls.find(call => call.url === '/api/upload/dataset-source').options.body.get('file');
  assert.equal(sentFile, file, 'multipart upload must receive the original File, without text reserialization');
  assert.deepEqual(new Uint8Array(await sentFile.arrayBuffer()), originalBytes, 'BOM, spacing and CRLF survive upload');
  assert.equal(calls.filter(call => call.url === '/api/upload/dataset-source').length, 1);
  assert.equal(app.datasetSourceFileId.value, 'source-1');
  assert.equal(app.datasetSourceTaskId.value, '');
  await app.submit();
  assert.equal(bodies.at(-1).source_file_id, 'source-1');
  assert.equal(bodies.at(-1).source_task_id, undefined);

  for (const failure of ['empty', 'parse', 'upload', 'cancel', 'no-file']) {
    const countBefore = calls.filter(call => call.url === '/api/upload/dataset-source').length;
    state.parsed = failure === 'empty' ? {items: [], errors: ['empty file']} : {items: [compareItem], errors: []};
    state.parseError = failure === 'parse'; state.uploadError = failure === 'upload';
    state.allowed = failure !== 'cancel';
    if (failure === 'cancel') app.opItems.value[0].query = 'unsaved edit';
    await upload(failure === 'no-file' ? null : file);
    assert.equal(app.datasetSourceFileId.value, 'source-1', `${failure} must retain the current source`);
    assert.equal(app.datasetName.value, file.name, `${failure} must retain the current dataset`);
    assert.equal(calls.filter(call => call.url === '/api/upload/dataset-source').length,
      countBefore + (failure === 'upload' ? 1 : 0), 'unusable or canceled imports must not upload a source');
  }
  state.parsed = {items: [compareItem], errors: []};state.parseError = false;state.uploadError = false;state.allowed = true;

  app.useComparisonDataset({task_id: 'reused', dataset_name: 'same.json', items: [compareItem], source_files: [{id: 'x'}]});
  assert.equal(app.datasetSourceFileId.value, '');
  await app.submit();
  assert.equal(bodies.at(-1).source_task_id, 'reused');
  assert.equal(bodies.at(-1).source_file_id, undefined);
  assert.equal(bodies.at(-1).options.dataset_source_task_id, 'reused');
  state.history = {task_id: 'rich-history', mode: 'rich_content', status: 'done', items: [richItem], results: []};
  await app.loadHistoryTask('rich-history');
  assert.equal(app.datasetSourceTaskId.value, 'rich-history');
  assert.equal(app.opItems.value[0].query, richItem.query, 'loaded rich history must replace the prior compare draft');
  await app.submit();
  assert.equal(bodies.at(-1).source_task_id, 'rich-history');
  assert.equal(bodies.at(-1).options.dataset_source_task_id, undefined, 'compare metadata must not leak to rich tasks');
  assert.equal(bodies.at(-1).items[0].video_path, 'video.mp4');
  state.parsed = {items: [richItem], errors: []};state.uploadId = 'rich-file';
  await upload(new File(['{}\r\n'], 'rich.jsonl'));
  await app.submit();
  assert.equal(bodies.at(-1).source_file_id, 'rich-file', 'both modes submit uploaded sources');
  assert.equal(bodies.at(-1).source_task_id, undefined);

  // A history load must replace the source ref even for records without saved files.
  state.history = {task_id: 'legacy', mode: 'compare', status: 'done', items: [compareItem], results: []};
  await app.loadHistoryTask('legacy');
  assert.equal(app.datasetSourceFileId.value, '');
  assert.equal(app.datasetSourceTaskId.value, 'legacy');
  state.historyRows = [{task_id: 'download-task', source_files: [{id: 'saved', filename: 'original.csv'}]}];
  await app.loadHistory();
  assert.equal(app.historyItems.value[0].source_files[0].filename, 'original.csv');
  assert.equal(app.taskId.value, 'legacy', 'listing downloads must not select another task');
  app.switchMode('rich_content');
  assert.equal(app.datasetSourceFileId.value, '');assert.equal(app.datasetSourceTaskId.value, '');
  Object.assign(app.opItems.value[0], {query: 'manual', videoPath: 'manual.mp4'});
  await app.submit();
  assert.equal(bodies.at(-1).source_file_id, undefined);assert.equal(bodies.at(-1).source_task_id, undefined);

  // Late file reads, parses and uploads cannot overwrite a switched mode or reused history.
  for (const phase of ['text', 'parse', 'upload']) {
    const pending = deferred(), racing = harness();racing.app.switchMode('compare');
    const raceFile = phase === 'text' ? {name: 'slow.json', text: () => pending.promise} : new File(['{}'], 'slow.json');
    if (phase === 'parse') racing.state.parseWait = pending;
    if (phase === 'upload') racing.state.uploadWait = pending;
    const importing = racing.upload(raceFile);await tick();
    const activeRequest = racing.calls.at(-1);
    racing.app.switchMode('rich_content');
    if (activeRequest) assert.equal(activeRequest.options.signal.aborted, true);
    pending.resolve(phase === 'text' ? '{}' : response(phase === 'parse' ? {items: [compareItem]} : {id: 'late-source'}));
    await importing;
    assert.equal(racing.app.mode.value, 'rich_content');
    assert.equal(racing.app.datasetSourceFileId.value, '');assert.equal(racing.app.datasetName.value, '');
    assert.equal(racing.app.opPreparing.value, false);
    assert.equal(racing.calls.filter(call => call.url === '/api/upload/dataset-source').length, phase === 'upload' ? 1 : 0);
  }
  const racing = harness();racing.app.switchMode('compare');
  const slow = deferred();racing.state.uploadWait = slow;
  const first = racing.upload(new File(['first'], 'first.json'));await tick();
  racing.state.uploadWait = null;racing.state.uploadId = 'latest';
  await racing.upload(new File(['second'], 'second.json'));
  slow.resolve(response({id: 'stale'}));await first;
  assert.equal(racing.app.datasetSourceFileId.value, 'latest');
  assert.equal(racing.app.datasetName.value, 'second.json');
  const historyRace = deferred();racing.state.uploadWait = historyRace;
  const beforeReuse = racing.upload(new File(['third'], 'third.json'));await tick();
  racing.app.useComparisonDataset({task_id: 'picked-history', items: [compareItem]});
  historyRace.resolve(response({id: 'unused'}));await beforeReuse;
  assert.equal(racing.app.datasetSourceTaskId.value, 'picked-history');assert.equal(racing.app.datasetSourceFileId.value, '');

  // Download anchors use a task-scoped route and the saved name, without fetching blobs or switching history.
  const sandbox = {computed: fn => ({get value() {return fn();}}), CompareCaseList: {}};
  vm.runInNewContext(strip(read('compare-data.js')) + '\nthis.Downloads=SourceFileDownloads;this.Panel=CompareDatasetPanel;', sandbox);
  const props = {taskId: 'task /中文', files: [{id: 'file /1', filename: '原始.jsonl'}, {id: 'two', filename: 'second.csv'}]};
  const downloads = sandbox.Downloads.setup(props).downloads;
  assert.equal(downloads.value[0].url, '/api/history/task%20%2F%E4%B8%AD%E6%96%87/source-files/file%20%2F1');
  assert.equal(downloads.value[1].filename, 'second.csv');
  assert.match(sandbox.Downloads.template, /<a v-for=/);assert.match(sandbox.Downloads.template, /:download="file.filename"/);
  assert.match(sandbox.Downloads.template, /downloads.length>1\?'：'\+file.filename/);
  assert.doesNotMatch(sandbox.Downloads.template, /@click/);
  props.files = [];assert.equal(downloads.value.length, 0);assert.match(sandbox.Downloads.template, /v-else[^>]*>未保存源文件/);
  props.files = undefined;assert.equal(downloads.value.length, 0);
  assert.match(read('index.html'), /<source-file-downloads :task-id="h.task_id" :files="h.source_files"/);
  assert.match(sandbox.Panel.template, /<source-file-downloads :task-id="row.task_id" :files="row.source_files"/);
  assert.match(sandbox.Panel.template, /<source-file-downloads :task-id="preview.task_id" :files="preview.source_files"/);
  assert.ok(sandbox.Panel.components.SourceFileDownloads);
  console.log('Original File upload, failed/canceled/stale imports, source inheritance, both modes and direct download links passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
