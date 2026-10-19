const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let app;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, clearTimeout,
  createApp(options) {app = options.setup(); return {mount() {}};},
});
app.mode.value = 'compare';
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.0',protocol_manifest:{bundle_revision:'1.0.0'}});
assert.equal(app.resultCols.value.filter(c => c.key.endsWith('_summary')).length, 9);
assert.ok(app.resultCols.value.some(c => c.key === 'personalization_summary'));
assert.ok(!app.resultCols.value.some(c => c.key === 'accuracy_summary'));
const row = {index:0,standard_id:'qa_chat_compare',product_count:2,
  answer1_input_status:'complete',answer1_response_gate:'unclear',answer1_safety_gate:'pass',
  answer1_personalization_applicable:true,answer1_personalization_verification_status:'partial',
  answer1_personalization_score_status:'scored',answer1_personalization_score:5,
  answer1_personalization_reason:'准确采用当前偏好', answer1_personalization_evidence:['当前轮上部'],
  answer2_personalization_score_status:'unverifiable',answer2_personalization_reason:'缺少产品2关键历史',
  personalization_applicable:false}; // Shared legacy view must not clear product 1.
app.results.value = [row];
assert.match(app.cell(row,{key:'personalization_summary'}),/P1:5分/);
assert.match(app.cell(row,{key:'personalization_summary'}),/P2:无法核验/);
const reasons = Array.from(app.cellReasons(row,{key:'personalization_summary'}));
assert.ok(reasons.some(e => e.text.includes('关键历史')));
app.addScoreRule();
Object.assign(app.scoreRules.value[0],{dimension:'personalization',operator:'eq',value:'5',products:[1]});
assert.equal(app.filteredResults.value.length,1);
row.answer1_personalization_score = '5';
assert.equal(app.filteredResults.value.length,0,'numeric strings are not legal chat scores');
row.answer1_personalization_score = 5;
row.answer1_safety_gate = 'fail';
assert.equal(app.filteredResults.value.length,0,'safety fail invalidates a stale score');
app.selectedEvaluationProfile.value = 'qa_competitor_compare@0.3';
assert.ok(app.resultCols.value.some(c => c.key === 'personalization_summary'),'new-task selection never renames loaded results');
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.0',protocol_diagnostics:['未知冻结修订']});
assert.match(app.cell(row,{key:'personalization_summary'}),/协议待恢复/);
assert.equal(app.filteredResults.value.length,0,'unknown frozen revisions never pass numeric filters');
app.setTaskProtocol({evaluation_profile:'qa_competitor_compare@0.3'});
assert.ok(app.resultCols.value.some(c => c.key === 'accuracy_summary'));
assert.equal(app.scoreRules.value.length,0,'switching tasks removes incompatible score filters');
console.log('Chat frozen dimensions, product-local reasons, null states and strict filters passed');
