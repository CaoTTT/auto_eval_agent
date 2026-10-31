const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', name), 'utf8');
const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');

async function main() {
  let app, mounted, submitted, restored;
  const ids = [
    'qa_competitor_compare@0.2-simplified', 'qa_competitor_compare@0.2-simplified-plus',
    'qa_competitor_compare@0.3', 'qa_competitor_compare@0.2-simplified-calibrated',
    'qa_competitor_compare@0.2-simplified-thinking-exposure', 'qa_chat_compare@1.0',
    'qa_chat_compare@1.1', 'qa_chat_compare@1.1-demand-calibrated',
    'chitchat_evidence_first_flat@1.1', 'timeliness_compare@1.0',
    'knowledge_qa_compare@1.0', 'qa_competitor_compare@0.2-simplified-1022',
  ];
  const disabled = new Set(ids.slice(2, 6));
  const profiles = ids.map((id, index) => ({id, display:`旧显示名${index}`,
    canonical_name:`服务端标准${index}-V1-P${index}-261031-实验`, bundle_revision:`${index}.0.1`,
    modes:['compare'], selectable:!disabled.has(id), status:index<2?'stable':'experimental', score_range:[1,5]}));
  const catalog = profiles.map(profile => ({evaluation_profile:profile.id,
    bundle_revision:profile.bundle_revision, canonical_name:profile.canonical_name,
    legacy_name:profile.display, selectable:profile.selectable}));
  const requests = [];
  const base = {id:'case', query:'问题', product_count:2, video1:'one.mp4',video2:'two.mp4',
    context1:'产品1背景',context2:'产品2背景'};
  const sandbox = {
    ref:value=>({value}), computed:fn=>({get value(){return fn();}}),
    onMounted(fn){mounted=fn;},onUnmounted(){},nextTick(){},AbortController,FormData,console,
    window:{setInterval(){return 1;},clearInterval(){}},
    confirm(){return true;},alert(message){throw Error(message);},
    createApp(options){app=options.setup();return {mount(){}};},
    async fetch(url,options={}) {
      requests.push({url,options});
      const data = url==='/api/config' ? {evaluation_profiles:profiles,prompt_name_catalog:catalog}
        : url.startsWith('/api/settings') ? {}
        : url.startsWith('/api/history?') ? {items:[],total:0,page:1}
        : url==='/api/queue' ? {running:null,queued:[]}
        : url.startsWith('/api/history/') ? restored : null;
      if(data)return {ok:true,json:async()=>data};
      if(url==='/api/eval'){
        submitted=JSON.parse(options.body);
        return {ok:false,json:async()=>({detail:'test stop'})};
      }
      throw Error(url);
    },
  };
  vm.runInNewContext(strip(read('compare-cases.js')),sandbox);
  vm.runInNewContext(strip(read('app.js')),sandbox);
  await mounted();
  assert.equal(app.promptNameCatalog.value.length,12);
  assert.equal(app.selectedEvaluationProfile.value,ids[0]);
  for(const profile of profiles) assert.equal(app.evaluationProfileOptionLabel(profile),profile.canonical_name);
  app.switchMode('compare');
  app.useComparisonDataset({task_id:'source',items:[base]});
  await app.submit();
  assert.equal(submitted.evaluation_profile,ids[0]);
  assert.ok(!JSON.stringify(submitted).includes('canonical_name'),'display metadata never enters evaluation options or source items');
  for(const id of disabled){
    app.selectedEvaluationProfile.value=id;
    const previous=requests.length;
    await app.submit();
    assert.equal(requests.length,previous,'renaming does not enable unavailable protocols');
  }

  const profile=profiles[8];
  assert.equal(app.evaluationProfileLabel(profile.id),profile.display,'without a revision use the original label');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,bundle_revision:profile.bundle_revision}),profile.canonical_name);
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,bundle_revision:'historical'}),profile.display);
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,bundle_revision:profile.bundle_revision,
    protocol_manifest:{bundle_revision:'historical',display:'冲突记录原名'}}),'冲突记录原名');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,bundle_revision:profile.bundle_revision,
    protocol_name_identity_conflict:true}),profile.display,'identity conflicts cannot acquire a canonical name through the summary fallback');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,protocol_manifest:{bundle_revision:'historical',display:'冻结的旧名字'}}),'冻结的旧名字');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,evaluation_profile_display:'服务器冻结标签'}),'服务器冻结标签');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:profile.id,legacy_evaluation_profile_display:'旧缓存原名'}),'旧缓存原名');
  assert.equal(app.evaluationProfileLabel({evaluation_profile:'unknown@1.0',bundle_revision:'1.0.0'}),'unknown@1.0');
  assert.equal(app.evaluationProfileOptionLabel({id:'unknown'}),'unknown');
  restored={task_id:'history',mode:'compare',status:'done',evaluation_profile:profile.id,
    protocol_manifest:{id:profile.id,bundle_revision:'historical',display:'冻结的旧名字'},items:[base],results:[]};
  await app.loadHistoryTask('history');
  assert.equal(app.taskEvaluationProfileDisplay.value,'冻结的旧名字');
  app.selectedEvaluationProfile.value=profiles[0].id;
  assert.equal(app.taskEvaluationProfileDisplay.value,'冻结的旧名字','new task selection does not rename restored results');
  app.setTaskProtocol({evaluation_profile:profile.id,protocol_manifest:{id:profile.id,bundle_revision:profile.bundle_revision}});
  assert.equal(app.taskEvaluationProfileDisplay.value,profile.canonical_name);
  app.historySearchDraft.value.evaluation_profile=profile.id;
  await app.applyHistorySearch();
  const search=requests.findLast(request=>request.url.startsWith('/api/history?'));
  assert.equal(new URL('http://test'+search.url).searchParams.get('evaluation_profile'),profile.id);

  const humanSandbox={ref:sandbox.ref,computed:sandbox.computed,onUnmounted(){},console};
  vm.runInNewContext(strip(read('human-compare.js'))+'\nthis.Panel=HumanComparePanel;',humanSandbox);
  const human=humanSandbox.Panel.setup({taskId:'history',evaluationProfile:profile.id,
    evaluationProfileDisplay:'冻结的旧名字',protocolManifest:restored.protocol_manifest,
    promptNameCatalog:catalog,evaluationProfiles:profiles});
  const standard=human.standards.find(entry=>entry[0]===profile.id);
  assert.equal(human.standardOptionLabel(standard),profile.canonical_name);
  assert.equal(human.standardOptionLabel(standard,true),'冻结的旧名字','template download retains the source task revision');
  assert.equal(human.frozenProtocolLabel({human_standard_id:'chitchat_evidence_first_flat',human_standard_version:'1.1',
    human_bundle_revision:profile.bundle_revision}),profile.canonical_name);
  assert.equal(human.frozenProtocolLabel({human_standard_id:'chitchat_evidence_first_flat',human_standard_version:'1.1',
    human_bundle_revision:'historical'}),profile.id);
  assert.equal(human.frozenProtocolLabel({evaluation_profile:profile.id,protocol_manifest:{bundle_revision:'historical',display:'另一个报告的旧名'}}),'另一个报告的旧名');
  assert.equal(human.frozenProtocolLabel({evaluation_profile:profile.id,bundle_revision:profile.bundle_revision,
    protocol_name_identity_conflict:true}),profile.id);
  human.mapping.value={human_standard_id:'chitchat_evidence_first_flat',human_standard_version:'1.1',
    human_bundle_revision:profile.bundle_revision,products:[],labels:[]};
  human.changeMappingStandard('qa_chat_compare@1.1');
  assert.equal(human.mapping.value.human_standard_id,'qa_chat_compare');
  assert.equal(human.mapping.value.human_standard_version,'1.1');
  assert.equal(human.mapping.value.human_bundle_revision,'');

  const html=read('index.html');
  assert.match(html,/evaluationProfileLabel\(entry\)/);
  assert.match(html,/evaluationProfileLabel\(h\)/);
  assert.match(html,/evaluationProfileOptionLabel\(profile\) \}\}（全部修订）/);
  assert.match(html,/:value="profile\.id"/);
  assert.match(html,/当前结果标准：\{\{taskEvaluationProfileDisplay\}\}/);
  assert.match(read('human-compare.js'),/frozenProtocolLabel\(protocol\)/);
  console.log('Prompt naming UI: API names, unchanged IDs/default/availability, exact revisions, conservative history and human comparison labels passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
