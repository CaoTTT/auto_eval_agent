const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');
const plain = value => JSON.parse(JSON.stringify(value));

async function main() {
  let app, parsed, submitted, validation, retry;
  const sandbox = {
    ref:value => ({value}), computed:getter => ({get value(){return getter();}}),
    onMounted(){}, onUnmounted(){}, watch(){}, nextTick:async()=>{}, AbortController,
    createApp(options){app=options.setup();return {mount(){}};},
    console:{log(){},warn(){},error(){}}, confirm:()=>true,
    alert(message){throw Error(message);},
    async fetch(url, options) {
      if (url === '/api/parse') return {ok:true,json:async()=>parsed};
      if (url === '/api/eval') {
        submitted=JSON.parse(options.body);
        return {ok:false,json:async()=>({detail:'test stop'})};
      }
      if (url === '/api/dataset-files/validate') {
        validation=JSON.parse(options.body);
        return {ok:true,json:async()=>({issues:[]})};
      }
      if (url.endsWith('/retries')) {
        retry=JSON.parse(options.body);
        return {ok:false,json:async()=>({detail:'test stop'})};
      }
      throw Error(url);
    }
  };
  vm.runInNewContext(strip(read('compare-cases.js'))+'\nthis.CaseList=CompareCaseList;',sandbox);
  vm.runInNewContext(strip(read('app.js')),sandbox);
  app.mode.value='compare';

  for (const count of [2,3]) {
    const item={id:`joint-${count}`,query:'补充地图卡片',product_count:count,
      evidence_mode:'long_screenshot_with_video',query_images:['question.png']};
    for(let n=1;n<=count;n++){
      item[`screenshot${n}`]=`product${n}.png`;item[`video${n}`]=`product${n}.mp4`;
    }
    parsed={items:[item],errors:[],evidence_counts:{long_screenshot_with_video:1}};
    await app.onOpManifestFile({target:{value:'',files:[{name:'joint.json',text:async()=>''}]}});
    assert.equal(app.importReport.value.joint,1);
    assert.equal(app.canSubmit.value,true);
    await app.submit();
    assert.equal(submitted.items[0].evidence_mode,'long_screenshot_with_video');
    for(let n=1;n<=count;n++){
      assert.equal(submitted.items[0][`screenshot${n}`],item[`screenshot${n}`]);
      assert.equal(submitted.items[0][`video${n}`],item[`video${n}`]);
    }
    const draft=app.opItems.value[0],files=sandbox.caseMedia(draft);
    assert.equal(files.length,1+count*2);
    assert.equal(new Set(files.map(file=>file.id)).size,files.length,'both media roles have separate preview identities');
    assert.deepEqual(plain(files.slice(1).map(file=>file.role)),Array.from({length:count},()=>['screenshot','video']).flat());
    assert.equal(sandbox.caseMedia(sandbox.fromDatasetItem(item,0)).length,1+count*2,'history retains joint evidence');

    draft.video2Path='';
    assert.equal(app.canSubmit.value,false,'joint never silently falls back to screenshots');
    submitted=null;await app.submit();assert.equal(submitted,null);
    assert.match(sandbox.evidenceModeMessage(draft),/每个产品都必须/);
    draft.video2Path=item.video2;
    draft.sessionId='session';draft.screenshotScope='current_turn';
    assert.equal(sandbox.selectEvidenceMode(draft),'','multi-turn joint mode is rejected');
    assert.match(sandbox.evidenceModeMessage(draft),/多轮仅支持/);
  }

  const legacy={productCount:2,evidenceMode:'video_frames',screenshot1Path:'a.png',screenshot2Path:'b.png',video1Path:'a.mp4',video2Path:'b.mp4'};
  assert.equal(sandbox.selectEvidenceMode(legacy),'long_screenshot','legacy complete screenshots still take precedence');
  const joint={...legacy,evidenceMode:'long_screenshot_with_video'};
  vm.runInNewContext(strip(read('compare-data.js')).replace(/^\{ref, computed, onMounted, onUnmounted, nextTick\};/m,'')+'\nthis.Panel=CompareDatasetPanel;',sandbox);
  const panel=sandbox.Panel.setup({items:[joint],datasetName:'',revision:0});
  assert.equal(panel.counts([joint]).joint,1);assert.equal(panel.counts([joint]).invalid,0);
  await panel.checkFiles();
  assert.deepEqual(validation.files.map(file=>file.role),['screenshot','video','screenshot','video']);

  app.taskId.value='task';app.items.value=[{evidence_mode:'long_screenshot_with_video',screenshot1:'a.png',video1:'a.mp4'}];
  assert.match(app.itemArtifactUrl({index:0},'video'),/format=video/,'joint retains video export despite screenshot presence');
  app.results.value=[
    {index:0,item_id:'blocked',error:'too long',retry_allowed:false,context_limit_error:{reason:'输入超过模型限制'},input_diagnostic_status:'blocked',
      request_budget_report:{estimated_input_tokens:12000,output_reserve_tokens:2000,effective_limits:{max_input_tokens:10000,context_window:12000}}},
    {index:1,item_id:'timeout',error:'timeout'}
  ];
  assert.deepEqual(plain(app.failedResultIndexes.value),[1]);
  app.toggleRetryIndex(0);assert.equal(app.selectedRetryIndexes.value.length,0);
  app.toggleRetryIndex(1);assert.deepEqual(plain(app.selectedRetryIndexes.value),[1]);
  await app.retryFailedCases([0,1]);assert.deepEqual(retry.indexes,[1],'terminal context errors cannot be manually resubmitted');
  assert.equal(app.contextLimitReason(app.results.value[0]),'输入超过模型限制');
  assert.match(app.requestDiagnosticText(app.results.value[0]),/输入上限 10000/);
  const trace=JSON.parse(app.diagnosticJson(app.results.value[0]));
  assert.equal(trace.task_id,'task');assert.equal(trace.item_id,'blocked');assert.equal(trace.retry_allowed,false);
  app.diagnosticFilter.value='context_limit';assert.equal(app.filteredResults.value.length,1);
  const template=sandbox.CaseList.template;
  assert.match(template,/value="long_screenshot_with_video" :disabled="!!entry.item.sessionId"/);
  assert.match(template,/screenshot'\+n\+'Path/);assert.match(template,/video'\+n\+'Path/);
  console.log('Joint evidence import, dual previews/checks, 2/3-product submission, multi-turn rejection and terminal budget errors passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
