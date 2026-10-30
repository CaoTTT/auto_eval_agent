const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');

async function main() {
  let app, rootOptions, submitted, parsed;
  const base = {id: 'original', query: '问题', product_count: 2,
    video1: 'one.mp4', video2: 'two.mp4', context1: '原产品1背景', context2: '原产品2背景',
    answer1: '原产品1回答', answer2: '原产品2回答'};
  const note = '已互换：裁判位置1=原产品2，裁判位置2=原产品1；产品数据、评分和胜负均按原产品身份展示。';
  const sandbox = {
    ref: value => ({value}), computed: fn => ({get value() {return fn();}}),
    onMounted() {}, onUnmounted() {}, nextTick() {}, AbortController, FormData, console,
    confirm() {return true;}, alert(message) {throw Error(message);},
    createApp(options) {rootOptions = options; app = options.setup(); return {mount() {}};},
    async fetch(url, options) {
      if (url === '/api/eval') {
        submitted = JSON.parse(options.body);
        return {ok: false, json: async () => ({detail: 'test stop'})};
      }
      if (url.startsWith('/api/history/')) return {ok: true, json: async () => ({
        task_id: 'history', mode: 'compare', status: 'done', items: [base], results: [],
        options: {reverse_product_order: url.endsWith('/reversed')}, judge_product_order_note: url.endsWith('/reversed') ? note : undefined,
      })};
      if (url === '/api/parse') return {ok: true, json: async () => parsed};
      if (url === '/api/upload/dataset-source') return {ok: true, json: async () => ({id: 'source'})};
      throw Error(url);
    },
  };
  vm.runInNewContext(strip(read('compare-cases.js')), sandbox);
  vm.runInNewContext(strip(read('app.js')), sandbox);
  app.mode.value = 'compare';
  app.useComparisonDataset({task_id: 'source', items: [base]});
  assert.equal(app.reverseProductOrder.value, false);
  assert.equal(app.reverseOrderAvailable.value, true);
  await app.submit();
  assert.equal(submitted.options.reverse_product_order, false);
  const originalItems = JSON.stringify(submitted.items);
  app.reverseProductOrder.value = true;
  await app.submit();
  assert.equal(submitted.options.reverse_product_order, true);
  assert.equal(JSON.stringify(submitted.items), originalItems, 'presentation order never rewrites source products');

  app.opItems.value[0].productCount = 3;
  app.opItems.value[0].video3Path = 'third.mp4';
  app.opItems.value[0].context3 = '原产品3背景';
  assert.equal(app.reverseOrderAvailable.value, false);
  // Exercise the same Options API watcher Vue invokes when the computed eligibility changes.
  rootOptions.watch.reverseOrderAvailable.call({set reverseProductOrder(value) {app.reverseProductOrder.value = value;}}, false);
  assert.equal(app.reverseProductOrder.value, false);
  app.reverseProductOrder.value = true;
  await app.submit();
  assert.equal(submitted.options.reverse_product_order, false, 'submit also guards rapid edits before Vue watchers flush');
  assert.equal(submitted.items[0].product_count, 3);
  app.opItems.value[0].productCount = 2;
  assert.equal(app.reverseProductOrder.value, false, 'returning to two products does not re-enable reversal');

  await app.loadHistoryTask('reversed');
  assert.equal(app.reverseProductOrder.value, true);
  assert.equal(app.taskProductOrderNote.value, note);
  assert.equal(app.opItems.value[0].answer1, base.answer1);
  await app.submit();
  assert.equal(submitted.options.reverse_product_order, true);
  await app.loadHistoryTask('normal');
  assert.equal(app.reverseProductOrder.value, false);
  assert.match(app.taskProductOrderNote.value, /未互换/);

  app.reverseProductOrder.value = true;
  app.useComparisonDataset({task_id: 'source', items: [base], options: {reverse_product_order: true}});
  assert.equal(app.reverseProductOrder.value, false, 'dataset reuse creates a fresh configuration with the default order');
  app.reverseProductOrder.value = true;
  parsed = {items: [base], errors: [], blocked: false};
  await app.onOpManifestFile({target: {files: [{name: 'new.jsonl', text: async () => ''}], value: ''}});
  assert.equal(app.reverseProductOrder.value, false, 'a new uploaded dataset starts with reversal disabled');

  const html = read('index.html');
  assert.match(html, /id="reverse-product-order"[^>]*:disabled="submitting \|\| !reverseOrderAvailable"/);
  assert.match(html, /含三产品时不支持互换，开关自动关闭/);
  assert.match(html, /本任务裁判产品顺序/);
  console.log('Product-order UI: default, reversal, identity, three-product guard, history and fresh dataset configuration passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
