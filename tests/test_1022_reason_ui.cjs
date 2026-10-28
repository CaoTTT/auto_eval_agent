const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let app;
const source = fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static/app.js'), 'utf8');
vm.runInNewContext(source.replace(/^import[^\n]+\n/, ''), {
  ref: value => ({value}), computed: getter => ({get value() { return getter(); }}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, clearTimeout,
  createApp(options) { app = options.setup(); return {mount() {}}; },
});
app.mode.value = 'compare';
const profile = 'qa_competitor_compare@0.2-simplified-1022';
app.setTaskProtocol({evaluation_profile: profile, protocol_manifest: {
  bundle_revision: '0.2.6', output_contract: 'compare_1022_v2',
}});

function row(reason, productCount = 2) {
  const result = {index: 0, product_count: productCount,
    standard_id: 'qa_competitor_compare', standard_version: '0.2-simplified-1022',
    evaluation_profile: profile, bundle_revision: '0.2.6', output_contract: 'compare_1022_v2',
    understanding_applicable: true, understanding_reason: reason};
  for (let number = 1; number <= productCount; number++) Object.assign(result, {
    [`answer${number}_input_status`]: 'complete', [`answer${number}_response_gate`]: 'pass',
    [`answer${number}_safety_gate`]: 'pass', [`answer${number}_understanding_score`]: null,
  });
  return result;
}
const summary = value => app.cell(value, {key: 'understanding_summary'});
function assertFullReasonPreserved(value) {
  assert.equal(Array.from(app.cellReasons(value, {key: 'understanding_summary'}))[0].text,
    value.understanding_reason);
}

for (const [open, close] of [['“', '”'], ['‘', '’'], ['「', '」'], ['『', '』'], ['"', '"'], ['`', '`']]) {
  const value = row(`Query级前言：按当前问题判断适用；产品1：未评分（证据不足）：引用${open}来源🔎；产品2：仅为原话${close}无法核验。；产品2：未评分（采集待复核）：关键截图需确认。`);
  assert.equal(summary(value), 'P1:证据不足；P2:有说明的未评分', `${open}${close} must not split a quoted product label`);
  assertFullReasonPreserved(value);
}

const quotedPrefix = row('Query级前言：来源称“；产品2：未评分（证据不足）：引用文字。”不构成产品分段；产品1：未评分（证据不足）：本产品内容被遮挡。；产品2：已覆盖完整条件。');
quotedPrefix.answer2_understanding_score = 4;
assert.equal(summary(quotedPrefix), 'P1:证据不足；P2:4分');
assertFullReasonPreserved(quotedPrefix);

const escapedQuotes = row('产品1：未评分（证据不足）：来源为"包含\\"引号\\"；产品2：不是段落"，需复核。；产品2：未评分（输入待确认）：无法确定条件。');
assert.equal(summary(escapedQuotes), 'P1:证据不足；P2:有说明的未评分');
assertFullReasonPreserved(escapedQuotes);

const missingRealSection = row('产品1：未评分（证据不足）：原文“；产品2：未评分（证据不足）：引用文本。”并非另一产品理由。');
assert.equal(summary(missingRealSection), 'P1:证据不足；P2:应评分但缺失');

const triples = row('Query级前言：统一判断适用；产品1：按约束回答。；产品2：未评分（证据不足）：原文「；产品3：不是实际分段」仍待核验。；产品3：未评分（无法核验）：关键实体模糊。', 3);
triples.answer1_understanding_score = 5;
assert.equal(summary(triples), 'P1:5分；P2:证据不足；P3:有说明的未评分');
assertFullReasonPreserved(triples);

const duplicateSections = row('产品1：未评分（证据不足）：首段。；产品1：未评分（证据不足）：重复。；产品2：未评分（证据不足）：末段。');
assert.equal(summary(duplicateSections), 'P1:应评分但缺失；P2:应评分但缺失');

const ordinary = row('产品1：未评分（证据不足）：缺少关键截图。; 产品2:未评分（条件待确认）：需人工核对。');
assert.equal(summary(ordinary), 'P1:证据不足；P2:有说明的未评分');
ordinary.answer1_response_gate = 'fail';
assert.equal(summary(ordinary), 'P1:响应Gate阻断；P2:有说明的未评分');
ordinary.answer1_response_gate = 'pass';
ordinary.understanding_applicable = false;
assert.equal(summary(ordinary), 'P1:不适用；P2:不适用');

console.log('1022 reason UI: quoted source labels, Query prefaces, intact shared reasons, NA status and product attribution passed');
