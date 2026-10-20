const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let app;
const source=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/app.js'),'utf8');
vm.runInNewContext(source.replace(/^import[^\n]+\n/,''),{
  ref:value=>({value}),computed:getter=>({get value(){return getter();}}),onMounted(){},onUnmounted(){},nextTick(){},console,clearTimeout,
  createApp(options){app=options.setup();return {mount(){}};},
});
app.mode.value='compare';
app.setTaskProtocol({evaluation_profile:'timeliness_compare@1.0',
  protocol_manifest:{bundle_revision:'1.0.0',result_schema_version:'timeliness-result-1'}});
assert.equal(app.taskIsTimeliness.value,true);
assert.equal(app.resultCols.value.filter(c=>c.key.endsWith('_summary')).length,6);
assert.ok(!app.resultCols.value.some(c=>['confidence','rationale','has_conflict','style_summary','accuracy_summary'].includes(c.key)));
const row={index:0,standard_id:'timeliness_compare',standard_version:'1.0',
  evaluation_profile:'timeliness_compare@1.0',result_schema_version:'timeliness-result-1',product_count:2,
  answer1_input_status:null,answer1_response_gate:'unclear',answer1_safety_gate:'pass',
  answer1_understanding_score:3,answer1_timeliness_applicable:true,
  answer1_timeliness_verification_status:'verified',answer1_timeliness_score_status:'scored',answer1_timeliness_score:3,
  answer2_input_status:null,answer2_response_gate:'pass',answer2_safety_gate:'pass',
  answer2_understanding_score:3,answer2_timeliness_applicable:true,
  answer2_timeliness_verification_status:'unverifiable',answer2_timeliness_score_status:'insufficient_evidence',answer2_timeliness_score:null,
  timeliness_evidence:['产品1｜原始证据','产品2｜缺少日期'],
  timeliness_reason:'产品1：匹配目标日期；产品2：无法核验。'};
app.results.value=[row];
const reasons=Array.from(app.cellReasons(row,{key:'timeliness_summary'}));
assert.equal(reasons[0].text,row.timeliness_evidence.join('；'),'evidence appears before reason');
assert.equal(reasons[1].text,row.timeliness_reason);
assert.match(app.cell(row,{key:'timeliness_summary'}),/P1:3分/);
assert.match(app.cell(row,{key:'timeliness_summary'}),/P2:NA · 证据不足/);
const badges=Array.from(app.timelinessProductScores(row,{key:'timeliness_summary'}));
assert.equal(badges[0].className,'score-3');
assert.equal(badges[1].className,'score-na');
app.addScoreRule();
Object.assign(app.scoreRules.value[0],{dimension:'timeliness',operator:'eq',value:3,products:[1]});
assert.equal(app.filteredResults.value.length,1);
app.selectedEvaluationProfile.value='qa_chat_compare@1.0';
assert.equal(app.filteredResults.value.length,1,'new selection does not reinterpret frozen results');
for(const changes of [
  {answer1_timeliness_score:4},{answer1_timeliness_score:'3'},
  {answer1_timeliness_verification_status:'partial'},
  {answer1_understanding_score:1},{answer1_response_gate:'fail'},
  {answer1_input_status:'failed'},
]) {
  app.results.value=[{...row,...changes}];
  assert.equal(app.filteredResults.value.length,0,JSON.stringify(changes));
}
app.setTaskProtocol({evaluation_profile:'timeliness_compare@1.0',
  protocol_manifest:{bundle_revision:'1.0.1',result_schema_version:'timeliness-result-2'}});
const compact={...row,result_schema_version:'timeliness-result-2',bundle_revision:'1.0.1',
  answer1_understanding_applicable:true,answer1_understanding_score_status:'scored',
  answer2_understanding_applicable:true,answer2_understanding_score_status:'scored',
  answer1_timeliness_verification_status:null,answer2_timeliness_verification_status:null,
  answer2_timeliness_na_reason:'insufficient_evidence'};
