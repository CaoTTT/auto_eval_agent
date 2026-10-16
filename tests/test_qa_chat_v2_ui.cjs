const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let app;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/app.js'),'utf8').replace(/^import[^\n]+\n/,''),{
  ref:value=>({value}),computed:getter=>({get value(){return getter();}}),onMounted(){},onUnmounted(){},nextTick(){},console,clearTimeout,
  createApp(options){app=options.setup();return {mount(){}};},
});
app.mode.value='compare';
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.1',protocol_manifest:{bundle_revision:'1.1.0',result_schema_version:'qa-chat-result-2'}});
assert.ok(!app.resultCols.value.some(c=>['has_conflict','confidence','rationale'].includes(c.key)),'V2 hides fields outside the collected contract');
assert.equal(app.resultCols.value.filter(c=>c.key.endsWith('_summary')).length,9);
const shared='产品1：原话里提到“产品2”，仍完整保留第一段；产品2：说明不同分档。\n补充：不按中文分句重分配。';
const evidence=['产品1 T02 上部：原文包含；分号','产品2 T02 下部：不同证据'];
const row={index:0,standard_id:'qa_chat_compare',standard_version:'1.1',evaluation_profile:'qa_chat_compare@1.1',result_schema_version:'qa-chat-result-2',product_count:2,
  answer1_input_status:null,answer1_response_gate:'unclear',answer1_safety_gate:'pass',
  answer1_style_score:4,answer1_style_score_status:'scored',answer1_style_applicable:true,answer1_style_verification_status:'partial',
  answer2_input_status:null,answer2_response_gate:'pass',answer2_safety_gate:'pass',
  answer2_style_score:null,answer2_style_score_status:'unverifiable',answer2_style_applicable:true,answer2_style_verification_status:'unverifiable',
  style_reason:shared,style_evidence:evidence,answer1_style_reason:'旧产品理由不可冒充V2事实',answer1_style_primary_issue:'旧摘要不可展示'};
app.results.value=[row];
assert.match(app.cell(row,{key:'style_summary'}),/P1:4分/);
assert.match(app.cell(row,{key:'style_summary'}),/P2:无法核验/);
assert.match(app.cell(row,{key:'input_status_summary'}),/未知（未提供采集结论）/);
const reasons=Array.from(app.cellReasons(row,{key:'style_summary'}));
assert.equal(reasons.length,2);
assert.equal(reasons[0].text,shared,'shared reason is preserved verbatim without product segmentation');
assert.equal(reasons[1].text,evidence.join('；'));
assert.ok(reasons.every(entry=>entry.label.includes('维度共享')));
assert.ok(!reasons.some(entry=>entry.text.includes('旧摘要')||entry.text.includes('旧产品理由')));
app.addScoreRule();
Object.assign(app.scoreRules.value[0],{dimension:'style',operator:'eq',value:4,products:[1]});
assert.equal(app.filteredResults.value.length,1,'unknown capture state does not erase a valid V2 score');
app.selectedEvaluationProfile.value='qa_chat_compare@1.0';
assert.equal(app.filteredResults.value.length,1,'new-task protocol selection does not reinterpret frozen V2 results');
row.answer1_input_status='failed';
assert.equal(app.filteredResults.value.length,0,'trusted capture failure still excludes the product');
row.answer1_input_status=null;row.answer1_safety_gate='fail';
assert.equal(app.filteredResults.value.length,0,'Gate failure still excludes a stale score');
row.answer1_safety_gate='pass';row.answer1_style_score='4';
assert.equal(app.filteredResults.value.length,0,'string scores remain invalid');
row.answer1_style_score=4;
app.setTaskProtocol({evaluation_profile:'qa_chat_compare@1.0',protocol_manifest:{bundle_revision:'1.0.0',result_schema_version:'qa-chat-result-1'}});
assert.ok(app.resultCols.value.some(c=>c.key==='has_conflict'),'V1 keeps its original collected fields');
const old={...row,standard_version:'1.0',evaluation_profile:'qa_chat_compare@1.0',result_schema_version:'qa-chat-result-1'};
app.results.value=[old];
assert.equal(app.filteredResults.value.length,0,'V1 keeps the original required input status');
assert.ok(Array.from(app.cellReasons(old,{key:'style_summary'})).some(entry=>entry.text==='旧产品理由不可冒充V2事实'));
console.log('QA-chat V2 UI: frozen six dimensions, shared verbatim reasons/evidence, unknown capture filtering, hidden uncollected fields and V1 compatibility passed');
