import {ref, computed, watch, onUnmounted} from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

export function fromDatasetItem(item, index) {
  const source = item.source_data || {};
  const value = key => item[key] ?? source[key];
  return {
    _uiKey: `preview-${index}`, id: value('id') || '', query: value('query') || value('question') || '',
    context: value('context') || '', queryImages: [...(item.query_images || [])],
    queryImageMeta: item.query_image_meta || [], productCount: value('product_count') || (value('video3') || value('screenshot3') ? 3 : 2),
    evidenceMode: item.evidence_mode || (value('screenshot1') ? 'long_screenshot' : 'video_frames'),
    ...Object.fromEntries([1,2,3].flatMap(n => [
      [`video${n}Path`, value(`video${n}`) || ''], [`screenshot${n}Path`, value(`screenshot${n}`) || ''],
      [`answer${n}`, value(`answer${n}`) || ''], [`context${n}`, value(`context${n}`) || ''],
      [`screenshotMeta${n}`, item[`screenshot_meta${n}`] || {}],
    ])),
  };
}

export function caseMedia(item) {
  const files = [];
  if (item.queryImages?.[0]) files.push({id:'query', role:'query', label:'提问图片', path:item.queryImages[0], meta:item.queryImageMeta?.[0]});
  for (let n=1;n<=item.productCount;n++) {
    const screenshot = item.evidenceMode === 'long_screenshot';
    const path = item[`${screenshot ? 'screenshot' : 'video'}${n}Path`];
    if (path) files.push({id:`product${n}`, role:screenshot ? 'screenshot' : 'video',
      label:`产品${n}${screenshot ? '回答长截图' : '录屏'}`, path, meta:item[`screenshotMeta${n}`]});
  }
  return files;
}

