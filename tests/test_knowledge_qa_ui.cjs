const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'../src/auto_eval/web/static');
let app;
vm.runInNewContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/^import[^\n]+\n/,''),{
  ref:value=>({value}),computed:getter=>({get value(){return getter();}}),onMounted(){},onUnmounted(){},nextTick(){},console,clearTimeout,
  createApp(options){app=options.setup();return {mount(){}};},
});
const dims=['accuracy','clarity_and_structure','content_quality','multimedia_value'];
const profile={id:'knowledge_qa_compare@1.0',standard_id:'knowledge_qa_compare',display:'知识问答V1·0-2分（候选）',
  modes:['compare'],score_range:[0,2],result_schema_version:'knowledge-qa-result-2',bundle_revision:'1.0.0'};
app.mode.value='compare';
app.evaluationProfiles.value=[{id:'qa_competitor_compare@0.2-simplified',modes:['compare'],display:'原标准'},profile];
assert.ok(app.compareProfiles.value.some(p=>p.id===profile.id),'API metadata exposes the selectable standard');
app.setTaskProtocol({evaluation_profile:profile.id,protocol_manifest:profile});
assert.equal(app.taskIsKnowledgeQA.value,true);
assert.deepEqual(Array.from(app.scoreDimensions.value,d=>d.key),dims,'new protocol has four filters, no legacy Gate');
assert.deepEqual(Array.from(app.summaryDimensions.value,d=>d.key),dims,'accuracy participates in the new summary');
assert.equal(app.resultCols.value.filter(c=>c.key.endsWith('_summary')&&!c.key.startsWith('input_')).length,4);
assert.ok(!app.resultCols.value.some(c=>/gate|style_summary|has_conflict|rationale/.test(c.key)));
assert.equal(app.resultCols.value.find(c=>c.key==='accuracy_summary').label,'准确性');
const row={index:0,standard_id:'knowledge_qa_compare',standard_version:'1.0',evaluation_profile:profile.id,
  result_schema_version:profile.result_schema_version,product_count:2};
for(const dim of dims){row[`${dim}_reason`]=`产品1：截图顶部短原话，实际表现和分档依据；产品2：截图底部短原话，实际影响及分档依据。`;
  for(const n of [1,2]){row[`answer${n}_${dim}_status`]='scored';row[`answer${n}_${dim}_score_status`]='scored';row[`answer${n}_${dim}_score`]=2;}}
row.answer1_accuracy_score=0;
row.answer2_accuracy_score=1.5;
row.answer1_multimedia_value_score=null;
row.answer1_multimedia_value_status=row.answer1_multimedia_value_score_status='not_applicable';
row.answer2_multimedia_value_score=null;
row.answer2_multimedia_value_status=row.answer2_multimedia_value_score_status='unverifiable';
app.results.value=[row];
assert.equal(app.knowledgeQAColumn(row,{key:'accuracy_summary'}),true);
assert.equal(app.cellReasons(row,{key:'accuracy_summary'}).length,1,'merged reason is shown once without an evidence field');
assert.equal(app.cellReasons(row,{key:'accuracy_summary'})[0].text,row.accuracy_reason);
assert.match(app.cell(row,{key:'accuracy_summary'}),/P1:0分；P2:1.5分/);
assert.match(app.cell(row,{key:'multimedia_value_summary'}),/P1:NA · 不适用；P2:NA · 无法核验/);
assert.deepEqual(Array.from(app.knowledgeQAProductScores(row,{key:'accuracy_summary'}),e=>e.className),['knowledge-zero','knowledge-partial']);
app.addScoreRule();
const rule=app.scoreRules.value[0];
assert.equal(rule.dimension,'accuracy','default new filter is a valid dimension');
Object.assign(rule,{operator:'eq',value:0,products:[1]});
assert.equal(app.filteredResults.value.length,1,'zero remains a valid filter value');
Object.assign(rule,{value:1.5,products:[2]});
assert.equal(app.filteredResults.value.length,1,'half marks remain exact in filtering');
app.selectedEvaluationProfile.value='qa_chat_compare@1.1';
assert.equal(app.filteredResults.value.length,1,'new-task selection does not reinterpret frozen knowledge scores');
for(const changes of [
  {answer2_accuracy_score:'1.5'}, {answer2_accuracy_score:true}, {answer2_accuracy_score:0.5},
  {answer2_accuracy_status:'unverifiable',answer2_accuracy_score_status:'unverifiable'},
  {answer2_input_status:'failed'}, {answer2_execution_status:'input_failed'},
]){app.results.value=[{...row,...changes}];assert.equal(app.filteredResults.value.length,0,JSON.stringify(changes));}
app.clearScoreRules();
app.results.value=[{...row,answer2_clarity_and_structure_score:1.5}];
assert.match(app.cell(app.results.value[0],{key:'clarity_and_structure_summary'}),/结果缺失或异常/,'half marks are accuracy-only');
app.results.value=[{...row,answer1_accuracy_status:'not_applicable',answer1_accuracy_score_status:'not_applicable',answer1_accuracy_score:null}];
assert.match(app.cell(app.results.value[0],{key:'accuracy_summary'}),/结果缺失或异常/,'NA is media-only');
const summary={product_count:2,accuracy_answer1_avg:0,accuracy_answer1_scored:1,accuracy_answer1_eligible:2,
  accuracy_answer1_score_coverage:0.5,accuracy_answer1_score_distribution:{0:1,1:0,'1.5':0,2:0},
  accuracy_answer1_score_status_counts:{unverifiable:1},accuracy_answer1_answer2_paired_count:1,
  accuracy_answer1_answer2_paired_answer1_avg:0,accuracy_answer1_answer2_paired_answer2_avg:1.5};
