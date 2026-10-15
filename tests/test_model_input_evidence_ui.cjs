const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let app;
const downloads = [];
const sandbox = {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, AbortController, clearTimeout,
  createApp(options) {app = options.setup(); return {mount() {}};},
  window: {open(url) {downloads.push(url);}},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8')
  .replace(/^import[^\n]+\n/, ''), sandbox);
app.exportModelInputEvidence();
assert.equal(downloads.length, 0, 'no download without a selected task');
app.taskId.value = 'whole-task';
app.results.value = Array.from({length: 35}, (_, index) => ({index, item_id: `case-${index}`}));
app.scoreRules.value = [{id: 'hide-all', dimension: 'understanding', operator: 'eq', value: '5', products: [1]}];
app.resultPage.value = 3;
app.exportModelInputEvidence();
assert.deepEqual(downloads, ['/api/eval/whole-task/export?format=model_input_evidence'],
  'result filtering and pagination never limit the task export');
app.loadingTaskId.value = 'another-task';
app.exportModelInputEvidence();
assert.equal(downloads.length, 1, 'do not export while switching history tasks');
app.loadingTaskId.value = '';
app.taskId.value = 'task/with?reserved';
app.exportModelInputEvidence();
assert.equal(downloads[1], '/api/eval/task%2Fwith%3Freserved/export?format=model_input_evidence');
const html = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/index.html'), 'utf8');
assert.match(html, /@click="exportModelInputEvidence"[^>]*>导出裁判输入图片 ZIP（整任务）<\/button>/);
assert.match(html, /@click="exportFrames"/, 'the existing visual evidence export remains available');
console.log('model input evidence task export UI: passed');
