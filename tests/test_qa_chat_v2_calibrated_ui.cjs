const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/auto_eval/web/static');
const id = 'qa_chat_compare@1.1-demand-calibrated';
let app;
vm.runInNewContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, clearTimeout,
  createApp(options) {app = options.setup(); return {mount() {}};},
});
app.mode.value = 'compare';
app.evaluationProfiles.value = [
  {id: 'qa_chat_compare@1.1', display: '原闲聊V2', modes: ['compare']},
  {id, display: '需求与分档校准', modes: ['compare']},
];
assert.equal(app.compareProfiles.value.length, 2);
app.setTaskProtocol({evaluation_profile: id, protocol_manifest: {
  bundle_revision: '1.1.1', result_schema_version: 'qa-chat-result-2',
}});
assert.equal(app.taskIsChat.value, true);
const row = {
  index: 0, evaluation_profile: id, standard_id: 'qa_chat_compare',
  standard_version: '1.1-demand-calibrated', product_count: 2,
  style_reason: '产品1：短原话、实际表现及原档位；产品2：独立评分依据。',
  style_evidence: ['产品1 当前轮：可见依据', '产品2 当前轮：可见依据'],
  answer1_style_reason: '不得用旧产品级理由替代共享理由',
};
for (const schema of ['qa-chat-result-2', undefined]) {
  row.result_schema_version = schema;
  const reasons = Array.from(app.cellReasons(row, {key: 'style_summary'}));
  assert.equal(reasons[0].text, row.style_reason);
  assert.equal(reasons[1].text, row.style_evidence.join('；'));
  assert.ok(reasons.every(entry => entry.label.includes('维度共享')));
}
app.selectedEvaluationProfile.value = 'qa_chat_compare@1.1';
assert.equal(app.cellReasons(row, {key: 'style_summary'})[0].text, row.style_reason);

const human = {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onUnmounted() {}, AbortController, DOMException, URLSearchParams, FormData, setTimeout, clearTimeout,
};
const humanSource = fs.readFileSync(path.join(root, 'human-compare.js'), 'utf8')
  .replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');
vm.runInNewContext(humanSource + '\nthis.Panel = HumanComparePanel;', human);
const panel = human.Panel.setup({taskId: 'calibrated-chat', evaluationProfile: id});
assert.ok(panel.standards.some(([key]) => key === id));
assert.ok(panel.standards.some(([key]) => key === 'qa_chat_compare@1.1'));
assert.equal(panel.templateStandard.value, id);
assert.equal(panel.selectedDimensions.value.length, 6);
assert.equal(Object.keys(panel.baselineDimensions.value).length, 6);
panel.mapping.value = {human_standard_id: 'qa_chat_compare', human_standard_version: '1.1',
  human_bundle_revision: '1.1.0', human_result_schema_version: 'qa-chat-result-2'};
panel.changeMappingStandard(id);
assert.equal(panel.mappingStandard(), id);
assert.equal(panel.mapping.value.human_bundle_revision, '');
assert.equal(panel.mapping.value.human_result_schema_version, '');
console.log('Calibrated chat: independent selection, shared reasons and human mapping passed');
