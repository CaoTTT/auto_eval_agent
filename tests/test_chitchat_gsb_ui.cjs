const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '../src/auto_eval/web/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const strip = source => source.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');
let app;
vm.runInNewContext(strip(read('app.js')), {
  ref:value => ({value}), computed:getter => ({get value() { return getter(); }}),
  onMounted() {}, onUnmounted() {}, nextTick() {}, console, clearTimeout,
  createApp(options) { app = options.setup(); return {mount() {}}; },
});
const profile = {id:'chitchat_gsb_compare@1.0', standard_id:'chitchat_gsb_compare',
  standard_version:'1.0', bundle_revision:'1.0.0', result_schema_version:'chitchat-gsb-result-1',
  modes:['compare'], score_range:null, dimensions:[{key:'overall',label:'整体GSB'}],
  score_dimensions:[], has_gates:false, result_type:'pairwise_gsb'};
app.mode.value = 'compare';
app.evaluationProfiles.value = [profile];
app.setTaskProtocol({mode:'compare', evaluation_profile:profile.id, protocol_manifest:profile});
assert.equal(app.taskIsChitchatGSB.value, true);
assert.equal(app.taskIsDialogueExperience.value, false);
assert.equal(app.taskIsChat.value, false);
assert.deepEqual(Array.from(app.scoreDimensions.value), []);
assert.deepEqual(Array.from(app.summaryDimensions.value), []);
assert.ok(app.resultCols.value.some(column => column.key === 'gsb_summary'));
assert.ok(!app.resultCols.value.some(column => /gate|has_conflict|understanding|personalization|overall_summary|rationale/.test(column.key)));
assert.ok(app.columnWidth({key:'gsb_summary'}) >= 500);

function pair(a, b, gsb='G', status='evaluated', serious='none') {
  return {product_a:a,product_b:b,status,gsb,
    reason:`产品${a}的专属证据与产品${b}比较后，说明当前诉求的实际差异。`,serious_issue:serious};
}
function payload(count=2) {
  return {index:0, product_count:count, standard_id:profile.standard_id,
    evaluation_profile:profile.id, result_schema_version:profile.result_schema_version,
    gsb_pairs:count === 3 ? {'1_2':pair(1,2),'1_3':pair(1,3,'S'),'2_3':pair(2,3,'B','evaluated','A')}
      : {'1_2':pair(1,2)}};
}
const two = payload();
app.results.value = [two];
assert.equal(app.chitchatGSBColumn(two,{key:'gsb_summary'}), true);
assert.equal(app.chitchatGSBColumn(two,{key:'overall_summary'}), false);
assert.equal(app.dialogueExperienceColumn(two,{key:'gsb_summary'}), false);
assert.equal(app.chitchatGSBPairs(two).length, 1);
assert.equal(app.chitchatGSBPairs(two)[0].label, 'G · 产品1更好');
assert.equal(app.chitchatGSBPairs(two)[0].pairLabel, 'A：产品1 / B：产品2');
assert.equal(app.chitchatGSBPairs(two)[0].seriousLabel, '无已确认严重问题');
assert.match(app.cell(two,{key:'gsb_summary'}), /A：产品1 \/ B：产品2：G · 产品1更好/);
assert.equal(app.cellReasons(two,{key:'gsb_summary'}).length, 1);
app.resultQuery.value = '专属证据';
assert.equal(app.filteredResults.value.length, 1, 'nested pair reasons remain searchable');
app.resultQuery.value = '';
app.selectedEvaluationProfile.value = 'qa_chat_compare@1.1';
assert.equal(app.taskIsChitchatGSB.value, true, 'new-task selector cannot reinterpret loaded task');

const three = payload(3);
const entries = Array.from(app.chitchatGSBPairs(three));
assert.deepEqual(entries.map(entry => entry.key), ['1_2','1_3','2_3']);
assert.deepEqual(entries.map(entry => entry.label), ['G · 产品1更好','S · 整体相近','B · 产品3更好']);
assert.equal(entries[2].seriousLabel, '产品2');
assert.equal(app.cellReasons(three,{key:'gsb_summary'}).length, 3);
app.results.value = [two,three];
assert.equal(app.chitchatGSBPairOptions.value.length, 3);
app.gsbPairFilter.value = '2_3';
app.gsbVerdictFilter.value = 'B';
assert.equal(app.filteredResults.value.length, 1);
app.gsbPairFilter.value = '';
app.gsbVerdictFilter.value = 'S';
assert.equal(app.filteredResults.value.length, 1, 'any-pair filter uses at least one valid S, never missing pairs');

