// Use the actual Vue setup functions to verify all fields survive UI operations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function main() {
  let app, submitted;
  const items = [
    {id:'text-video', query:'text', video1:'a.mp4', video2:'b.mp4'},
    {id:'image-video', query:'image', query_images:['q.png'], video1:'a.mp4', video2:'b.mp4'},
    {id:'text-shot', query:'text', screenshot1:'a.png', screenshot2:'b.png'},
    {id:'image-shot', query:'image', query_images:['q.png'], screenshot1:'a.png', screenshot2:'b.png'},
  ];
  const sandbox = {
    ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
    onMounted() {}, onUnmounted() {}, nextTick: async () => {},
    createApp(options) {app=options.setup(); return {mount(){}};},
    console, AbortController, FormData, setTimeout, clearTimeout,
    alert(message) {throw Error(message);},
    async fetch(url, options) {
      if(url === '/api/parse') return {ok:true, json:async()=>({items,errors:[]})};
      if(url === '/api/eval') {
        submitted=JSON.parse(options.body);
        return {ok:false,json:async()=>({detail:'test stop'})};
      }
      if(url.startsWith('/api/history/')) return {ok:true,json:async()=>({task_id:'history',mode:'compare',status:'done',items,results:[],summary:null})};
      throw Error(url);
    },
  };
  const source=fs.readFileSync(path.join(__dirname,'../src/auto_eval/web/static/app.js'),'utf8');
  vm.runInNewContext(source.replace(/^import[^\n]+\n/,''),sandbox);
  app.mode.value='compare';
  await app.onOpManifestFile({target:{value:'a',files:[{name:'mixed.jsonl',text:async()=>''}]}});
  assert.equal(app.modalityCounts.value.text,2);
  assert.equal(app.modalityCounts.value.text_image,2);
  app.modalityFilter.value='text_image';
  await app.submit();
  assert.equal(submitted.items.length,4,'Display filter cannot remove submitted samples');
  assert.deepEqual(submitted.items.map(it=>it.query_images),[[],['q.png'],[],['q.png']]);
  assert.deepEqual(submitted.items.map(it=>it.video1 || it.screenshot1),['a.mp4','a.mp4','a.png','a.png']);
  app.setQueryImagePath(app.opItems.value[1],'changed.png');
  app.setQueryImagePath(app.opItems.value[3],'');
  await app.submit();
  assert.deepEqual(submitted.items[1].query_images,['changed.png']);
  assert.deepEqual(submitted.items[3].query_images,[]);
  // A prepared history restores the immutable question image and its preview.
  items[1].query_images=['runs/frozen.png'];
  items[1].query_image_meta=[{image_id:'test',preview_url:'/api/query-images/test'}];
  const historyFn = app.loadHistoryTask;
  assert.ok(historyFn, 'history handler must be exposed');
  await historyFn('history');
  assert.equal(app.opItems.value[1].queryImages[0],'runs/frozen.png');
  assert.equal(app.opItems.value[1].queryImageMeta[0].preview_url,'/api/query-images/test');
  const queryPictures = app.evidenceImages({index:1});
  assert.equal(queryPictures.length,1);
  assert.equal(queryPictures[0].downloadUrl,'/api/query-images/test?original=true');
  const screenshots = app.evidenceImages({index:2});
  assert.equal(screenshots.length,2);
  assert.ok(screenshots[0].previewUrl.startsWith('/api/eval/history/items/2/screenshots/1?revision='));
  assert.ok(screenshots[1].downloadUrl.endsWith('&download=true'));
  assert.ok(screenshots.every(image=>image.longScreenshot));
  assert.equal(app.itemArtifactUrl({index:2},'video'),'');
  assert.equal(app.evidenceImages({index:0}).length,0,'video rows must not acquire screenshot previews');
  items[3].product_count=3;
  items[3].screenshot3='c.png';
  items[3].query_image_meta=items[1].query_image_meta;
  assert.equal(app.evidenceImages({index:3}).length,4,'one query image plus three product screenshots');
  // Image folding is page-local and must not alter case inputs or results.
  app.modalityFilter.value='';
  app.results.value=Array.from({length:15},(_,index)=>({index}));
  const inputsBefore=JSON.stringify(app.opItems.value);
  const resultsBefore=JSON.stringify(app.results.value);
  app.setPageEvidenceExpanded(false);
  assert.equal(app.evidenceExpanded({index:0}),false);
  assert.equal(app.evidenceExpanded({index:10}),true);
  app.setEvidenceExpanded({index:0},true);
  assert.equal(app.evidenceExpanded({index:0}),true);
  assert.equal(app.evidenceExpanded({index:1}),false);
  app.resultPage.value=2;
  app.setPageEvidenceExpanded(false);
  assert.equal(app.evidenceExpanded({index:10}),false);
  app.setPageEvidenceExpanded(true);
  assert.equal(app.evidenceExpanded({index:10}),true);
  assert.equal(app.evidenceExpanded({index:1}),false,'page two controls must not expand page one');
  assert.equal(JSON.stringify(app.opItems.value),inputsBefore);
  assert.equal(JSON.stringify(app.results.value),resultsBefore);
  app.taskId.value='another-task';
  assert.equal(app.evidenceExpanded({index:1}),true,'another task has independent folding state');
  console.log('VQA mixed import, edit, filter, submit and history checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
