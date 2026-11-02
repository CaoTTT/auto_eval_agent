const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '../src/auto_eval/web/static');
const read = name => fs.readFileSync(path.join(root,name),'utf8');
const strip = source => source.replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
let app;
vm.runInNewContext(strip(read('app.js')), {
  ref:value=>({value}), computed:getter=>({get value(){return getter();}}),
  onMounted(){},onUnmounted(){},nextTick(){},console,clearTimeout,
  createApp(options){app=options.setup();return {mount(){}};},
});
const profile = {id:'dialogue_experience_compare@1.0',standard_id:'dialogue_experience_compare',
  standard_version:'1.0',bundle_revision:'1.0.0',result_schema_version:'dialogue-experience-result-1',
  canonical_name:'对话体验对比 | 标准S1.0 | Prompt P01 | Qwen3.8-Flash | 实验',
  modes:['compare'],score_range:[1,5],has_gates:false};
app.mode.value='compare';
app.evaluationProfiles.value=[profile];
app.setTaskProtocol({evaluation_profile:profile.id,protocol_manifest:profile});
assert.equal(app.taskIsDialogueExperience.value,true);
assert.equal(app.taskIsFlatChat.value,false);
assert.equal(app.taskIsChat.value,false);
assert.equal(app.compareProfiles.value[0].id,profile.id);
assert.deepEqual(Array.from(app.scoreDimensions.value,d=>d.key),['overall']);
assert.deepEqual(Array.from(app.summaryDimensions.value,d=>d.key),['overall']);
assert.ok(app.resultCols.value.some(c=>c.key==='overall_summary'));
assert.ok(app.columnWidth({key:'overall_summary',label:'综合体验'}) >= 400);
assert.ok(!app.resultCols.value.some(c=>/gate|has_conflict|understanding|rationale/.test(c.key)));

function payload(count) {
  const result = {index:0,product_count:count,standard_id:profile.standard_id,
    evaluation_profile:profile.id,result_schema_version:profile.result_schema_version,
    model_product_results:{}};
  for(let product=1;product<=count;product++){
    result[`answer${product}`]=`产品${product}的复制文本`;
    result[`answer${product}_overall_score`]=product===1?3:5;
    result[`answer${product}_overall_score_status`]='scored';
    result[`answer${product}_overall_reason`]=`【类型：知识型；目标：回答本轮；主查：需求覆盖；证据状态：充分】产品${product}的专属理由。`;
    result[`answer${product}_suggestion`]=product===1?'补齐遗漏的条件。':'无';
    result[`answer${product}_domain`]='知识问答';
    result.model_product_results[`answer${product}`]={score:result[`answer${product}_overall_score`],
      reason:result[`answer${product}_overall_reason`],suggestion:result[`answer${product}_suggestion`],domain:'知识问答'};
  }
  return result;
}
const two=payload(2);
app.results.value=[two];
assert.equal(app.dialogueExperienceColumn(two,{key:'overall_summary'}),true);
assert.equal(app.dialogueExperienceColumn(two,{key:'understanding_summary'}),false);
assert.equal(app.flatChatColumn(two,{key:'overall_summary'}),false);
assert.equal(app.knowledgeQAColumn(two,{key:'overall_summary'}),false);
const entries=Array.from(app.dialogueExperienceProducts(two));
assert.deepEqual(entries.map(e=>e.label),['3分','5分']);
assert.deepEqual(entries.map(e=>e.className),['flat-mid','flat-high']);
assert.equal(entries[0].suggestion,'补齐遗漏的条件。');
assert.equal(entries[1].suggestion,'无');
assert.equal(entries[0].domain,'知识问答');
assert.equal(entries[0].reason,two.answer1_overall_reason);
assert.equal(app.cellReasons(two,{key:'overall_summary'}).length,2);
assert.match(app.cell(two,{key:'overall_summary'}),/P1:3分；P2:5分/);
assert.match(app.cell(two,{key:'input_status_summary'}),/P1:未知（未提供采集结论）/);

