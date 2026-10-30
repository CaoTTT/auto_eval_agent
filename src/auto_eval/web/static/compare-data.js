import {createApp as vueCreateApp, ref, computed, watch, onMounted, onUnmounted, nextTick} from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";
import {CompareCaseList, fromDatasetItem, caseMedia, caseEvidenceMode, selectEvidenceMode, isRemoteUrl, productContextValue, missingProductContexts} from "./compare-cases.js?v=20261028_snapshot_product_contexts";
import {HumanComparePanel} from "./human-compare.js?v=20261030_chitchat_flat";
import {ScreenshotEvidence} from "./screenshot-evidence.js?v=20261030_product_order";
export {ref, computed, onMounted, onUnmounted, nextTick};
export {selectEvidenceMode, isRemoteUrl, productContextValue, missingProductContexts};

export const SourceFileDownloads = {
  props:{taskId:String,files:Array},
  setup(props) {
    const downloads=computed(()=>(props.files || []).filter(file=>file.id && props.taskId).map(file=>({
      ...file, url:`/api/history/${encodeURIComponent(props.taskId)}/source-files/${encodeURIComponent(file.id)}`,
    })));
    return {downloads};
  },
  template:`<span v-if="downloads.length"><a v-for="file in downloads" :key="file.id" class="history-text-link" style="display:inline-block;margin-right:8px" :href="file.url" :download="file.filename" :title="file.filename">下载源文件{{downloads.length>1?'：'+file.filename:''}}</a></span><span v-else class="hint">未保存源文件</span>`,
};

