const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '../src/auto_eval/web/static');
let app;
vm.runInNewContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').replace(/^import[^\n]+\n/, ''), {
  ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, clearTimeout,
  createApp(options) {app = options.setup(); return {mount() {}};},
});
const dims = ['response_experience','safety_stability','understanding','emotional_expression',
  'style','scenario_fulfillment','guided_recommendation'];
const profile = {id:'chitchat_evidence_first_flat@1.1', standard_id:'chitchat_evidence_first_flat',
  display:'闲聊七维·证据先行·1-5分（候选）', modes:['compare'], score_range:[1,5],
  result_schema_version:'chitchat-flat-result-1', bundle_revision:'1.1.0'};
app.mode.value = 'compare';
app.evaluationProfiles.value = [{id:'qa_competitor_compare@0.2-simplified',modes:['compare']}, profile];
app.setTaskProtocol({evaluation_profile:profile.id,protocol_manifest:profile});
assert.ok(app.compareProfiles.value.some(p => p.id === profile.id));
assert.equal(app.taskIsFlatChat.value, true);
assert.equal(app.taskIsChat.value, false, 'new protocol never enters old chat gate cascade');
assert.deepEqual(Array.from(app.scoreDimensions.value, d => d.key), dims);
assert.deepEqual(Array.from(app.summaryDimensions.value, d => d.key), dims);
assert.deepEqual(Array.from(app.resultCols.value.filter(c => c.key.endsWith('_summary')
  && c.key !== 'input_status_summary'), c => c.key), dims.map(d => d + '_summary'));
assert.ok(!app.resultCols.value.some(c => /gate|personalization|has_conflict|rationale|needs_human_review|total_score/.test(c.key)));

function payload(count) {
  const raw = {};
  for (const dim of dims) {
    raw[`${dim}_reason`] = Array.from({length:count}, (_,i) => `产品${i+1}：可见原话与本维要求对应，说明实际影响。`).join(' ');
    for (let n = 1; n <= count; n++) raw[`answer${n}_${dim}_score`] = 5;
  }
  assert.equal(Object.keys(raw).length, count === 2 ? 21 : 28);
  return {index:0,standard_id:profile.standard_id,evaluation_profile:profile.id,
    result_schema_version:profile.result_schema_version,product_count:count,...raw};
}
const row = payload(2);
row.answer1_safety_stability_score = 1;
row.answer1_understanding_score = 1;
row.answer1_style_score = 3;
row.answer2_style_score = 4;
row.answer1_guided_recommendation_score = null;
row.answer1_guided_recommendation_score_status = 'unverifiable';
app.results.value = [row];
assert.equal(app.flatChatColumn(row,{key:'style_summary'}), true);
assert.equal(app.cellReasons(row,{key:'style_summary'}).length, 1);
assert.equal(app.cellReasons(row,{key:'style_summary'})[0].text, row.style_reason);
assert.match(app.cell(row,{key:'scenario_fulfillment_summary'}), /P1:5分；P2:5分/,
  'safety and understanding scores of one do not clear other dimensions');
assert.match(app.cell(row,{key:'guided_recommendation_summary'}), /P1:证据不足；P2:5分/);
assert.deepEqual(Array.from(app.flatChatProductScores(row,{key:'style_summary'}), x => x.className), ['flat-mid','flat-high']);
assert.deepEqual(Array.from(app.flatChatProductScores(row,{key:'safety_stability_summary'}), x => x.className), ['flat-low','flat-high']);
app.addScoreRule();
const rule = app.scoreRules.value[0];
Object.assign(rule,{dimension:'style',operator:'eq',value:3,products:[1]});
assert.equal(app.filteredResults.value.length,1);
app.selectedEvaluationProfile.value = 'qa_chat_compare@1.1';
assert.equal(app.filteredResults.value.length,1,'changing new task selector cannot reinterpret loaded task');
Object.assign(rule,{operator:'diff_lte',value:1,products:[1,2]});
assert.equal(app.filteredResults.value.length,1);
for (const changes of [
  {answer1_style_score:'3'}, {answer1_style_score:true}, {answer1_style_score:3.5},
  {answer1_style_score_status:'unverifiable'}, {answer1_input_status:'failed'},
  {answer1_execution_status:'input_failed'}, {error:'technical error'},
]) {
  app.results.value = [{...row,...changes}];
  assert.equal(app.filteredResults.value.length,0,JSON.stringify(changes));
}
Object.assign(rule,{dimension:'safety_stability',operator:'eq',value:3,products:[1]});
app.results.value = [{...row,answer1_safety_stability_score:3}];
assert.equal(app.filteredResults.value.length,0,'gate mapping accepts only 1 or 5');
assert.match(app.cell(app.results.value[0],{key:'safety_stability_summary'}), /结果缺失或异常/);
app.clearScoreRules();
const three = payload(3);
assert.equal(app.flatChatProductScores(three,{key:'response_experience_summary'}).length,3);
assert.equal(app.cellReasons(three,{key:'response_experience_summary'})[0].text, three.response_experience_reason);
const summary = {product_count:2,style_answer1_avg:4.2441,style_answer1_scored:299,
  style_answer1_eligible:300,style_answer1_score_coverage:299/300,
  style_answer1_score_distribution:{1:0,2:1,3:2,4:100,5:196},
  style_answer1_score_status_counts:{unverifiable:1},style_answer1_answer2_paired_count:298,
  style_answer1_answer2_paired_answer1_avg:4.24,style_answer1_answer2_paired_answer2_avg:4.14,
  style_answer1_answer2_mean_ratio:1.0242,style_answer1_answer2_paired_mean_ratio:1.02415};
