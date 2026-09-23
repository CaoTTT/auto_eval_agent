const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let app;
const requests = [];
const sandbox = {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, AbortController, clearTimeout,
  createApp(options) {app = options.setup(); return {mount() {}};},
  async fetch(url) {
    requests.push(url);
    return {ok: true, json: async () => ({task_id: 'new-task', mode: 'compare', status: 'done',
      items: [], results: [{index: 0, item_id: 'new-case', product_count: 2}], summary: null})};
  },
  window: {open(url) {requests.push(url);}},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8')
  .replace(/^import[^\n]+\n/, ''), sandbox);
const row = (id, a, b, extra = {}) => ({item_id: id, index: id, product_count: 2,
  understanding_applicable: true, answer1_understanding_score: a, answer2_understanding_score: b,
  answer1_response_gate: 'pass', answer2_response_gate: 'fail', ...extra});
const rows = [
  row('low', 0, 2, {understanding_reason: '产品1遗漏问题约束，覆盖缺失。', session_id: 's', turn_index: 1}),
  row('equal', 3, 3, {answer2_response_gate: 'pass', session_id: 's', turn_index: 2, input_modality: 'text_image'}),
  row('far', 5, 1, {answer1_response_gate: 'unclear'}),
  row('missing', null, undefined),
  row('inapplicable', 1, 1, {understanding_applicable: false}),
  row('three', 2, 4, {product_count: 3, answer3_understanding_score: 1, answer3_response_gate: 'pass'}),
  row('failed', 0, 0, {error: 'model error'}),
];
app.mode.value = 'compare'; app.results.value = rows;
const ids = () => Array.from(app.filteredResults.value, r => r.item_id);
function rule(extra = {}, clear = true) {
  if (clear) app.clearScoreRules();
  app.addScoreRule();
  const r = app.scoreRules.value.at(-1);
  Object.assign(r, {dimension: 'understanding', operator: 'lt', value: '3', products: [1, 2], productJoin: 'any'}, extra);
  return r;
}
assert.deepEqual(ids(), rows.map(r => r.item_id), 'no rule retains successes and failures');
assert.deepEqual(Array.from(app.filterProducts.value), [1, 2, 3]);
let r = rule();
assert.deepEqual(ids(), ['low', 'far', 'three'], 'OR low-score matching excludes N/A and errors');
r.productJoin = 'all';
assert.deepEqual(ids(), ['low'], 'AND requires every selected product to match');
r.products = [2]; r.operator = 'eq'; r.value = '3';
assert.deepEqual(ids(), ['equal'], 'specific product exact score');
r.products = [1]; r.value = '0';
assert.deepEqual(ids(), ['low'], 'zero is valid while null is not zero');
r.operator = 'gte'; r.value = '3';
assert.deepEqual(ids(), ['equal', 'far']);
r.operator = 'gt';
assert.deepEqual(ids(), ['far']);
r.operator = 'lte';
assert.deepEqual(ids(), ['low', 'equal', 'three']);
r.operator = 'lt'; r.value = '2.5';
assert.deepEqual(ids(), ['low', 'three']);

r = rule({operator: 'diff_lte', value: '2'});
assert.deepEqual(ids(), ['low', 'equal', 'three'], 'absolute gap includes the boundary, excludes missing scores');
r.value = '0';
assert.deepEqual(ids(), ['equal'], 'zero gap means equal valid scores');
r.value = '2'; r.products = [2, 1];
assert.deepEqual(ids(), ['low', 'equal', 'three'], 'reversing product order does not change absolute difference');
r.products = [2, 3]; r.value = '3';
assert.deepEqual(ids(), ['three'], 'product 3 gap excludes two-product cases');
r.products = [1, 2, 3];
assert.match(app.scoreFilterError.value, /恰好选择两个/);
assert.deepEqual(ids(), []);
r.products = []; assert.match(app.scoreFilterError.value, /至少选择一个/);
r.products = [1, 2];
for (const bad of ['', ' ', '-1', 'NaN', 'Infinity']) {
  r.value = bad;
  assert.ok(app.scoreFilterError.value, `reject invalid threshold ${bad}`);
  assert.deepEqual(ids(), []);
}
r = rule({operator: 'na', products: [1]});
assert.deepEqual(ids(), ['missing', 'inapplicable']);
r.products = [3];
assert.deepEqual(ids(), [], 'a nonexistent product is not a missing score for that case');

r = rule({dimension: 'response_gate', operator: 'pass', productJoin: 'all'});
assert.deepEqual(ids(), ['equal'], 'Gate AND uses explicit pass, never scores');
r.operator = 'fail'; r.productJoin = 'any';
assert.deepEqual(ids(), ['low', 'far', 'missing', 'inapplicable', 'three']);
r.operator = 'unclear';
assert.deepEqual(ids(), ['far']);
r.products = [1, 3]; r.operator = 'pass'; r.productJoin = 'all';
assert.deepEqual(ids(), ['three']);
app.changeScoreDimension(Object.assign(r, {dimension: 'safety_gate'}));
assert.equal(r.operator, 'pass');
app.changeScoreDimension(Object.assign(r, {dimension: 'service_closure'}));
assert.equal(r.operator, 'lt'); assert.equal(r.value, '');

