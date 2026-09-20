const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

async function main(){
  const requests=[];let cleanup;
  const context={ref:value=>({value}),computed:fn=>({get value(){return fn();}}),
    onUnmounted(fn){cleanup=fn;},AbortController,
    fetch(url,options){return new Promise(resolve=>requests.push({url,options,resolve}));}};
  const source=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/screenshot-evidence.js'),'utf8');
  vm.runInNewContext(source.replace(/^import[^\n]*\n/gm,'').replace(/^export /gm,'')+'\nthis.Component=ScreenshotEvidence;',context);
  const panel=context.Component.setup({taskId:'task /A',itemIndex:2});
  assert.equal(requests.length,0,'opening a task must not eagerly fetch every prompt or image');
  const first=panel.toggle();
  assert.equal(requests[0].url,'/api/eval/task%20%2FA/items/2/screenshot-evidence');
  await panel.toggle();
  assert.equal(requests[0].options.signal.aborted,true);
  requests[0].resolve({ok:true,json:async()=>({images:[{image_no:999}]})});await first;
  assert.equal(panel.record.value,null,'late reply cannot reopen a closed panel');
  const second=panel.toggle();
  requests[1].resolve({ok:false,json:async()=>({detail:'record missing'})});await second;
  assert.equal(panel.error.value,'record missing');
  await panel.toggle();
  const third=panel.toggle();
  const record={record_status:'request_not_recorded',images:[],preprocessing:[]};
  requests[2].resolve({ok:true,json:async()=>record});await third;
  assert.equal(panel.record.value,record);
  assert.match(panel.state(record.record_status),/实际请求未记录/);
  cleanup();assert.equal(requests[2].options.signal.aborted,true);
  console.log('Screenshot evidence lazy loading, retry, cancellation and legacy status passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