app.results.value=[compact];
assert.equal(app.filteredResults.value.length,1,'compact score is usable without a collected verification field');
assert.match(app.cell(compact,{key:'timeliness_summary'}),/P1:3分/);
app.results.value=[{...compact,answer1_timeliness_verification_status:'verified'}];
assert.equal(app.filteredResults.value.length,1,'compact scores do not depend on obsolete verification metadata');
app.results.value=[{...compact,answer1_understanding_score:null,answer1_understanding_score_status:'insufficient_evidence'}];
assert.equal(app.filteredResults.value.length,1,'valid understanding NA does not block later dimensions');
const unknown={...compact,answer2_timeliness_applicable:null,answer2_timeliness_na_reason:'applicability_unknown'};
assert.match(app.cell(unknown,{key:'timeliness_summary'}),/适用性待核验/);
for(const changes of [
  {answer1_timeliness_na_reason:'not_applicable'},
  {answer1_timeliness_na_reason:null},
  {answer1_understanding_score:1},
  {answer1_understanding_score:undefined},
  {answer1_understanding_score_status:undefined},
  {answer1_understanding_applicable:undefined},
  {answer1_understanding_score_status:'insufficient_evidence'},
  {answer1_understanding_na_reason:'insufficient_evidence'},
]) {
  app.results.value=[{...compact,...changes}];
  assert.equal(app.filteredResults.value.length,0,'compact invalid state: '+JSON.stringify(changes));
}
app.results.value=[row];
assert.equal(app.filteredResults.value.length,1,'old result uses its own frozen schema under a new task selection');
app.results.value=[{...row,answer1_timeliness_verification_status:null}];
assert.equal(app.filteredResults.value.length,0,'old result must still satisfy old verification rules');
app.results.value=[row];
const summary={product_count:2,
  timeliness_answer1_avg:3,timeliness_answer1_scored:1,timeliness_answer1_eligible:2,
  timeliness_answer1_score_coverage:0.5,timeliness_answer1_applicable_unknown:1,
  timeliness_answer1_score_status_counts:{insufficient_evidence:1,technical_failure:1},
  timeliness_answer1_score_status_rates:{not_applicable:0,blocked_gate:0,blocked_understanding:0.25},
  timeliness_answer1_answer2_paired_count:0};
const stat=Array.from(app.timelinessSummaryRows(summary)).find(r=>r.key==='timeliness-1');
assert.equal(stat.coverage,'50.0%');
assert.equal(stat.unknown,1);
assert.match(stat.states,/技术失败 1/);
assert.match(stat.states,/理解阻断 0（占全部 25.0%）/);
const paired=Array.from(app.timelinessPairedRows(summary)).find(r=>r.key==='timeliness_answer1_answer2');
assert.equal(paired.count,0);
assert.equal(paired.averageA,'N/A');
const three=Array.from(app.timelinessPairedRows({product_count:3}));
assert.equal(three.length,9,'three dimensions times three product pairs');
const html=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/index.html'),'utf8');
assert.ok(html.indexOf('timeliness-evidence-preview')<html.indexOf('timeliness-score-list'));
assert.match(html,/timelinessPairedRows\(summary\)/);
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{
  bundle_revision:'1.1.0',result_schema_version:'qa-chat-result-2'}});
assert.equal(app.taskIsTimeliness.value,false);
assert.ok(app.resultCols.value.some(c=>c.key==='style_summary'));
console.log('Timeliness UI: three dimensions, evidence before scores, strict 1–3 filtering, NA and paired coverage passed');
const humanSource=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/human-compare.js'),'utf8')
  .replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
const human={ref:v=>({value:v}),computed:f=>({get value(){return f();}}),onUnmounted(){},
  AbortController,DOMException,URLSearchParams,FormData,setTimeout,clearTimeout};
vm.runInNewContext(humanSource+'\nthis.Panel=HumanComparePanel;',human);
const panel=human.Panel.setup({taskId:'time-task',evaluationProfile:'timeliness_compare@1.0'});
assert.equal(panel.templateStandard.value,'timeliness_compare@1.0',
  'human template defaults to the frozen task family even if dataset API omits protocol metadata');
assert.equal(Array.from(panel.selectedDimensions.value).join(','),'understanding,authority,timeliness');
assert.equal(Object.keys(panel.baselineDimensions.value).join(','),'understanding,authority,timeliness');
panel.mapping.value={human_standard_id:'qa_chat_compare',human_standard_version:'1.1',human_bundle_revision:'1.1.0',products:[],labels:[]};
panel.changeMappingStandard('timeliness_compare@1.0');
assert.equal(panel.mappingStandard(),'timeliness_compare@1.0');
assert.equal(Object.keys(panel.mappingDimensions.value).length,3);
panel.report.value={human_protocol:{human_standard_id:'timeliness_compare'},metrics:{ranks:[{fake:true}]}};
assert.equal(panel.reportIsTimeliness.value,true);
assert.equal(panel.visibleRanks.value.length,0);
console.log('Timeliness human UI: three mapped dimensions and no rankings passed');