for (const [status, label] of [['insufficient_evidence','待补证据'],['needs_review','待复核']]) {
  const pending = {...two,gsb_pairs:{'1_2':pair(1,2,null,status)}};
  const entry = app.chitchatGSBPairs(pending)[0];
  assert.equal(entry.gsb, null);
  assert.equal(entry.label, label);
  app.results.value = [pending];
  app.gsbVerdictFilter.value = 'S';
  assert.equal(app.filteredResults.value.length, 0);
  app.gsbVerdictFilter.value = status;
  assert.equal(app.filteredResults.value.length, 1);
}
for (const change of [
  {status:'needs_review',gsb:'S'}, {gsb:null}, {gsb:'tie'}, {reason:' '},
  {product_a:2,product_b:1}, {serious_issue:'product1'},
  {serious_issue:'A',gsb:'G'}, {serious_issue:'A',gsb:'S'},
  {serious_issue:'B',gsb:'B'}, {serious_issue:'B',gsb:'S'},
]) {
  const bad = {...two,gsb_pairs:{'1_2':{...two.gsb_pairs['1_2'],...change}}};
  const entry = app.chitchatGSBPairs(bad)[0];
  assert.equal(entry.gsb, null, JSON.stringify(change));
  assert.equal(entry.label, '结果缺失或异常');
}
for (const [serious, gsb] of [['A','B'],['B','G'],['both','G'],['both','S'],['both','B']]) {
  const valid = {...two,gsb_pairs:{'1_2':pair(1,2,gsb,'evaluated',serious)}};
  assert.equal(app.chitchatGSBPairs(valid)[0].gsb,gsb,'valid severity direction remains comparable');
}
const failedCollection = {...three,trusted_collection_metadata:{answer1:{input_status:'failed'}}};
const collected = Array.from(app.chitchatGSBPairs(failedCollection));
assert.deepEqual(collected.map(entry => entry.gsb), [null,null,'B']);
assert.match(collected[0].reason,/可信采集失败/);
assert.match(app.cell(failedCollection,{key:'input_status_summary'}), /P1:失败/);
assert.equal(app.chitchatGSBPairs({...two,error:'request failed'})[0].label,'评估失败');
assert.equal(app.chitchatGSBPairs({...two,result_schema_version:'future-schema'})[0].label,'协议待恢复');
for (const changes of [
  {bundle_revision:'0.9.0'}, {bundle_revision:'unknown'}, {bundle_revision:1},
  {evaluation_profile:'chitchat_gsb_compare@2.0'}, {standard_version:'2.0'},
]) {
  const entry = app.chitchatGSBPairs({...two,...changes})[0];
  assert.equal(entry.gsb,null,JSON.stringify(changes));
  assert.equal(entry.label,'协议待恢复');
}
assert.equal(app.chitchatGSBPairs({...two,bundle_revision:'1.0.0',standard_version:'1.0'})[0].gsb,'G');
app.taskProtocolDiagnostics.value = ['wrong frozen revision'];
assert.equal(app.chitchatGSBPairs(two)[0].gsb, null);
assert.equal(app.chitchatGSBPairs(two)[0].label, '协议待恢复');
app.taskProtocolDiagnostics.value = [];

const summary = {gsb_pairs:{
  '1_2':{product_a:1,product_b:2,total:5,valid_count:3,G:2,S:1,B:0,net_win_rate:2/3,insufficient_evidence:1,needs_review:1},
  '2_3':{product_a:2,product_b:3,total:2,valid_count:0,G:0,S:0,B:0,net_win_rate:null,insufficient_evidence:2},
}};
const stats = Array.from(app.chitchatGSBSummaryRows(summary));
assert.equal(stats[0].netRate, '+66.7%');
assert.equal(stats[0].validCount, 3);
assert.equal(stats[0].insufficient, 1);
assert.equal(stats[0].review, 1);
assert.equal(stats[1].netRate, '—', 'zero valid sample count is not a zero win rate');
assert.equal(app.chitchatGSBSummaryRows({gsb_pairs:{'1_2':{valid_count:2,net_win_rate:0}}})[0].netRate,'0.0%');
app.resetResultFilters();
assert.equal(app.gsbPairFilter.value, '');
assert.equal(app.gsbVerdictFilter.value, '');

const html = read('index.html');
assert.match(html,/taskIsChitchatGSB && summary/);
assert.match(html,/mode==='compare' && !taskIsChitchatGSB/);
assert.match(html,/待补证据和待复核不计入有效分母/);
assert.match(html,/不生成或推断整段会话的 GSB/);
const card = html.slice(html.indexOf('<div v-if="chitchatGSBColumn(r,c)"'),html.indexOf('<div v-if="dialogueExperienceColumn(r,c)"'));
for (const field of ['entry.pairLabel','entry.label','entry.reason','entry.seriousLabel']) assert.ok(card.includes(field));
assert.match(html,/!chitchatGSBColumn\(r,c\)/);
assert.match(html,/<human-compare-panel v-if="mode==='compare' && taskId/);
assert.match(html,/app\.js\?v=20261103_partial_eval/);
assert.match(read('compare-data.js'),/human-compare\.js\?v=20261102_chitchat_gsb/);

app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{result_schema_version:'qa-chat-result-2'}});
assert.equal(app.taskIsChitchatGSB.value, false);
assert.ok(app.scoreDimensions.value.some(dimension => dimension.gate));
assert.ok(app.resultCols.value.some(column => column.key === 'personalization_summary'));
assert.ok(!app.resultCols.value.some(column => column.key === 'gsb_summary'));
console.log('Chitchat GSB UI: categorical pairs, strict missing-state handling, 2/3 products, filters, nested reasons, summaries and legacy isolation passed');
