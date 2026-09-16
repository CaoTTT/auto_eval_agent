const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/human-compare.js'),'utf8').replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'');
async function ticks(){for(let n=0;n<15;n++)await Promise.resolve();}
async function main(){
  let cleanup;const requests=[],timers=[];
  const context={ref:v=>({value:v}),computed:f=>({get value(){return f();}}),onUnmounted:f=>cleanup=f,console,
    AbortController,DOMException,URLSearchParams,FormData,setTimeout:f=>(timers.push(f),timers.length),clearTimeout(){},
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
  vm.runInNewContext(source+'\nthis.Panel=HumanComparePanel;',context);
  const panel=context.Panel.setup({taskId:'task-one'});panel.open.value=true;
  const first=panel.viewReport('old'),second=panel.viewReport('new');
  requests[1].resolve({ok:true,json:async()=>({report_id:'new',status:'generating'})});await second;
  requests[0].resolve({ok:true,json:async()=>({report_id:'old',status:'error'})});await first;
  assert.equal(panel.report.value.report_id,'new','late report response must not replace new selection');
  assert.equal(timers.length,1,'only selected report polls');
  panel.close();timers[0]();await ticks();assert.equal(requests.length,2,'closed panel stops polling');
  panel.open.value=true;
  const pending=panel.viewReport('pending');panel.close();assert.equal(requests[2].options.signal.aborted,true);
  requests[2].resolve({ok:true,json:async()=>({report_id:'pending',status:'generating'})});await pending;
  assert.equal(panel.report.value,null,'closed panel ignores in-flight response');
  panel.open.value=true;panel.report.value={report_id:'rows'};
  const row1=panel.loadRows(1),row2=panel.loadRows(2);
  requests[4].resolve({ok:true,json:async()=>({items:[{match_id:'page2'}],total:50})});await row2;
  requests[3].resolve({ok:true,json:async()=>({items:[{match_id:'page1'}],total:50})});await row1;
  assert.equal(panel.rows.value[0].match_id,'page2');
  panel.preview.value={confirmations:[]};panel.confirmIdentity.value=true;panel.invalidate();
  assert.equal(panel.preview.value,null);assert.equal(panel.confirmIdentity.value,false);
  cleanup();assert.equal(panel.number(null),'—');assert.equal(panel.number(0),'0');
  console.log('Human comparison UI: report selection, cancellation, polling, pagination and mapping invalidation passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