const stat=Array.from(app.knowledgeQASummaryRows(summary)).find(r=>r.key==='accuracy-1');
assert.equal(stat.average,0);assert.equal(stat.coverage,'50.0%');assert.match(stat.distribution,/0分 1/);assert.match(stat.states,/无法核验 1/);
const pair=Array.from(app.timelinessPairedRows(summary)).find(r=>r.key==='accuracy_answer1_answer2');
assert.equal(pair.averageA,0);assert.equal(pair.averageB,1.5);
assert.equal(app.timelinessPairedRows({product_count:3}).length,12,'four dimensions and three product pairs');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
assert.ok(html.indexOf('knowledgeQAColumn(r,c)')<html.indexOf('knowledge-qa-score-list'),'merged evidence and reason render before product scores');
assert.match(html,/knowledgeQASummaryRows\(summary\)/);
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{result_schema_version:'qa-chat-result-2'}});
assert.equal(app.taskIsKnowledgeQA.value,false);
assert.ok(app.scoreDimensions.value.some(d=>d.gate),'legacy Gate filters remain available');
assert.ok(app.resultCols.value.some(c=>c.key==='style_summary'));
const humanSource=fs.readFileSync(path.join(root,'human-compare.js'),'utf8').replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
const human={ref:v=>({value:v}),computed:f=>({get value(){return f();}}),onUnmounted(){},
  AbortController,DOMException,URLSearchParams,FormData,setTimeout,clearTimeout};
vm.runInNewContext(humanSource+'\nthis.Panel=HumanComparePanel;',human);
const panel=human.Panel.setup({taskId:'knowledge-task',evaluationProfile:profile.id});
assert.equal(panel.templateStandard.value,profile.id);
assert.deepEqual(Array.from(panel.selectedDimensions.value),dims);
assert.deepEqual(Object.keys(panel.selectableBaselineDimensions.value),dims,'human comparison includes accuracy');
assert.ok(panel.standards.some(s=>s[0]===profile.id));
panel.mapping.value={human_standard_id:'knowledge_qa_compare',human_standard_version:'1.0',products:[{product_id:'p1'}],labels:[]};
assert.equal(panel.mappingStandard(),profile.id);
panel.addLabel();
assert.equal(panel.mapping.value.labels[0].dimension_id,'accuracy');
panel.report.value={human_protocol:{human_standard_id:'knowledge_qa_compare'},metrics:{ranks:[{fake:true}]}};
assert.equal(panel.reportIsKnowledgeQA.value,true);assert.equal(panel.visibleRanks.value.length,0);
assert.equal(panel.number(0),'0');assert.equal(panel.number(1.5),'1.5');
console.log('Knowledge QA UI: selectable metadata, four dimensions without Gate, zero/half scores, merged reasons, status display, summaries and human mapping passed');
