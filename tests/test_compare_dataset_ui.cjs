const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static',name),'utf8');
const strip=code=>code.replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
async function tick(){for(let i=0;i<12;i++)await Promise.resolve();}
async function main(){
  let app,submitted,allowReplace=true,confirmations=0,parsed;
  const raw=Array.from({length:25},(_,i)=>({id:`q${i}`,query:`question ${i}`,product_count:2,
    screenshot1:'a.png',screenshot2:'b.png',answer1:'answer',query_images:i%2?['q.png']:[]}));
  const context={ref:value=>({value}),computed:fn=>({get value(){return fn();}}),
    onMounted(){},onUnmounted(){},nextTick(){},AbortController,console,
    confirm(){confirmations++;return allowReplace;},alert(message){throw Error(message);},
    createApp(options){app=options.setup();return {mount(){}};},
    async fetch(url,options){
      if(url==='/api/eval'){submitted=JSON.parse(options.body);return {ok:false,json:async()=>({detail:'test stop'})};}
      if(url==='/api/parse')return {ok:true,json:async()=>parsed};
      throw Error(url);
    }};
  vm.runInNewContext(strip(read('app.js')),context);app.mode.value='compare';
  app.selectedEvaluationProfile.value='current-standard';
  app.useComparisonDataset({task_id:'source',dataset_name:'old.jsonl',items:raw});
  assert.equal(app.taskId.value,'');assert.equal(app.opItems.value.length,25);
  assert.equal(app.selectedEvaluationProfile.value,'current-standard');
  app.opPage.value=3;app.modalityFilter.value='text_image';await app.submit();
  assert.equal(submitted.items.length,25);assert.equal(submitted.options.dataset_source_task_id,'source');
  assert.equal(submitted.items[1].query_images[0],'q.png');assert.equal(submitted.items[24].screenshot2,'b.png');
  app.opItems.value[0].query='unsaved edit';allowReplace=false;
  app.useComparisonDataset({task_id:'other',dataset_name:'other.jsonl',items:raw});
  assert.equal(app.opItems.value[0].query,'unsaved edit');assert.equal(confirmations,1);
  assert.equal(raw[0].query,'question 0','editing reused data cannot mutate historical response');
  parsed={items:[],errors:['invalid JSONL']};
  await app.onOpManifestFile({target:{files:[{name:'bad.jsonl',text:async()=>''}],value:''}});
  assert.equal(app.datasetName.value,'old.jsonl');assert.equal(app.opItems.value.length,25);
  allowReplace=true;parsed={items:raw.slice(0,2),errors:[]};
  await app.onOpManifestFile({target:{files:[{name:'new.jsonl',text:async()=>''}],value:''}});
  assert.equal(app.datasetName.value,'new.jsonl');assert.equal(app.datasetSourceTaskId.value,'');
  app.opItems.value[1].screenshot2Path='';submitted=null;await app.submit();
  assert.equal(submitted,null);assert.match(app.runError.value,/不会跳过/);

  const requests=[],watchers=[];let cleanup;
  const caseContext={...context,watch(source,fn){watchers.push({source,fn});},onUnmounted(fn){cleanup=fn;},
    fetch(url,options){return new Promise(resolve=>requests.push({url,options,resolve}));}};
  vm.runInNewContext(strip(read('compare-cases.js'))+'\nthis.CaseList=CompareCaseList;this.mapper=fromDatasetItem;',caseContext);
  const props={items:raw.map(caseContext.mapper)},cases=caseContext.CaseList.setup(props);
  assert.equal(cases.rows.value.length,10);assert.equal(requests.length,0);
  const item=props.items[0],file=cases.caseMedia(item)[0];
  cases.toggleCase(item);assert.equal(requests.length,0,'expanding case text must not fetch images');
  cases.toggleMedia(item,file);assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/dataset-media');
  cases.toggleMedia(item,file);assert.equal(requests[0].options.signal.aborted,true);
  requests[0].resolve({ok:true,json:async()=>({preview_url:'/stale.png'})});await tick();
  assert.equal(cases.media.value[cases.key(item,file)],undefined,'late preview cannot reopen a collapsed image');
  cases.changePage(3);assert.equal(cases.rows.value.length,5);cleanup();

  const panelRequests=[],panelContext={...caseContext,nextTick:async()=>{},caseMedia:cases.caseMedia,
    fromDatasetItem:caseContext.mapper,CompareCaseList:caseContext.CaseList,
    fetch(url,options){return new Promise(resolve=>panelRequests.push({url,options,resolve}));}};
  vm.runInNewContext(strip(read('compare-data.js')).replace(/^\{ref, computed, onMounted, onUnmounted, nextTick\};/m,'')+'\nthis.Panel=CompareDatasetPanel;',panelContext);
  const panel=panelContext.Panel.setup({items:[],datasetName:'',revision:0});
  const first=panel.previewDataset('first'),second=panel.previewDataset('second');
  assert.equal(panelRequests[0].options.signal.aborted,true);
  panelRequests[1].resolve({ok:true,json:async()=>({task_id:'second',items:raw})});await second;
  panelRequests[0].resolve({ok:true,json:async()=>({task_id:'first',items:[]})});await first;
  assert.equal(panel.preview.value.task_id,'second','late history response cannot replace the selected preview');
  const pending=panel.previewDataset('closed');panel.closePreview();
  panelRequests[2].resolve({ok:true,json:async()=>({task_id:'closed',items:raw})});await pending;
  assert.equal(panel.preview.value,null,'closing preview cancels pending history detail');
  console.log('Dataset reuse, editing isolation, full submission, rejected imports and click-only previews passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
