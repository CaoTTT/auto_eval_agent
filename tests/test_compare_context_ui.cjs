const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static',name),'utf8');
const strip=code=>code.replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');

async function main(){
  let app,submitted,parsed,sourceUploads=0,preflights=0;
  const sandbox={ref:value=>({value}),computed:fn=>({get value(){return fn();}}),
    onMounted(){},onUnmounted(){},nextTick(){},AbortController,FormData,console,
    confirm(){return true;},alert(message){throw Error(message);},
    createApp(options){app=options.setup();return {mount(){}};},
    async fetch(url,options){
      if(url==='/api/eval'){submitted=JSON.parse(options.body);return {ok:false,json:async()=>({detail:'test stop'})};}
      if(url==='/api/compare/preflight'){preflights++;return {ok:true,json:async()=>({blocked_turn_count:0})};}
      if(url==='/api/parse')return {ok:true,json:async()=>parsed};
      if(url==='/api/upload/dataset-source'){sourceUploads++;return {ok:true,json:async()=>({id:'source'})};}
      if(url.startsWith('/api/history/'))return {ok:!url.endsWith('/missing'),json:async()=>({
        task_id:'restored',mode:'compare',status:'done',items:[base],results:[]})};
      throw Error(url);
    }};
  vm.runInNewContext(strip(read('compare-cases.js')),sandbox);
  vm.runInNewContext(strip(read('app.js')),sandbox);app.mode.value='compare';
  const base={id:'legacy',query:'问题',product_count:2,screenshot1:'a.png',screenshot2:'b.png',
    context:'产品1旧字段背景',context2:'产品2背景'};
  function reuse(item){submitted=null;app.useComparisonDataset({task_id:'old',items:[item]});}
  reuse(base);
  assert.equal(app.opItems.value[0].context1,base.context);
  assert.equal(app.canSubmit.value,true);await app.submit();
  assert.equal(submitted.items[0].context1,base.context);
  assert.equal(submitted.items[0].context,undefined,'legacy context is not resubmitted as shared background');
  reuse({...base,context1:'新产品1背景'});await app.submit();
  assert.equal(submitted.items[0].context1,'新产品1背景');
  for(const value of ['', '   ', null, 7, false]){
    reuse({...base,context1:value});
    assert.equal(app.canSubmit.value,false,'explicit invalid context1 cannot fall back');
    await app.submit();assert.equal(submitted,null);assert.match(app.runError.value,/context1/);
    app.opItems.value[0].context1='修正产品1背景';
    assert.equal(app.canSubmit.value,true,'historical rows remain editable');
  }
  reuse({...base,context2:''});await app.submit();
  assert.equal(submitted,null);assert.match(app.runError.value,/context2/);
  reuse({...base,product_count:3,screenshot3:'c.png'});await app.submit();
  assert.equal(submitted,null);assert.match(app.runError.value,/context3/);
  app.opItems.value[0].context3='产品3背景';await app.submit();
  assert.equal(submitted.items[0].context3,'产品3背景');

  reuse({...base,id:'multi',session_id:'S',turn_index:1,screenshot_scope:'current_turn',context2:''});
  await app.submit();assert.equal(preflights,0,'missing backgrounds block even the preflight request');
  reuse(base);
  const draft=app.opItems.value[0];
  parsed={items:[base],errors:['第2条（Case bad） compare_context_missing: context2 必须是非空字符串'],blocked:true};
  await app.onOpManifestFile({target:{files:[{name:'mixed.jsonl',text:async()=>''}],value:''}});
  assert.equal(app.opItems.value[0],draft,'blocked mixed file preserves previous draft');
  assert.equal(sourceUploads,0,'do not archive or load a partial context-invalid import');
  assert.equal(app.canSubmit.value,false);submitted=null;await app.submit();assert.equal(submitted,null);
  assert.match(app.runError.value,/重新导入/);
  await app.loadHistoryTask('missing');
  assert.equal(app.importReport.value.blocked,true,'failed history loads cannot clear an import block');
  assert.equal(app.canSubmit.value,false);
  await app.loadHistoryTask('restored');
  assert.equal(app.importReport.value,null,'successfully replacing the draft clears the old import report');
  assert.equal(app.errors.value.length,0);
  assert.equal(app.canSubmit.value,true,'an unrelated invalid import cannot block a valid restored dataset');
  await app.onOpManifestFile({target:{files:[{name:'mixed.jsonl',text:async()=>''}],value:''}});
  assert.equal(app.canSubmit.value,false);
  parsed={items:[base],errors:[],blocked:false};
  await app.onOpManifestFile({target:{files:[{name:'fixed.jsonl',text:async()=>''}],value:''}});
  assert.equal(app.canSubmit.value,true);await app.submit();assert.equal(submitted.items[0].context1,base.context);

  const cases=read('compare-cases.js');
  assert.ok(!cases.includes('>共享背景</label>'));
  assert.match(cases,/context\{\{n\}\}，必填/);
  const panel=read('compare-data.js');assert.match(panel,/importReport\.blocked/);
  console.log('Required product contexts: strict alias fallback, per-product blocking, editable history and atomic import passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
