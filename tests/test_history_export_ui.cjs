const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let app; const requests = [], downloads = [];
let blockDownload = false, removedLinks = 0;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/app.js'),'utf8').replace(/^import[^\n]+\n/,''), {
  ref: value=>({value}), computed: fn=>({get value(){return fn();}}),
  onMounted(){}, onUnmounted(){}, nextTick(){}, console,
  createApp(options){app=options.setup();return {mount(){}};},
  fetch(url, options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));},
  window:{setTimeout(fn){fn();},open(){throw Error('XLSX must not use a popup');}},
  document:{
    body:{appendChild(){}},
    createElement(tag){
      assert.equal(tag,'a');
      return {click(){if(blockDownload)throw Error('browser blocked download');downloads.push({href:this.href,name:this.download});},remove(){removedLinks++;}};
    },
  },
  alert(){},
});
function respond(url,data,ok=true){
  const pos=requests.findIndex(r=>r.url===url);
  assert.ok(pos>=0,`missing request ${url}`);
  requests.splice(pos,1)[0].resolve({ok,json:async()=>data});
}
const snapshot=id=>({task_id:id,mode:'compare',status:'done',items:[{query:id}],results:[{index:0,error:'test'}]});
async function tick(){for(let n=0;n<8;n++)await Promise.resolve();}
(async()=>{
  app.taskId.value='A';
  const b=app.loadHistoryTask('B');
  assert.equal(app.loadingTaskId.value,'B');
  await app.exportXlsx();
  assert.equal(requests.length,1,'current-task export disabled until loading completes');
  const c=app.loadHistoryTask('C');
  respond('/api/history/C',snapshot('C')); await c;
  respond('/api/history/B',snapshot('B')); await b;
  assert.equal(app.taskId.value,'C','late B must not overwrite last selected C');
  assert.equal(app.loadingTaskId.value,'');

  const download=app.exportXlsx('B');
  assert.equal(app.exportingTaskId.value,'B');
  const d=app.loadHistoryTask('D');
  respond('/api/history/D',snapshot('D')); await d;
  respond('/api/eval/B/exports?include_images=true',{export_id:'fixed-B',status:'queued'}); await tick();
  respond('/api/exports/fixed-B',{export_id:'fixed-B',status:'generating'}); await tick();
  respond('/api/exports/fixed-B',{export_id:'fixed-B',status:'ready',filename:'测试数据B_模型测评结果.xlsx'}); await download;
  assert.equal(app.taskId.value,'D','exporting B must not switch visible task');
  assert.equal(app.exportDownloadUrl.value,'/api/exports/fixed-B/download');
  assert.match(app.exportMessage.value,/B/);
  assert.equal(app.exportingTaskId.value,'');
  assert.deepEqual(downloads,[{href:'/api/exports/fixed-B/download',name:'测试数据B_模型测评结果.xlsx'}]);
  assert.equal(removedLinks,1);

  blockDownload=true;
  const blocked=app.exportXlsx('B');
  respond('/api/eval/B/exports?include_images=true',{export_id:'blocked',status:'ready'}); await blocked;
  assert.equal(app.exportDownloadUrl.value,'/api/exports/blocked/download');
  assert.equal(app.exportError.value,'','a blocked automatic download must retain the ready state');
  assert.equal(removedLinks,2);
  blockDownload=false;

  const failed=app.exportXlsx('B');
  respond('/api/eval/B/exports?include_images=true',{detail:'queue full'},false); await failed;
  assert.match(app.exportError.value,/queue full/);
  assert.equal(app.exportDownloadUrl.value,'');
  const retry=app.exportXlsx('B');
  respond('/api/eval/B/exports?include_images=true',{export_id:'retry',status:'error',error:'disk full'}); await retry;
  assert.match(app.exportError.value,/disk full/);
  const loadError=app.loadHistoryTask('gone');
  requests.shift().reject(Error('network failed')); await loadError;
  assert.equal(app.loadingTaskId.value,'');
  assert.equal(app.taskId.value,'D');
  assert.match(app.runError.value,/network failed/);
  assert.equal(app.exportIncludeImages.value, true);
  app.exportIncludeImages.value = false;
  const small = app.exportXlsx();
  assert.ok(requests.some(r => r.url === '/api/eval/D/exports?include_images=false'));
  app.exportIncludeImages.value = true; // Changing the UI does not change an in-flight export.
  respond('/api/eval/D/exports?include_images=false', {export_id:'small',status:'ready',filename:'数据_不含原图.xlsx'});
  await small;
  assert.match(app.exportMessage.value, /不含原图/);
  assert.equal(downloads.at(-1).name, '数据_不含原图.xlsx');
  app.exportIncludeImages.value = false;
  const smallHistory = app.exportXlsx('B');
  respond('/api/eval/B/exports?include_images=false', {export_id:'small-history',status:'ready'});
  await smallHistory;
  app.comparisonTaskIds.value = ['A', 'B'];
  const comparison = app.exportComparisonXlsx();
  const request = requests.find(r => r.url === '/api/exports/comparison');
  assert.deepEqual(JSON.parse(request.options.body).task_ids, ['A', 'B']);
  app.comparisonTaskIds.value = ['C', 'D'];
  respond('/api/exports/comparison', {export_id:'pair', status:'ready', filename:'对比.xlsx'});
  await comparison;
  assert.equal(app.exportDownloadUrl.value, '/api/exports/pair/download');
  assert.match(app.exportMessage.value, /A \/ B/);
  assert.equal(app.taskId.value, 'D');
  assert.equal(requests.length,0);
  console.log('History switching, explicit-task export, polling, errors and retry passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