export const CompareCaseList = {
  props: {items:{type:Array,default:()=>[]}, readonly:Boolean, title:{type:String,default:'Case 列表'}},
  emits:['remove','add','query-upload','query-path'],
  setup(props) {
    const page=ref(1), search=ref(''), expanded=ref({}), media=ref({}), listOpen=ref(true);
    const controllers=new Map();
    const filtered=computed(()=>props.items.map((item,index)=>({item,index})).filter(({item})=>
      `${item.id} ${item.query}`.toLowerCase().includes(search.value.trim().toLowerCase())));
    const pageCount=computed(()=>Math.max(1,Math.ceil(filtered.value.length/10)));
    const rows=computed(()=>filtered.value.slice((Math.min(page.value,pageCount.value)-1)*10,Math.min(page.value,pageCount.value)*10));
    function key(item,file) {return `${item._uiKey}:${file.id}`;}
    function stopMedia() {for(const c of controllers.values())c.abort();controllers.clear();media.value={};}
    function closeMedia(item,file) {const k=key(item,file);controllers.get(k)?.abort();controllers.delete(k);delete media.value[k];}
    async function openMedia(item,file) {
      const k=key(item,file);
      if(media.value[k])return;
      const controller=new AbortController();controllers.set(k,controller);
      media.value[k]={loading:true};
      try {
        const response=await fetch('/api/dataset-media',{method:'POST',signal:controller.signal,
          headers:{'Content-Type':'application/json'},body:JSON.stringify({path:file.path,role:file.role,
            expected_sha256:file.meta?.original_path===file.path ? file.meta.original_sha256 || '' : ''})});
        const data=await response.json();
        if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'预览加载失败');
        if(controllers.get(k)===controller)media.value[k]={...data,loading:false};
      } catch(error) {
        if(error.name!=='AbortError' && controllers.get(k)===controller)media.value[k]={error:error.message};
      }
    }
    function toggleMedia(item,file) {media.value[key(item,file)] ? closeMedia(item,file) : openMedia(item,file);}
    function toggleCase(item) {
      expanded.value[item._uiKey]=!expanded.value[item._uiKey];
      if(!expanded.value[item._uiKey])caseMedia(item).forEach(file=>closeMedia(item,file));
    }
    let batchVersion=0;
    async function allImages(open) {
      const version=++batchVersion;
      if(!open){stopMedia();return;}
      const jobs=[];
      for(const {item} of rows.value) {
        expanded.value[item._uiKey]=true;
        for(const file of caseMedia(item).filter(f=>f.role!=='video'))jobs.push(()=>openMedia(item,file));
      }
      for(let i=0;i<jobs.length && version===batchVersion;i+=4)await Promise.all(jobs.slice(i,i+4).map(run=>run()));
    }
    function changePage(next) {page.value=Math.max(1,Math.min(pageCount.value,Number(next)||1));}
    watch(search,()=>{page.value=1;stopMedia();batchVersion++;});
    watch(page,()=>{stopMedia();batchVersion++;});
    watch(pageCount,count=>{page.value=Math.min(page.value,count);});
    watch(()=>props.items,()=>{page.value=1;expanded.value={};stopMedia();batchVersion++;});
    watch(()=>JSON.stringify(props.items.map(caseMedia)),()=>{stopMedia();batchVersion++;});
    watch(listOpen,open=>{if(!open){stopMedia();batchVersion++;}});
    onUnmounted(()=>{stopMedia();batchVersion++;});
    const hasImages=computed(()=>rows.value.some(({item})=>caseMedia(item).some(f=>f.role!=='video')));
    return {page,search,expanded,media,listOpen,filtered,pageCount,rows,key,caseMedia,toggleCase,toggleMedia,allImages,changePage,hasImages,
      basename:path=>String(path||'').split(/[\\/]/).pop()};
  },
  template:`
    <div class="compare-case-list">
      <div class="dataset-toolbar">
        <button class="dataset-disclosure" @click="listOpen=!listOpen" :aria-expanded="listOpen">{{listOpen?'▾':'▸'}} {{title}} · {{items.length}} 条</button>
        <template v-if="listOpen && hasImages"><button @click="allImages(false)">折叠当前页全部图片</button><button @click="allImages(true)">展开当前页全部图片</button></template>
      </div>
      <div v-if="listOpen">
        <div class="dataset-toolbar"><input v-model="search" placeholder="搜索题号或问题" aria-label="搜索 Case"><span class="hint">共 {{filtered.length}} 条 · 每页最多 10 条</span></div>
        <div v-for="entry in rows" :key="entry.item._uiKey" class="compare-case">
          <div class="compare-case-heading">
            <button class="case-toggle" @click="toggleCase(entry.item)" :aria-expanded="!!expanded[entry.item._uiKey]">
              <span>{{expanded[entry.item._uiKey]?'▾':'▸'}} {{entry.index+1}}</span><span class="case-heading-query">{{entry.item.query || '尚未填写问题'}}</span>
            </button>
            <span class="dataset-tag">{{entry.item.queryImages?.length?'图文':'文字'}}</span><span class="dataset-tag">{{entry.item.evidenceMode==='long_screenshot'?'长截图':'录屏'}}</span><span class="dataset-tag">{{entry.item.productCount}} 产品</span>
          </div>
          <div v-if="expanded[entry.item._uiKey]" class="compare-case-body">
            <div class="dataset-toolbar"><strong>Case {{entry.item.id || entry.index+1}}</strong><button v-if="!readonly && items.length>1" @click="$emit('remove',entry.index)" class="btn-danger">删除此条</button></div>
            <label class="dataset-label">完整问题</label><p v-if="readonly" class="case-text">{{entry.item.query}}</p><textarea v-else v-model="entry.item.query" rows="2" placeholder="用户问题（必填）"></textarea>
            <label class="dataset-label">共享背景</label><p v-if="readonly" class="case-text">{{entry.item.context || '未填写'}}</p><textarea v-else v-model="entry.item.context" rows="2" placeholder="共享背景（可选）"></textarea>
            <div v-if="!readonly" class="dataset-toolbar">
              <label>回答证据 <select v-model="entry.item.evidenceMode"><option value="video_frames">录屏视频</option><option value="long_screenshot">回答长截图</option></select></label>
              <label>产品数 <select v-model.number="entry.item.productCount"><option :value="2">2</option><option :value="3">3</option></select></label>
            </div>
            <div class="case-products"><div v-for="n in entry.item.productCount" :key="n" class="case-product">
              <strong>产品 {{n}}</strong><label class="dataset-label">回答文字</label><p v-if="readonly" class="case-text">{{entry.item['answer'+n] || '未填写'}}</p><textarea v-else v-model="entry.item['answer'+n]" rows="3" placeholder="产品回答文字（可选）"></textarea>
              <label class="dataset-label">产品背景</label><p v-if="readonly" class="case-text">{{entry.item['context'+n] || '未填写'}}</p><input v-else v-model="entry.item['context'+n]" placeholder="产品背景（可选）">
            </div></div>
            <div class="case-media-list"><div v-for="file in caseMedia(entry.item)" :key="file.id" class="case-media">
              <div class="dataset-toolbar"><button @click="toggleMedia(entry.item,file)" :aria-expanded="!!media[key(entry.item,file)]">{{media[key(entry.item,file)]?'收起':'查看'}}{{file.label}}</button><span class="hint">{{basename(file.path)}}</span></div>
              <div v-if="media[key(entry.item,file)]" class="case-media-view">
                <div v-if="media[key(entry.item,file)].preview_url" class="dataset-toolbar"><a :href="media[key(entry.item,file)].preview_url" target="_blank" rel="noopener">{{file.role==='video'?'在新窗口查看':'查看原图'}}</a><a :href="media[key(entry.item,file)].download_url">{{file.role==='video'?'下载录屏':'下载原图'}}</a></div>
                <p v-if="media[key(entry.item,file)].loading" class="hint">正在加载…</p>
                <p v-else-if="media[key(entry.item,file)].error" class="err">{{media[key(entry.item,file)].error}}；请检查文件信息，收起后可重试。</p>
                <template v-else>
                  <video v-if="file.role==='video'" :src="media[key(entry.item,file)].preview_url" controls preload="metadata" @error="media[key(entry.item,file)].error='录屏无法在浏览器播放，可收起后重试或下载原文件'"></video>
                  <div v-else class="case-image-scroll"><img :src="media[key(entry.item,file)].preview_url" :alt="file.label" @error="media[key(entry.item,file)].error='图片加载失败或预览已过期'"></div>
                </template>
              </div>
            </div><p v-if="!caseMedia(entry.item).length" class="hint">尚未关联图片或录屏。</p></div>
            <details class="case-file-info"><summary>文件信息{{readonly?'':'与编辑'}} · {{entry.item.id || '未设置题号'}}</summary>
              <template v-if="readonly"><p v-for="file in caseMedia(entry.item)" :key="file.id" class="case-text">{{file.label}}：{{file.path}}</p></template>
              <template v-else>
                <label class="dataset-label">题号</label><input v-model="entry.item.id" placeholder="Case 唯一编号">
                <label class="dataset-label">提问原图路径（可选）</label><input :value="entry.item.queryImages?.[0] || ''" @input="$emit('query-path',entry.item,$event.target.value)" :disabled="entry.item.queryUploading" placeholder="后端可读取的图片路径">
                <label class="op-upload-btn"><input type="file" accept=".png,.jpg,.jpeg,.webp" hidden :disabled="entry.item.queryUploading" @change="$emit('query-upload',$event,entry.index)">上传 / 替换提问图片</label>
                <button v-if="entry.item.queryImages?.length" @click="$emit('query-path',entry.item,'')" :disabled="entry.item.queryUploading">移除提问图片</button>
                <span v-if="entry.item.queryUploading">上传中…</span><p v-if="entry.item.queryUploadError" class="err">{{entry.item.queryUploadError}}</p>
                <template v-for="n in entry.item.productCount" :key="n"><label class="dataset-label">产品 {{n}} {{entry.item.evidenceMode==='long_screenshot'?'长截图':'录屏'}}路径</label><input v-if="entry.item.evidenceMode==='long_screenshot'" v-model="entry.item['screenshot'+n+'Path']" placeholder="回答长截图路径"><input v-else v-model="entry.item['video'+n+'Path']" placeholder="录屏路径"></template>
              </template>
            </details>
          </div>
        </div>
        <p v-if="!rows.length" class="hint">没有匹配的 Case。</p>
        <div class="pagination" v-if="pageCount>1"><button @click="changePage(page-1)" :disabled="page<=1">上一页</button><span>{{Math.min(page,pageCount)}} / {{pageCount}}</span><button @click="changePage(page+1)" :disabled="page>=pageCount">下一页</button><label>跳至 <input type="number" min="1" :max="pageCount" :value="Math.min(page,pageCount)" @change="changePage($event.target.value)" aria-label="Case 页码"> 页</label></div>
        <button v-if="!readonly" @click="$emit('add')" class="op-add">+ 添加 Case</button>
      </div>
    </div>`
};