export const CompareDatasetPanel = {
  components:{CompareCaseList,SourceFileDownloads},
  props:{items:Array,datasetName:String,sourceTaskId:String,busy:Boolean,errors:Array,importReport:Object,revision:Number},
  emits:['upload','use','add','remove','query-upload','query-path'],
  setup(props) {
    const historyOpen=ref(false),historyRows=ref([]),historyPage=ref(1),historyTotal=ref(0),search=ref('');
    const historyLoading=ref(false),historyError=ref(''),preview=ref(null),previewItems=ref([]),previewLoading=ref('');
    const fileIssues=ref([]),fileChecked=ref(false),fileChecking=ref(false),fileError=ref('');
    const previewPanel=ref(null),currentPanel=ref(null);
    let historyController=null,previewController=null,fileController=null,disposed=false;
    async function loadDatasets(page=1) {
      historyController?.abort();const controller=new AbortController();historyController=controller;
      historyLoading.value=true;historyError.value='';
      try {
        const r=await fetch(`/api/datasets?page=${page}&search=${encodeURIComponent(search.value)}`,{signal:controller.signal});
        const data=await r.json();if(!r.ok)throw Error(data.detail || '历史数据加载失败');
        if(historyController!==controller || disposed)return;
        historyRows.value=data.items;historyPage.value=data.page;historyTotal.value=data.total;
      } catch(e){if(e.name!=='AbortError' && historyController===controller)historyError.value=e.message;}
      finally {if(historyController===controller)historyLoading.value=false;}
    }
    function toggleHistory(){historyOpen.value=!historyOpen.value;if(historyOpen.value)loadDatasets();else{historyController?.abort();closePreview();}}
    function closePreview(){previewController?.abort();previewController=null;previewLoading.value='';preview.value=null;previewItems.value=[];}
    async function previewDataset(id) {
      closePreview();const controller=new AbortController();previewController=controller;
      previewLoading.value=id;historyError.value='';
      try {
        const r=await fetch(`/api/datasets/${encodeURIComponent(id)}`,{signal:controller.signal});
        const data=await r.json();if(!r.ok)throw Error(data.detail || '数据预览失败');
        if(previewController!==controller || disposed)return;
        preview.value=data;previewItems.value=data.items.map(fromDatasetItem);
        await nextTick();if(previewController===controller && !disposed)previewPanel.value?.scrollIntoView({block:'start',behavior:'smooth'});
      } catch(e){if(e.name!=='AbortError' && previewController===controller)historyError.value=e.message;}
      finally {if(previewController===controller)previewLoading.value='';}
    }
    async function checkFiles() {
      fileController?.abort();const controller=new AbortController();fileController=controller;
      fileChecking.value=true;fileChecked.value=false;fileIssues.value=[];fileError.value='';
      try {
        const files=props.items.flatMap((item,index)=>caseMedia(item).map(file=>({path:file.inputPath || file.path,role:file.role,index,label:file.label})));
        const r=await fetch('/api/dataset-files/validate',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({files})});
        const data=await r.json();if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'文件检查失败');
        if(fileController!==controller || disposed)return;
        fileIssues.value=data.issues;fileChecked.value=true;
      } catch(e){if(e.name!=='AbortError' && fileController===controller)fileError.value=e.message;}
      finally {if(fileController===controller)fileChecking.value=false;}
    }
    watch(()=>props.revision,async()=>{closePreview();historyOpen.value=false;checkFiles();await nextTick();if(!disposed)currentPanel.value?.scrollIntoView({block:'start',behavior:'smooth'});});
    onMounted(()=>{if(props.datasetName)checkFiles();});
    watch(()=>JSON.stringify(props.items.map(caseMedia)),()=>{
      fileController?.abort();fileController=null;fileChecking.value=false;fileChecked.value=false;fileIssues.value=[];fileError.value='';
    },{flush:'sync'});
    onUnmounted(()=>{disposed=true;historyController?.abort();previewController?.abort();fileController?.abort();});
    const counts=items=>({image:items.filter(i=>i.queryImages?.length).length,
      shots:items.filter(i=>caseEvidenceMode(i)==='long_screenshot').length,
      videos:items.filter(i=>caseEvidenceMode(i)==='video_frames').length,
      invalid:items.filter(i=>!caseEvidenceMode(i)).length,
      missingContexts:items.filter(i=>missingProductContexts(i).length).length});
    return {historyOpen,historyRows,historyPage,historyTotal,search,historyLoading,historyError,preview,previewItems,previewLoading,
      previewPanel,currentPanel,fileIssues,fileChecked,fileChecking,fileError,loadDatasets,toggleHistory,closePreview,previewDataset,checkFiles,counts,
      time:value=>value?new Date(value*1000).toLocaleString():'时间未记录'};
  },
  template:`
    <div class="compare-dataset-panel">
      <div class="dataset-toolbar dataset-source-actions">
        <label class="op-upload-btn"><input type="file" accept=".jsonl,.json,.csv,application/json,text/csv" @change="$emit('upload',$event)" :disabled="busy" hidden>上传新测评数据</label>
        <button @click="toggleHistory" :aria-expanded="historyOpen" :disabled="busy">{{historyOpen?'收起历史数据':'选择历史数据'}}</button>
        <span v-if="busy" class="hint">正在解析…</span>
      </div>
      <details class="dataset-format-help"><summary>JSON / JSONL · 查看格式与证据选择规则</summary><p>JSON 支持数组或单个对象，JSONL 每行一个 Case。各产品必须提供非空 context1、context2，三产品还需 context3。仅当 context1 字段不存在时，兼容读取 context 作为产品1背景；显式空值不回退。任何 Case 缺少产品背景时整份导入暂停，请补齐后重新导入。可同时填写 screenshot1/2 与 video1/2；screenshotN、videoN 和 query_images 均可填本地路径或 HTTP(S) URL，URL 在评测时由后端下载并保留原始来源。所有产品长截图齐全时优先使用长截图，否则只有所有产品录屏齐全才回退录屏。无法统一证据层的 Case 拒绝导入，其余 Case 正常载入。可选 query_images 单张提问原图；三产品填写 product_count: 3 及产品3字段。证据选择按路径字段是否完整判断，文件可用性可在下方检查。相对路径以服务器项目目录为基准。自定义字段会保留到 Excel 导出。</p></details>
      <section v-if="historyOpen" class="dataset-history">
        <div class="dataset-toolbar"><strong>历史测评数据</strong><input v-model="search" placeholder="搜索文件名、备注或任务编号" @keyup.enter="loadDatasets(1)" aria-label="搜索历史数据"><button @click="loadDatasets(1)">搜索 / 刷新</button></div>
        <p class="hint">复用该次任务提交的数据；同名文件的不同任务分别保留，不会覆盖原结果。</p>
        <p v-if="historyError" class="err" role="alert">{{historyError}}</p><p v-if="historyLoading" class="hint">正在读取历史数据…</p>
        <div class="table-scroll" v-else><table class="grid dataset-history-table"><thead><tr><th>测试文件 / 备注</th><th>用例数</th><th>评测时间</th><th>操作</th></tr></thead><tbody>
          <tr v-for="row in historyRows" :key="row.task_id"><td><strong>{{row.dataset_name || '未命名数据'}}</strong><small>{{row.note || row.task_id}}</small></td><td>{{row.total}}</td><td>{{time(row.created_at)}}</td><td><button @click="previewDataset(row.task_id)" :disabled="previewLoading===row.task_id">{{previewLoading===row.task_id?'加载中…':'预览 Case'}}</button> <source-file-downloads :task-id="row.task_id" :files="row.source_files"></source-file-downloads></td></tr>
        </tbody></table><p v-if="!historyRows.length" class="hint">暂无匹配的垂域视觉对比数据。</p></div>
        <div class="pagination" v-if="historyTotal>10"><span>共 {{historyTotal}} 份 · 每页 10 份</span><button @click="loadDatasets(historyPage-1)" :disabled="historyPage<=1 || historyLoading">上一页</button><span>{{historyPage}} / {{Math.ceil(historyTotal/10)}}</span><button @click="loadDatasets(historyPage+1)" :disabled="historyPage>=Math.ceil(historyTotal/10) || historyLoading">下一页</button></div>
        <section v-if="preview" class="dataset-preview" ref="previewPanel">
          <div class="dataset-toolbar"><strong>预览：{{preview.dataset_name || '未命名数据'}}</strong><button @click="closePreview">关闭预览</button></div>
          <p class="hint">来源：{{time(preview.created_at)}} · {{preview.task_id}}</p><p class="hint">{{previewItems.length}} 条 · 图文 {{counts(previewItems).image}} · 文字 {{previewItems.length-counts(previewItems).image}} · 长截图 {{counts(previewItems).shots}} · 录屏 {{counts(previewItems).videos}}</p>
          <source-file-downloads :task-id="preview.task_id" :files="preview.source_files"></source-file-downloads>
          <compare-case-list :key="preview.task_id" :items="previewItems" readonly title="预览 Case"></compare-case-list>
          <button class="primary" @click="$emit('use',preview)" :disabled="!preview.items.length || busy">使用这份数据，新建评测</button>
        </section>
      </section>
      <div class="dataset-current" ref="currentPanel">
        <p v-if="items.some(it=>it.sessionId)" class="hint">多轮：{{new Set(items.filter(it=>it.sessionId).map(it=>it.sessionId)).size}} 个会话 · {{items.filter(it=>it.sessionId).length}} 轮。共享逐轮问题和题图；每张长截图只含当前轮。每轮使用完整原始历史独立评分，开始时自动检查图片与请求预算。</p>
        <strong>当前数据：{{datasetName || '手动录入'}}</strong><span class="dataset-tag">{{sourceTaskId?'历史复用':'新上传 / 手动录入'}}</span>
        <p class="hint" v-if="sourceTaskId">来源任务：{{sourceTaskId}}</p>
        <p class="hint">共 {{items.length}} 条 · 图文 {{counts(items).image}} · 文字 {{items.length-counts(items).image}} · 长截图 {{counts(items).shots}} · 录屏 {{counts(items).videos}} · 证据不齐全 {{counts(items).invalid}} · 产品背景未齐全 {{counts(items).missingContexts}}。开始评估时提交全部 Case。</p>
        <p v-if="counts(items).missingContexts" class="err" role="alert">请展开标记缺少 context 的 Case，补齐每个产品的背景后才能开始评估。</p>
      </div>
      <div v-if="importReport" class="dataset-import-report" role="status">
        <strong>{{importReport.filename}}：{{importReport.blocked?'校验通过':'接受'}} {{importReport.accepted}} 条，拒绝 {{importReport.rejected}} 条</strong>
        <p v-if="importReport.blocked" class="err" role="alert">产品背景缺失或无效，本次文件未载入，已阻止开始评估。请按错误明细补齐文件后重新导入，或重新选择历史数据。</p>
        <p v-else-if="importReport.accepted" class="hint">已载入：长截图 {{importReport.screenshots}} 条，录屏 {{importReport.videos}} 条。拒绝的 Case 已从本次导入中剔除，不会参与评测。</p>
        <p v-else class="err">本文件没有可评测的 Case，当前输入数据未被替换。</p>
      </div>
      <details v-if="errors?.length" class="dataset-import-errors" open><summary class="err">{{errors.length}} 条拒绝原因（对应 Case 未载入）</summary><pre>{{errors.join('\\n')}}</pre></details>
      <div class="dataset-toolbar"><button @click="checkFiles" :disabled="fileChecking">{{fileChecking?'检查文件中…':'检查文件是否可用'}}</button><span v-if="fileChecked" :class="fileIssues.length?'err':'ok'">{{fileIssues.length?'发现 '+fileIssues.length+' 项文件问题':'文件路径检查通过'}}</span><span v-if="fileError" class="err">{{fileError}}</span></div>
      <details v-if="fileIssues.length" class="dataset-import-errors"><summary>查看文件问题（不会改变用例内容）</summary><p v-for="(issue,i) in fileIssues" :key="i" class="err">Case {{issue.index+1}} · {{issue.label}}：{{issue.message}}</p></details>
      <compare-case-list :items="items" @add="$emit('add')" @remove="$emit('remove',$event)" @query-upload="(event,index)=>$emit('query-upload',event,index)" @query-path="(item,path)=>$emit('query-path',item,path)"></compare-case-list>
    </div>`
};

export function createApp(options) {
  return vueCreateApp({...options, components:{...options.components,CompareDatasetPanel,HumanComparePanel,ScreenshotEvidence,SourceFileDownloads}});
}
