const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let app;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/app.js'),'utf8').replace(/^import[^\n]+\n/,''),{
  ref:value=>({value}),computed:getter=>({get value(){return getter();}}),onMounted(){},onUnmounted(){},nextTick(){},console,clearTimeout,
  createApp(options){app=options.setup();return {mount(){}};},
});
app.mode.value='compare';
const profile='qa_competitor_compare@0.2-simplified-1022';
app.setTaskProtocol({evaluation_profile:profile,protocol_manifest:{bundle_revision:'0.2.3',output_contract:'compare_1022_v2'}});
assert.ok(!app.resultCols.value.some(c=>['accuracy_summary','needs_human_review','rationale'].includes(c.key)));
assert.equal(app.resultCols.value.filter(c=>c.key.endsWith('_summary')).length,9);
const reason='产品1：首屏包含目标信息，用户可完成操作。；产品2：未评分（证据不足）：关键位置被遮挡。';
const row={index:0,product_count:2,standard_id:'qa_competitor_compare',standard_version:'0.2-simplified-1022',
  evaluation_profile:profile,bundle_revision:'0.2.3',output_contract:'compare_1022_v2',
  answer1_input_status:'complete',answer1_response_gate:'pass',answer1_safety_gate:'pass',answer1_understanding_score:4,
  answer2_input_status:'partial',answer2_response_gate:'pass',answer2_safety_gate:'pass',answer2_understanding_score:null,
  understanding_applicable:true,understanding_reason:reason};
app.results.value=[row];
assert.equal(app.cell(row,{key:'understanding_summary'}),'P1:4分；P2:证据不足');
assert.ok(!app.cell(row,{key:'understanding_summary'}).includes('P3'));
assert.equal(Array.from(app.cellReasons(row,{key:'understanding_summary'}))[0].text,reason);
app.addScoreRule();
Object.assign(app.scoreRules.value[0],{dimension:'understanding',operator:'eq',value:4,products:[1]});
assert.equal(app.filteredResults.value.length,1);
for (const gate of ['response','safety']) for (const state of ['fail','unclear']) {
  row[`answer1_${gate}_gate`]=state;
  assert.equal(app.filteredResults.value.length,0,'either non-pass Gate blocks a stale score');
  assert.match(app.cell(row,{key:'understanding_summary'}),/P1:.*Gate阻断/);
  row[`answer1_${gate}_gate`]='pass';
}
row.answer1_understanding_score='4';
assert.equal(app.filteredResults.value.length,0,'numeric strings are invalid');
row.answer1_understanding_score=4;
app.selectedEvaluationProfile.value='qa_competitor_compare@0.3';
assert.equal(app.filteredResults.value.length,1,'task contract remains frozen when changing the new-task selector');
for (const revision of ['0.2.2','0.2.3','0.2.4','0.2.5','0.2.6']) {
  app.setTaskProtocol({evaluation_profile:profile,protocol_manifest:{bundle_revision:revision}});
  assert.ok(!app.resultCols.value.some(c=>['accuracy_summary','needs_human_review','rationale'].includes(c.key)));
  const fallback={...row,bundle_revision:revision};delete fallback.output_contract;
  assert.equal(app.cell(fallback,{key:'understanding_summary'}),'P1:4分；P2:证据不足');
  delete fallback.bundle_revision;
  assert.equal(app.cell(fallback,{key:'understanding_summary'}),'P1:4分；P2:证据不足');
}
const old={...row,bundle_revision:'0.2.1',output_contract:'compare_1022_v1',understanding_verification_status:'unverifiable'};
app.setTaskProtocol({evaluation_profile:profile,protocol_manifest:{bundle_revision:'0.2.1',output_contract:'compare_1022_v1'}});
assert.ok(app.resultCols.value.some(c=>c.key==='accuracy_summary'));
assert.ok(app.resultCols.value.some(c=>c.key==='needs_human_review'));
assert.match(app.cell(old,{key:'understanding_summary'}),/无法核验/);
const panelSource=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/human-compare.js'),'utf8').replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
const context={};vm.runInNewContext(panelSource+'\nthis.Panel=HumanComparePanel;',context);
assert.match(context.Panel.template,/<template v-if="r.model_output_contract!=='compare_1022_v2'">；核验：/);
const index=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/index.html'),'utf8');
assert.match(index,/!compact1022\(\) && summary.needs_review_count/);
assert.match(index,/app\.js\?v=20261028_history_search/);
console.log('1022 compact UI: six dimensions, Gate blocking, shared reasons, no third product, hidden retired fields and frozen legacy display passed');
