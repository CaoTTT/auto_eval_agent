const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function main() {
  let app, submitted, preflight, blocked = false;
  const items = [1,2,3].map(t => ({id:`S-T${t}`,session_id:'S',turn_index:t,screenshot_scope:'current_turn',
    query:`question ${t}`,query_images:t===3?['q1.png','q2.png']:[],screenshot1:`p1-t${t}.png`,screenshot2:`p2-t${t}.png`,product_count:2}));
  const sandbox = {
    ref:value=>({value}),computed:getter=>({get value(){return getter();}}),
    onMounted(){},onUnmounted(){},nextTick:async()=>{},
    createApp(options){app=options.setup();return {mount(){}};},
    console,AbortController,FormData,setTimeout,clearTimeout,
    async fetch(url,options){
      if(url==='/api/parse')return {ok:true,json:async()=>({items,errors:[]})};
      if(url==='/api/compare/preflight'){
        preflight=JSON.parse(options.body);
        return {ok:true,json:async()=>({blocked_turn_count:blocked?1:0,turns:[],findings:[]})};
      }
      if(url==='/api/eval'){submitted=JSON.parse(options.body);return {ok:false,json:async()=>({detail:'test stop'})};}
      if(url.startsWith('/api/history/'))return {ok:true,json:async()=>({task_id:'history',mode:'compare',status:'done',items,results:[]})};
      throw Error(url);
    }
  };
  const root=path.join(__dirname,'../src/auto_eval/web/static');
  vm.runInNewContext(fs.readFileSync(path.join(root,'compare-cases.js'),'utf8').replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,''),sandbox);
  vm.runInNewContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/^import[^\n]+\n/,''),sandbox);
  app.mode.value='compare';
  await app.onOpManifestFile({target:{value:'a',files:[{name:'multi.json',text:async()=>''}]}});
  assert.equal(app.canSubmit.value,true);
  await app.submit();
  assert.deepEqual(submitted.items.map(it=>[it.session_id,it.turn_index,it.screenshot_scope]),[['S',1,'current_turn'],['S',2,'current_turn'],['S',3,'current_turn']]);
  assert.deepEqual(submitted.items[2].query_images,['q1.png','q2.png']);
  assert.deepEqual(preflight,submitted,'preflight uses exactly the submitted model and inputs');
  blocked=true;submitted=null;
  await app.submit();
  assert.equal(submitted,null,'blocked preflight cannot create a paid evaluation');
  assert.match(app.runError.value,/阻断/);
  blocked=false;
  await app.loadHistoryTask('history');
  assert.equal(app.opItems.value[2].sessionId,'S');
  assert.equal(app.opItems.value[2].turnIndex,3);
  await app.submit();
  assert.equal(submitted.items[2].screenshot_scope,'current_turn');
  app.opItems.value[1].video1Path='mixed.mp4';
  assert.equal(app.canSubmit.value,false,'cannot silently discard multi-turn videos');
  console.log('Multi-turn import, preflight, blocking, serialization and history passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