// Each numeric dimension and Gate uses its own field; no score-scale assumptions.
for (const dimension of Array.from(app.scoreDimensions).filter(d => !d.gate)) {
  app.results.value = [row('dimension', null, null, {
    [`${dimension.key}_applicable`]: true, [`answer1_${dimension.key}_score`]: '5',
    [`answer2_${dimension.key}_score`]: false,
  })];
  rule({dimension: dimension.key, operator: 'eq', value: '5', products: [1]});
  assert.deepEqual(ids(), ['dimension']);
  app.scoreRules.value[0].products = [2]; app.scoreRules.value[0].value = '0';
  assert.deepEqual(ids(), [], 'boolean values must not be interpreted as scores');
}
app.results.value = rows;
r = rule({products: [1]});
rule({dimension: 'response_gate', operator: 'unclear', products: [1]}, false);
assert.deepEqual(ids(), [], 'cross-dimension AND');
app.scoreRuleJoin.value = 'any';
assert.deepEqual(ids(), ['low', 'far', 'three'], 'cross-dimension OR');
app.resultQuery.value = '覆盖缺失';
assert.deepEqual(ids(), ['low'], 'search includes folded dimension reasons');
app.resetResultFilters();
app.turnFilter.value = '2'; app.modalityFilter.value = 'text_image';
rule({operator: 'eq', value: '3'});
assert.deepEqual(ids(), ['equal'], 'score, turn and modality filters intersect');
app.failedOnly.value = true;
assert.deepEqual(ids(), [], 'evaluation errors are distinct from Gate failures');
app.resetResultFilters();

// Folding affects display only, independently for each dimension/product reason.
const detail = {index: 42, product_count: 3, understanding_reason: 'score detail',
  answer1_response_gate_reason: 'product one', answer2_response_gate_reason: 'product two',
  answer3_response_gate_reason: 'product three', rationale: '<script>plain text</script>'};
const dimensionCol = {key: 'understanding_summary', label: '理解需求'};
const gateCol = {key: 'response_gate_summary', label: '响应体验'};
const before = JSON.stringify(detail);
assert.equal(app.reasonsExpanded(detail, dimensionCol), false);
assert.equal(app.cellReasons(detail, dimensionCol)[0].text, 'score detail');
assert.deepEqual(Array.from(app.cellReasons(detail, gateCol), r => r.label), ['产品 1', '产品 2', '产品 3']);
app.toggleCellReasons(detail, dimensionCol);
assert.equal(app.reasonsExpanded(detail, dimensionCol), true);
assert.equal(app.reasonsExpanded(detail, gateCol), false);
app.setAllReasonsExpanded(true);
assert.equal(app.reasonsExpanded(detail, gateCol), true);
app.toggleCellReasons(detail, gateCol);
assert.equal(app.reasonsExpanded(detail, gateCol), false);
app.setAllReasonsExpanded(false);
assert.equal(app.reasonsExpanded(detail, dimensionCol), false);
assert.equal(JSON.stringify(detail), before);
assert.equal(app.reasonOnlyColumn({key: 'problem_solved_reason'}), true);
assert.equal(app.cellReasons({problem_solved_reason: 'rich mode reason'}, {key: 'problem_solved_reason', label: '评价原因'})[0].text, 'rich mode reason');
assert.equal(app.cellReasons({review_reasons: [], review_reason: 'legacy review'}, {key: 'needs_human_review'})[0].text, 'legacy review');
assert.match(app.cell(rows[0], gateCol), /P2:不通过/);
app.mode.value = 'rich_content';
rule({value: ''});
assert.deepEqual(ids(), rows.map(r => r.item_id), 'hidden comparison controls cannot filter single-product results');
app.mode.value = 'compare'; app.resetResultFilters();

app.resultPage.value = 7;
rule(); assert.equal(app.resultPage.value, 1);
app.resultPage.value = 4;
app.removeScoreRule(app.scoreRules.value[0].id); assert.equal(app.resultPage.value, 1);
app.results.value = Array.from({length: 30}, (_, i) => row(i, i % 5, 3));
rule({operator: 'lt', value: '2', products: [1]});
assert.equal(app.filteredResults.value.length, 12);
assert.equal(app.pageCount.value, 2);
app.resultPage.value = 2;
assert.equal(app.pagedResults.value.length, 2);

(async () => {
  app.taskId.value = 'original-task';
  app.exportCsv(); app.exportJson();
  assert.deepEqual(requests.slice(-2), ['/api/eval/original-task/export?format=csv', '/api/eval/original-task/export?format=json']);
  app.turnFilter.value = '3'; app.resultQuery.value = 'old'; app.setAllReasonsExpanded(true);
  await app.loadHistoryTask('new-task');
  assert.equal(app.scoreRules.value.length, 0, 'new task clears stale score filters');
  assert.equal(app.turnFilter.value, ''); assert.equal(app.resultQuery.value, '');
  assert.equal(app.allReasonsExpanded.value, false);
  assert.deepEqual(ids(), ['new-case']);
  console.log('Score filters, product AND/OR, gaps, missing scores, reasons, pagination and task switching passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
