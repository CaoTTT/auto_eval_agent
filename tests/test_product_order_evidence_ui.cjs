const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const source = read('screenshot-evidence.js');
const sandbox = {ref: value => ({value}), computed: fn => ({get value() {return fn();}}), onUnmounted() {}};
vm.runInNewContext(source.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '') + '\nthis.Component=ScreenshotEvidence;', sandbox);
const panel = sandbox.Component.setup({taskId: 'order', itemIndex: 0});
panel.record.value = {record_status: 'model_response_received'};
for (const [slot, original] of [[1, 2], [2, 1]]) {
  const entry = {product_no: slot, original_product_no: original};
  assert.equal(panel.judgePosition(entry), slot);
  assert.equal(entry.original_product_no ?? entry.product_no, original);
}
panel.record.value = {record_status: 'request_not_recorded'};
assert.equal(panel.judgePosition({product_no: 1, original_product_no: 1}), '未记录');
panel.record.value = {record_status: 'assembled'};
assert.equal(panel.judgePosition({product_no: 3}), 3);
assert.equal(panel.judgePosition({image_role: 'query_image'}), '共享');
assert.match(sandbox.Component.template, /产品 \{\{entry\.original_product_no \?\? entry\.product_no\}\}/);
assert.match(sandbox.Component.template, /picture\.original_product_no \?\? picture\.product_no/);
assert.match(sandbox.Component.template, /record\.judge_product_order_note/);
assert.match(sandbox.Component.template, /裁判位置 \{\{judgePosition\(entry\)\}\}/);
assert.match(sandbox.Component.template, /picture\.image_role!=='query_image'/);
assert.match(read('compare-data.js'), /screenshot-evidence\.js\?v=20261030_product_order/);
assert.match(read('app.js'), /compare-data\.js\?v=[^"]+_product_order/);
console.log('Screenshot evidence UI: source product labels, separate judge slots, legacy unknown positions and order note passed');