app.addScoreRule();
const rule=app.scoreRules.value[0];
assert.equal(rule.dimension,'overall');
Object.assign(rule,{operator:'diff_lte',value:2,products:[1,2]});
assert.equal(app.filteredResults.value.length,1);
app.selectedEvaluationProfile.value='qa_chat_compare@1.1';
assert.equal(app.filteredResults.value.length,1,'new selector never reinterprets loaded task');
for(const changes of [
  {answer1_overall_score:'3'},{answer1_overall_score:true},{answer1_overall_score:3.5},
  {answer1_overall_score:0},{answer1_overall_score:6},{answer1_overall_score_status:'unverifiable'},
  {answer1_overall_score_status:null},{answer1_input_status:'failed'},
  {answer1_execution_status:'input_failed'},{error:'technical failure'},
]){
  app.results.value=[{...two,...changes}];
  assert.equal(app.filteredResults.value.length,0,JSON.stringify(changes));
}
app.clearScoreRules();
const failed={...two,answer1_overall_score:null,answer1_input_status:'failed'};
assert.equal(app.dialogueExperienceProducts(failed)[0].label,'采集失败');
const missing={...two,answer1_overall_score:null,answer1_overall_score_status:'unverifiable'};
assert.equal(app.dialogueExperienceProducts(missing)[0].label,'证据不足');
const three=payload(3);
assert.equal(app.dialogueExperienceProducts(three).length,3);
assert.equal(app.cellReasons(three,{key:'overall_summary'}).length,3);
assert.equal(app.dialogueExperienceProducts(three)[2].reason,three.answer3_overall_reason);
const projected={...three};
delete projected.answer3_overall_reason;delete projected.answer3_suggestion;delete projected.answer3_domain;
assert.equal(app.dialogueExperienceProducts(projected)[2].reason,three.model_product_results.answer3.reason);
assert.equal(app.dialogueExperienceProducts(projected)[2].domain,'知识问答');

const summary={product_count:3,overall_answer1_avg:3.5,overall_answer1_scored:2,
  overall_answer1_eligible:3,overall_answer1_score_coverage:2/3,
  overall_answer1_score_distribution:{1:0,2:0,3:1,4:1,5:0},
  overall_answer1_score_status_counts:{input_failed:1},
  overall_answer1_answer2_paired_count:2,overall_answer1_answer2_paired_answer1_avg:3.5,
  overall_answer1_answer2_paired_answer2_avg:4,overall_answer1_answer2_mean_ratio:0.875,
  overall_answer1_answer2_paired_mean_ratio:0.875};
const stat=app.flatChatSummaryRows(summary)[0];
assert.equal(stat.dimension,'综合体验');assert.equal(stat.average,3.5);
assert.equal(stat.scored,2);assert.equal(stat.eligible,3);
assert.match(stat.distribution,/3分 1/);assert.match(stat.states,/采集失败 1/);
assert.equal(app.flatChatRatios(summary).length,3);
assert.equal(app.flatChatRatios(summary)[0].ratio,'87.50%');
assert.equal(app.flatChatRatios(summary)[0].count,2);

const html=read('index.html');
assert.match(html,/taskIsDialogueExperience && summary/);
assert.match(html,/对话体验对比 · 综合体验 1–5分/);
const cell=html.slice(html.indexOf('<div v-if="dialogueExperienceColumn(r,c)"'),html.indexOf('<div v-if="flatChatColumn(r,c)"'));
assert.match(cell,/entry\.reason/);assert.match(cell,/entry\.label/);
assert.match(cell,/entry\.suggestion/);assert.match(cell,/entry\.domain/);
assert.ok(cell.indexOf('entry.reason')<cell.indexOf('entry.label'));
assert.match(html,/!dialogueExperienceColumn\(r,c\)/);

const human={ref:value=>({value}),computed:getter=>({get value(){return getter();}}),onUnmounted(){},
  AbortController,DOMException,URLSearchParams,FormData,setTimeout,clearTimeout};
vm.runInNewContext(strip(read('human-compare.js'))+'\nthis.Panel=HumanComparePanel;',human);
const panel=human.Panel.setup({taskId:'experience-task',evaluationProfile:profile.id});
assert.equal(panel.templateStandard.value,profile.id);
assert.deepEqual(Array.from(panel.selectedDimensions.value),['overall']);
assert.deepEqual(Object.keys(panel.selectableBaselineDimensions.value),['overall']);
assert.equal(panel.statsDimension.value,'overall');
assert.ok(panel.standards.some(s=>s[0]===profile.id));
panel.mapping.value={human_standard_id:profile.standard_id,human_standard_version:'1.0',products:[{product_id:'p1'}],labels:[]};
assert.equal(panel.mappingStandard(),profile.id);
assert.equal(panel.mappingIsDialogueExperience.value,true);
assert.deepEqual(Array.from(panel.mappingLabelColumns.value),['score','review','status','reason']);
panel.addLabel();assert.equal(panel.mapping.value.labels[0].dimension_id,'overall');
panel.report.value={human_protocol:{human_standard_id:profile.standard_id},metrics:{ranks:[{n:2}]}};
assert.equal(panel.reportIsDialogueExperience.value,true);
assert.equal(panel.visibleRanks.value.length,1);
assert.match(read('human-compare.js'),/r\.model_suggestion/);
assert.match(read('human-compare.js'),/r\.model_domain/);
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{result_schema_version:'qa-chat-result-2'}});
assert.equal(app.taskIsDialogueExperience.value,false);
assert.ok(app.scoreDimensions.value.some(d=>d.gate));
assert.ok(app.resultCols.value.some(c=>c.key==='personalization_summary'));
console.log('Dialogue experience UI: original four fields per product, 2/3 products, strict score filters, summaries, human mapping and legacy isolation passed');