const stat = Array.from(app.flatChatSummaryRows(summary)).find(x => x.key === 'style-1');
assert.equal(stat.average,4.2441);
assert.equal(stat.scored,299); assert.equal(stat.eligible,300);
assert.match(stat.distribution,/5分 196/); assert.match(stat.states,/证据不足 1/);
const ratio = Array.from(app.flatChatRatios(summary)).find(x => x.key === 'style_answer1_answer2');
assert.equal(ratio.ratio,'102.42%'); assert.equal(ratio.pairedRatio,'102.41%');
assert.equal(ratio.count,298);
assert.equal(app.flatChatRatios({product_count:3}).length,21);
assert.equal(app.flatChatRatios({product_count:2})[0].ratio,'N/A');
const html = fs.readFileSync(path.join(root,'index.html'),'utf8');
const block = html.slice(html.indexOf('<div v-if="flatChatColumn(r,c)"'),html.indexOf('<div v-if="knowledgeQAColumn(r,c)"'));
assert.ok(block.indexOf('reason.text') < block.indexOf('flat-chat-score-list'),'evidence reason is before all product scores');
assert.match(html,/flatChatSummaryRows\(summary\)/);
assert.match(html,/flatChatRatios\(summary\)/);
assert.match(html,/!flatChatColumn\(r,c\) && !knowledgeQAColumn/,'generic renderer excludes new cells');
const css = fs.readFileSync(path.join(root,'style.css'),'utf8');
assert.match(css,/\.flat-chat-result \.flat-mid[^}]+fef9c3/,'three uses middle-grade styling');
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{result_schema_version:'qa-chat-result-2'}});
assert.equal(app.taskIsFlatChat.value,false);
assert.ok(app.scoreDimensions.value.some(d => d.gate));
assert.ok(app.resultCols.value.some(c => c.key === 'personalization_summary'));
app.setTaskProtocol({evaluation_profile:'timeliness_compare@1.0',protocol_manifest:{result_schema_version:'timeliness-result-3'}});
assert.ok(app.resultCols.value.some(c => c.key === 'has_conflict'));

const source = fs.readFileSync(path.join(root,'human-compare.js'),'utf8')
  .replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
const human = {ref:v=>({value:v}),computed:f=>({get value(){return f();}}),onUnmounted(){},
  AbortController,DOMException,URLSearchParams,FormData,setTimeout,clearTimeout};
vm.runInNewContext(source+'\nthis.Panel=HumanComparePanel;',human);
const panel = human.Panel.setup({taskId:'flat-chat-task',evaluationProfile:profile.id});
assert.equal(panel.templateStandard.value,profile.id);
assert.deepEqual(Array.from(panel.selectedDimensions.value),dims);
assert.deepEqual(Object.keys(panel.selectableBaselineDimensions.value),dims);
assert.ok(panel.standards.some(s => s[0] === profile.id));
panel.mapping.value = {human_standard_id:profile.standard_id,human_standard_version:'1.1',products:[{product_id:'p1'}],labels:[]};
assert.equal(panel.mappingStandard(),profile.id);
panel.addLabel(); assert.equal(panel.mapping.value.labels[0].dimension_id,'response_experience');
panel.report.value = {human_protocol:{human_standard_id:profile.standard_id},metrics:{ranks:[{fake:true}]}};
assert.equal(panel.reportIsFlatChat.value,true);
assert.equal(panel.visibleRanks.value.length,0);
assert.match(source,/v-if="r.model_result_schema_version!=='chitchat-flat-result-1'">适用性/);
console.log('Flat chat UI: seven independent dimensions, strict scores, merged reason first, ratios, nulls, human mapping and legacy compatibility passed');
