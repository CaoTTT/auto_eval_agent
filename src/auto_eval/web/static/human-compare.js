import {ref, computed, onUnmounted} from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

export const HUMAN_DIMENSIONS = {understanding:'理解需求', service_closure:'服务闭环', scenario_fulfillment:'场景化满足', intuitive_efficiency:'直观高效', evidence_quality:'有理有据', guided_recommendation:'引导推荐', accuracy:'内容准确性（仅留档）'};
export const HumanComparePanel = {
  props:{taskId:{type:String,required:true}},
  setup(props) {
    const open=ref(false),busy=ref(false),error=ref(''),baselines=ref([]),baselineKey=ref(''),baselineSearch=ref(''),baselinePage=ref(1),baselineTotal=ref(0);
    const imports=ref(null),mapping=ref(null),importPreview=ref(null),name=ref(''),validOnly=ref(false),upgrade=ref(false),mappingJson=ref('');
    const tasks=ref([]),availableTasks=ref([]),taskSearch=ref(''),selectedTask=ref(''),compatibility=ref('same_standard');
    const preview=ref(null),previewPage=ref(1),confirmIdentity=ref(false),confirmBinding=ref(false),confirmReason=ref(''),report=ref(null),reports=ref([]);
    const rows=ref([]),rowTotal=ref(0),rowPage=ref(1),rowFilters=ref({product_id:'',dimension_id:'',task_id:'',category:'',direction:'',input_modality:'',evidence_mode:'',min_difference:0,state_mismatch:false});
    const evidence=ref({}),scopeText=ref(''),selectedDimensions=ref(Object.keys(HUMAN_DIMENSIONS).filter(k=>k!=='accuracy'));
    const baseline=computed(()=>baselines.value.find(b=>`${b.baseline_id}:${b.version}`===baselineKey.value));
    const scoreRows=computed(()=>report.value?.metrics?.scores||[]),pairRows=computed(()=>report.value?.metrics?.pairs||[]);
    const statsDimension=ref('understanding'),statsScope=ref('own');
    const visibleScores=computed(()=>scoreRows.value.filter(r=>r.dimension_id===statsDimension.value&&r.scope===statsScope.value));
    const visiblePairs=computed(()=>pairRows.value.filter(r=>r.dimension_id===statsDimension.value&&r.scope===statsScope.value));
    const standards=[['0.2-simplified','V0.2 简化版（1—5）'],['0.2-simplified-calibrated','V0.2 校准版（1—5）'],['0.2-simplified-thinking-exposure','V0.2 过程泄露实验版（1—5）'],['0.3','V0.3（0—3）']];
    const templateStandard=ref('0.2-simplified');
    let generation=0,controllers=new Set(),timer=null,disposed=false,rowGeneration=0,pollGeneration=0;
    async function api(url,options={}) {
      const controller=new AbortController(),epoch=generation;
      controllers.add(controller);
      try {
        const response=await fetch(url,{...options,signal:controller.signal});
        let data;
        try {data=await response.json();} catch {throw Error('服务返回异常（HTTP '+response.status+'），请稍后重试或检查服务日志');}
        if(disposed||epoch!==generation)throw new DOMException('已取消','AbortError');
        if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));
        return data;
      } finally {controllers.delete(controller);}
    }
    const post=(url,data)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    async function run(fn) {busy.value=true;error.value='';try{return await fn();}catch(e){if(e.name!=='AbortError')error.value=e.message;}finally{busy.value=false;}}
    function cancel(){generation++;pollGeneration++;rowGeneration++;for(const c of controllers)c.abort();controllers.clear();clearTimeout(timer);timer=null;}
    function close(){open.value=false;cancel();}
    onUnmounted(()=>{disposed=true;cancel();});
    async function loadBaselines(page=1){const d=await api(`/api/human-baselines?search=${encodeURIComponent(baselineSearch.value)}&page=${page}`);baselines.value=d.items;baselineTotal.value=d.total;baselinePage.value=page;}
    async function loadReports(){reports.value=(await api(`/api/human-comparisons?task_id=${encodeURIComponent(props.taskId)}&page_size=100`)).items;}
    async function show(){open.value=true;await run(async()=>{await loadBaselines();await loadReports();if(!tasks.value.length)await addTask(props.taskId);});}
    async function searchTasks(){await run(async()=>{availableTasks.value=(await api(`/api/datasets?search=${encodeURIComponent(taskSearch.value)}`)).items;});}
    async function addTask(id){
      if(!id||tasks.value.some(t=>t.task_id===id))return;
      const data=await api(`/api/datasets/${encodeURIComponent(id)}`);
      const items=data.items||[],first=items[0]||{},source=first.source_data||{};
      const fields=[...new Set(items.flatMap(i=>Object.keys(i).filter(k=>!['source_data'].includes(k)&&typeof i[k]!=='object')))];
      fields.push(...Object.keys(source).map(k=>'source_data.'+k));
      const count=Math.max(2,...items.map(i=>i.product_count||((i.video3||i.screenshot3||i.answer3)?3:2)));
      tasks.value.push({task_id:id,name:data.dataset_name||id,id_field:fields.includes('id')?'id':fields.includes('source_data.query_id')?'source_data.query_id':fields.includes('query_id')?'query_id':'id',fields,count,product_map:{},caseMapText:'{}'});
      assignProducts();invalidate();
    }
    function assignProducts(){for(const t of tasks.value)for(let n=1;n<=t.count;n++)if(!t.product_map['answer'+n])t.product_map['answer'+n]=baseline.value?.products[n-1]?.product_id||'';}
    function baselineChanged(){for(const t of tasks.value)t.product_map={};assignProducts();invalidate();}
    function invalidate(){preview.value=null;confirmIdentity.value=false;confirmBinding.value=false;confirmReason.value='';}
    async function upload(event){const file=event.target.files?.[0];event.target.value='';if(!file)return;
      await run(async()=>{const body=new FormData();body.append('file',file);imports.value=await api('/api/human-baselines/imports',{method:'POST',body});mapping.value=imports.value.suggested_mapping;mappingJson.value=JSON.stringify(mapping.value,null,2);name.value=file.name.replace(/\.xlsx$/i,'');importPreview.value=null;});}
    async function inspect(){await run(async()=>{const d=await api(`/api/human-baselines/imports/${imports.value.import_id}?sheet_name=${encodeURIComponent(mapping.value.sheet_name)}&header_row=${mapping.value.header_row}`);imports.value={...imports.value,...d};mapping.value=d.suggested_mapping;mappingJson.value=JSON.stringify(mapping.value,null,2);importPreview.value=null;});}
    function mappingChanged(){importPreview.value=null;mappingJson.value=JSON.stringify(mapping.value,null,2);}
    function reuseMapping(){if(!baseline.value?.mapping)return;mapping.value=JSON.parse(JSON.stringify(baseline.value.mapping));mappingChanged();}
    function applyJson(){try{mapping.value=JSON.parse(mappingJson.value);importPreview.value=null;error.value='';}catch(e){error.value='映射 JSON 格式错误：'+e.message;}}
    function addLabel(){mapping.value.labels.push({product_id:mapping.value.products[0].product_id,dimension_id:'understanding',score:'',review:'',status:'',reason:''});mappingChanged();}
    async function parse(){await run(async()=>{importPreview.value=await post(`/api/human-baselines/imports/${imports.value.import_id}/preview`,mapping.value);});}
    async function publish(){await run(async()=>{const selected=baseline.value;
      const d=await post('/api/human-baselines',{import_id:imports.value.import_id,preview_sha256:importPreview.value.preview_sha256,name:name.value,valid_only:validOnly.value,
        baseline_id:upgrade.value&&selected?selected.baseline_id:'',expected_version:upgrade.value&&selected?selected.version:0});
      baselineSearch.value='';await loadBaselines();baselineKey.value=`${d.baseline_id}:${d.version}`;imports.value=null;importPreview.value=null;baselineChanged();});}
    async function match(){await run(async()=>{if(!baseline.value)throw Error('请先选择人工基准');
      preview.value=await post('/api/human-comparisons/preview',{baseline_id:baseline.value.baseline_id,version:baseline.value.version,
        dimensions:selectedDimensions.value,case_ids:scopeText.value?scopeText.value.replace(/\r\n/g,'\n').split('\n').filter(Boolean):null,
        compatibility_mode:compatibility.value,tasks:tasks.value.map(t=>({task_id:t.task_id,id_field:t.id_field,product_map:t.product_map,case_map:JSON.parse(t.caseMapText)}))});
      previewPage.value=1;confirmIdentity.value=false;confirmBinding.value=false;});}
    async function previewRows(page){if(!preview.value)return;const id=preview.value.preview_id;
      await run(async()=>{const d=await api(`/api/human-comparisons/previews/${id}/rows?page=${page}&page_size=50`);if(preview.value?.preview_id===id){preview.value.rows=d.items;previewPage.value=page;}});}
    async function poll(id,epoch){
      if(epoch!==pollGeneration||!open.value||disposed)return;
      try {const d=await api(`/api/human-comparisons/${id}`);if(epoch!==pollGeneration)return;report.value=d;
        if(d.status==='ready'){statsDimension.value=d.config.dimensions[0];await loadRows(1);await loadReports();}
        else if(['queued','generating'].includes(d.status))timer=setTimeout(()=>poll(id,epoch),1000);
      }catch(e){if(e.name!=='AbortError')error.value=e.message;}
    }
    async function viewReport(id){clearTimeout(timer);rowGeneration++;rows.value=[];report.value=null;const epoch=++pollGeneration;await poll(id,epoch);}
    async function generate(){await run(async()=>{const groups=preview.value.confirmations;
      const d=await post('/api/human-comparisons',{preview_id:preview.value.preview_id,config_sha256:preview.value.config_sha256,
        confirm_match_ids:confirmIdentity.value?groups.flatMap(g=>g.match_ids):[],confirm_binding_ids:confirmBinding.value?groups.flatMap(g=>g.binding_ids):[],confirm_reason:confirmReason.value});
      await viewReport(d.report_id);});}
    async function retry(){await run(async()=>{const d=await post(`/api/human-comparisons/${report.value.report_id}/retry`,{});await viewReport(d.report_id);});}
    async function loadRows(page=1){const id=report.value?.report_id;if(!id)return;const epoch=++rowGeneration;
      const params=new URLSearchParams({...rowFilters.value,page,page_size:30});const d=await api(`/api/human-comparisons/${id}/rows?${params}`);
      if(epoch!==rowGeneration||id!==report.value?.report_id)return;rows.value=d.items;rowTotal.value=d.total;rowPage.value=page;evidence.value={};}
    async function showEvidence(row){const key=row.match_id;if(evidence.value[key]){delete evidence.value[key];return;}
      if(!row.evidence){evidence.value[key]={error:'报告没有可用的证据引用'};return;}
      await run(async()=>{const d=await post('/api/dataset-media',{path:row.evidence,role:row.evidence_mode==='long_screenshot'?'screenshot':'video'});evidence.value[key]=d;});}
    function pname(id){return (report.value?.products||baseline.value?.products||[]).find(p=>p.product_id===id)?.display_name||id;}
    const number=v=>v==null?'—':Number(v).toFixed(3).replace(/\.?0+$/,'');
    const percent=v=>v==null?'—':(v*100).toFixed(1)+'%';
    const time=v=>new Date(v*1000).toLocaleString();
    const statusNames={verified:'已核验',confirmed:'已人工确认',identity_unverified:'回答身份待确认',binding_unverified:'历史结果来源待确认',
      content_mismatch:'原始内容冲突',stale_result:'结果与当前输入不符',unmatched:'未匹配',ambiguous:'重复题号或歧义',missing_result:'缺少模型结果',
      scored:'已评分',unlabeled:'未标注',na_unspecified:'NA 原因未说明',not_applicable:'不适用',unverifiable:'无法核验',gate_blocked:'Gate 阻断',
      ready:'已完成',queued:'排队中',generating:'生成中',error:'生成失败',development:'开发调参',regression:'回归验证',holdout:'独立留出集',
      applicability:'维度适用性',response_gate:'响应 Gate',safety_gate:'安全 Gate',own:'各自有效样本',common:'共同有效样本'};
    const statusLabel=v=>statusNames[v]||v;
    const countsText=v=>Object.entries(v||{}).map(([k,n])=>statusLabel(k)+' '+n).join(' · ');
    return {open,busy,error,baselines,baselineKey,baseline,baselineSearch,baselinePage,baselineTotal,imports,mapping,importPreview,name,validOnly,upgrade,mappingJson,
      tasks,availableTasks,taskSearch,selectedTask,compatibility,preview,previewPage,confirmIdentity,confirmBinding,confirmReason,report,reports,rows,rowTotal,rowPage,rowFilters,evidence,
      standards,templateStandard,selectedDimensions,scopeText,dimensions:HUMAN_DIMENSIONS,visibleScores,visiblePairs,statsDimension,statsScope,
      show,close,run,loadBaselines,searchTasks,addTask,baselineChanged,invalidate,upload,inspect,mappingChanged,reuseMapping,applyJson,addLabel,parse,publish,match,previewRows,generate,viewReport,retry,loadRows,showEvidence,pname,number,percent,time,statusLabel,countsText};
  },
  template:`
  <div class="human-comparison">
    <button class="human-entry" @click="open?close():show()">{{open?'收起人工评分对比':'与人工评分对比'}}</button>
    <section v-if="open" class="human-panel" aria-label="人工评分基准对比">
      <div class="human-toolbar"><h3>人工评分基准对比</h3><span class="hint">历史结果直接对比，无须重新评测</span><button @click="close">关闭</button></div>
      <p class="err" role="alert" v-if="error">{{error}}</p><p v-if="busy" role="status">正在处理…</p>
      <fieldset :disabled="busy"><legend>1. 选择或导入人工基准</legend>
        <div class="human-toolbar"><input v-model="baselineSearch" placeholder="搜索人工基准"><button @click="run(()=>loadBaselines(1))">搜索</button>
          <select v-model="baselineKey" @change="baselineChanged" aria-label="人工基准"><option value="">请选择人工基准</option><option v-for="b in baselines" :value="b.baseline_id+':'+b.version">{{b.name}} · v{{b.version}} · {{b.case_count}} 题 · {{b.human_standard_version}}</option></select>
          <button v-if="baselinePage>1" @click="run(()=>loadBaselines(baselinePage-1))">上一页</button><button v-if="baselinePage*20<baselineTotal" @click="run(()=>loadBaselines(baselinePage+1))">下一页</button>
          <label class="op-upload-btn">上传人工 Excel<input type="file" accept=".xlsx" @change="upload" hidden></label>
        </div>
        <div class="human-toolbar"><select v-model="templateStandard" aria-label="模板人工标准"><option v-for="s in standards" :value="s[0]">{{s[1]}}</option></select><a :href="'/api/eval/'+encodeURIComponent(taskId)+'/human-template?standard='+encodeURIComponent(templateStandard)" download>下载当前任务空白标注模板</a></div>
        <p class="hint" v-if="baseline">人工口径：{{baseline.human_policy_note}} · 用途：{{statusLabel(baseline.purpose)}} · 版本发布后保留</p>
        <div v-if="imports && mapping" class="human-import">
          <div class="human-toolbar"><label>工作表 <select v-model="mapping.sheet_name"><option v-for="s in imports.sheets">{{s}}</option></select></label><label>表头行 <input type="number" min="1" max="30" v-model.number="mapping.header_row"></label><button @click="inspect">重新读取表头</button></div>
          <div class="human-toolbar"><label>基准名称 <input v-model="name"></label><label>人工标准 <select v-model="mapping.human_standard_version" @change="mappingChanged"><option value="">请明确选择</option><option v-for="s in standards" :value="s[0]">{{s[1]}}</option></select></label><label>用途 <select v-model="mapping.purpose" @change="mappingChanged"><option value="regression">回归验证</option><option value="development">开发调参</option><option value="holdout">独立留出集</option></select></label></div>
          <label class="human-wide">评分口径及依据 <input v-model="mapping.policy_note" @input="mappingChanged" placeholder="说明人工实际使用的标准；不能只按分值范围推定"></label>
          <div class="human-toolbar"><label>题号列 <select v-model="mapping.case_id" @change="mappingChanged"><option value="">请选择</option><option v-for="c in imports.columns" :value="c.column">{{c.column}} · {{c.header}}</option></select></label><label>问题列 <select v-model="mapping.query" @change="mappingChanged"><option value="">无</option><option v-for="c in imports.columns" :value="c.column">{{c.column}} · {{c.header}}</option></select></label></div>
          <div class="human-toolbar"><label v-for="p in mapping.products">产品名称 <input v-model="p.display_name" @input="mappingChanged"> 身份 <input v-model="p.product_id" @input="mappingChanged"></label></div>
          <p class="hint">表头重复时按 Excel 列地址区分。人工复核分非空优先；空白为未标注，裸 N/A 默认为原因未知。</p>
          <div class="table-scroll"><table class="grid"><thead><tr><th>产品</th><th>维度</th><th>人工分列</th><th>复核列</th><th>状态列</th><th>批注列</th><th></th></tr></thead><tbody><tr v-for="(l,i) in mapping.labels"><td><select v-model="l.product_id" @change="mappingChanged"><option v-for="p in mapping.products" :value="p.product_id">{{p.display_name}}</option></select></td><td><select v-model="l.dimension_id" @change="mappingChanged"><option v-for="(label,key) in dimensions" :value="key">{{label}}</option></select></td><td v-for="k in ['score','review','status','reason']"><select v-model="l[k]" @change="mappingChanged"><option value="">无</option><option v-for="c in imports.columns" :value="c.column">{{c.column}} · {{c.header}}</option></select></td><td><button @click="mapping.labels.splice(i,1);mappingChanged()">移除</button></td></tr></tbody></table></div>
          <button @click="addLabel">增加维度映射</button>
          <details><summary>回答身份、Gate、适用性及高级列映射</summary><p class="hint">responses 为产品 ID → 身份字段 → Excel 列地址；applicability 为维度 → 列地址；gates 为产品 ID → response/safety → 列地址。源文本确认可追溯后才将 origin 设为 source。</p><textarea v-model="mappingJson" rows="14" aria-label="高级列映射 JSON"></textarea><button @click="applyJson">应用高级映射</button></details>
          <button v-if="baseline?.mapping" @click="reuseMapping">复用所选基准的列映射</button><button @click="parse">解析与预览</button>
          <div v-if="importPreview"><p>有效题目 {{importPreview.summary.case_count}} · 标签 {{importPreview.summary.label_count}} · 问题 {{importPreview.summary.issue_count}}</p><p>{{countsText(importPreview.summary.statuses)}}</p><details v-if="importPreview.issues.length" open><summary>需处理的问题</summary><ul><li v-for="(i,n) in importPreview.issues" :key="n">{{i.sheet}} {{i.column}}{{i.row}} · {{i.case_id}}：{{i.message}}</li></ul></details>
            <label v-if="importPreview.issues.length"><input type="checkbox" v-model="validOnly">仅保存有效记录，并保存排除清单</label><label v-if="baseline"><input type="checkbox" v-model="upgrade">发布为所选基准的新版本</label><button @click="publish">保存人工基准</button></div>
        </div>
      </fieldset>
      <fieldset :disabled="busy || !baseline"><legend>2. 匹配任务与原始回答</legend>
        <div class="human-toolbar"><input v-model="taskSearch" placeholder="搜索其他历史任务"><button @click="searchTasks">查找任务</button><select v-model="selectedTask"><option value="">可选：其他 Prompt / 模型任务</option><option v-for="t in availableTasks" :value="t.task_id">{{t.dataset_name||t.task_id}} · {{t.task_id}}</option></select><button @click="run(()=>addTask(selectedTask))">加入对比</button></div>
        <div class="human-task" v-for="(t,i) in tasks" :key="t.task_id"><strong>{{i===0?'入口任务 · ':''}}{{t.name}}</strong><small>{{t.task_id}}</small><div class="human-toolbar"><label>真实题号字段 <select v-model="t.id_field" @change="invalidate"><option v-for="f in t.fields">{{f}}</option></select></label><label v-for="n in t.count">answer{{n}} 对应 <select v-model="t.product_map['answer'+n]" @change="invalidate"><option value="">请选择</option><option v-for="p in baseline?.products||[]" :value="p.product_id">{{p.display_name}}</option></select></label><button v-if="i" @click="tasks.splice(i,1);invalidate()">移除</button></div><details><summary>显式题号映射（可选）</summary><p class="hint">JSON：任务源题号 → 人工题号；不填写时精确匹配，不按行号或模糊问题匹配。</p><textarea v-model="t.caseMapText" @input="invalidate" rows="3"></textarea></details></div>
        <div class="human-toolbar"><label v-for="(label,key) in dimensions" v-show="key!=='accuracy'"><input type="checkbox" :value="key" v-model="selectedDimensions" @change="invalidate">{{label}}</label></div>
        <details><summary>限定本次题目范围（可选）</summary><p class="hint">填写入口任务中的人工题号，一行一个；留空使用入口任务全部题目。</p><textarea v-model="scopeText" @input="invalidate" rows="3" aria-label="本次题号范围"></textarea></details>
        <label>比较口径 <select v-model="compatibility" @change="invalidate"><option value="same_standard">相同人工标准</option><option value="regression">相对既有人工基准的回归比较</option></select></label><button @click="match">生成匹配预览</button>
      </fieldset>
      <fieldset v-if="preview" :disabled="busy"><legend>3. 核对匹配并生成报告</legend><p>范围 {{preview.scope_count}} 题 · 人工有效数字 {{preview.human_n}} 个 · 当前可比 {{preview.comparable_n}} 个（多任务分别计数）</p>
        <p class="hint">报告固定使用本次预览时的结果。重新评测后，需要重新预览。</p><p v-for="(count,task) in preview.case_matching">任务 {{task}}：题号匹配 {{count}} / {{preview.scope_count}} 题</p><p>回答身份（标签数）：{{countsText(preview.matches)}}</p><p>结果绑定（标签数）：{{countsText(preview.bindings)}}</p>
        <details><summary>匹配明细（第 {{previewPage}} 页 / 共 {{preview.row_count}} 条）</summary><div class="table-scroll"><table class="grid"><thead><tr><th>任务 / 题号</th><th>产品 / 维度</th><th>身份</th><th>结果绑定</th><th>原因</th></tr></thead><tbody><tr v-for="r in preview.rows"><td>{{r.task_id}} / {{r.case_id}}</td><td>{{pname(r.product_id)}} / {{dimensions[r.dimension_id]}}</td><td>{{statusLabel(r.match_status)}}</td><td>{{statusLabel(r.result_input_binding)}}</td><td>{{r.match_reasons.join('；')}}；{{r.binding_reasons.join('；')}}</td></tr></tbody></table></div></details>
        <div class="human-toolbar" v-if="preview.row_count>50"><button :disabled="previewPage<=1" @click="previewRows(previewPage-1)">上一页匹配</button><button :disabled="previewPage*50>=preview.row_count" @click="previewRows(previewPage+1)">下一页匹配</button></div><details v-if="preview.out_of_scope.length"><summary>范围外人工题号（不计分母）</summary>{{preview.out_of_scope.join('、')}}</details>
        <div v-if="preview.confirmations.length" class="human-confirm"><details><summary>缺少历史身份信息的 {{preview.confirmations.length}} 组回答</summary><ul><li v-for="c in preview.confirmations">{{c.task_id}} · {{c.case_id}} · {{pname(c.product_id)}}：回答身份 {{c.match_ids.length}} 项，结果绑定 {{c.binding_ids.length}} 项</li></ul></details>
          <label><input type="checkbox" v-model="confirmIdentity">确认以上未核验回答与人工标注来自同一次原始采集</label><label><input type="checkbox" v-model="confirmBinding">确认以上旧模型结果对应本次冻结的原始回答</label><input v-if="confirmIdentity||confirmBinding" v-model="confirmReason" placeholder="填写确认依据（必须）"></div>
        <button @click="generate">生成对比报告</button><p class="hint">明确内容冲突和旧结果冲突自动排除，不能由确认覆盖。</p>
      </fieldset>
      <div class="human-toolbar" v-if="reports.length"><label>已保存报告 <select @change="run(()=>viewReport($event.target.value))"><option value="">选择历史报告</option><option v-for="r in reports" :value="r.report_id">{{r.baseline_name}} v{{r.version}} · {{time(r.created_at)}} · {{statusLabel(r.status)}}</option></select></label></div>
      <section v-if="report" class="human-report"><div class="human-toolbar"><h3>对比报告</h3><span>{{report.baseline_name}} v{{report.version}} · {{report.report_id}}</span><a v-if="report.status==='ready'" :href="'/api/human-comparisons/'+report.report_id+'/download'" download>导出人机对比 Excel</a></div>
        <p v-if="report.status!=='ready'">{{report.status==='error'?report.error:'正在生成报告…'}} <button v-if="report.status==='error'" @click="retry">从冻结输入重试</button></p>
        <template v-else><p class="hint">{{report.compatibility_mode==='regression'?'相对既有人工基准的回归比较':'同标准比较'}} · {{report.warnings.join('；')}}</p>
          <div class="human-toolbar"><label>维度 <select v-model="statsDimension"><option v-for="d in report.config.dimensions" :value="d">{{dimensions[d]}}</option></select></label><label>样本口径 <select v-model="statsScope"><option value="own">各任务自身有效样本</option><option v-if="report.task_ids.length>1" value="common">所有任务共同有效样本</option></select></label></div>
          <div class="table-scroll"><table class="grid"><thead><tr><th>任务</th><th>产品</th><th>可比 / 人工有效</th><th>覆盖率</th><th>人工 / 模型均分</th><th>完全一致率</th><th>差≤1分</th><th>MAE</th><th>平均偏差</th><th>严重分歧</th><th>较首任务</th></tr></thead><tbody><tr v-for="s in visibleScores"><td>{{s.task_id}}</td><td>{{pname(s.product_id)}}</td><td>{{s.n}} / {{s.human_n}}</td><td>{{percent(s.coverage)}}</td><td>{{number(s.human_mean)}} / {{number(s.model_mean)}}</td><td>{{percent(s.exact)}}</td><td>{{percent(s.within_one)}}</td><td>{{number(s.mae)}}</td><td>{{number(s.bias)}}</td><td>{{percent(s.severe)}}</td><td>{{s.vs_first_task?'一致率 '+percent(s.vs_first_task.exact)+'；MAE '+number(s.vs_first_task.mae):'—'}}</td></tr></tbody></table></div>
          <details><summary>分布、混淆矩阵与排除原因</summary><div v-for="s in visibleScores"><strong>{{s.task_id}} · {{pname(s.product_id)}}</strong><pre>人工分布 {{s.human_distribution}}\n模型分布 {{s.model_distribution}}\n混淆矩阵（人工,模型） {{s.confusion}}\n分差 {{s.difference_counts}}\n排除 {{s.excluded}}</pre></div></details>
          <h4>产品差距</h4><div class="table-scroll"><table class="grid"><thead><tr><th>任务</th><th>方向 A/B</th><th>配对数 / 人工数</th><th>人工 / 模型分位值</th><th>偏差（百分点）</th><th>人工 G/S/B</th><th>模型 G/S/B</th><th>胜平负一致率</th><th>反转率</th></tr></thead><tbody><tr v-for="p in visiblePairs"><td>{{p.task_id}}</td><td>{{pname(p.product_a)}} / {{pname(p.product_b)}}</td><td>{{p.n}} / {{p.human_n}}</td><td>{{percent(p.human_ratio)}} / {{percent(p.model_ratio)}}</td><td>{{number(p.ratio_bias_pp)}}</td><td>{{p.human_gsb.join('/')}}</td><td>{{p.model_gsb.join('/')}}</td><td>{{percent(p.outcome_agreement)}}</td><td>{{percent(p.reversal)}}</td></tr></tbody></table></div>
          <p class="hint">分位值沿用均分之比；不代表百分位数。所有数字指标均只使用同一有效样本集合；无分母显示 —。</p>
          <details><summary>人工 Gate / 适用性与三产品排名</summary><table class="grid"><tbody><tr v-for="s in report.metrics.states"><td>{{s.task_id}}</td><td>{{pname(s.product_id)}} {{dimensions[s.dimension_id]}} {{statusLabel(s.kind)}}</td><td>{{s.n?'有效数 '+s.n:'缺少可比人工标签'}}</td><td>{{percent(s.agreement)}}</td></tr><tr v-for="r in report.metrics.ranks"><td>{{r.task_id}}</td><td>{{dimensions[r.dimension_id]}} 三产品排名 · {{statusLabel(r.scope)}}</td><td>{{r.n}}</td><td>{{percent(r.agreement)}}</td></tr></tbody></table></details>
          <h4>分歧与匹配明细</h4><div class="human-toolbar"><select v-model="rowFilters.product_id"><option value="">全部产品</option><option v-for="p in report.products" :value="p.product_id">{{p.display_name}}</option></select><select v-model="rowFilters.dimension_id"><option value="">全部维度</option><option v-for="d in report.config.dimensions" :value="d">{{dimensions[d]}}</option></select><select v-model="rowFilters.task_id"><option value="">全部任务</option><option v-for="t in report.task_ids">{{t}}</option></select><select v-model="rowFilters.direction"><option value="">所有偏差</option><option value="higher">模型偏高</option><option value="lower">模型偏低</option></select><label>最小分差 <input type="number" min="0" v-model.number="rowFilters.min_difference"></label><input v-model="rowFilters.category" placeholder="场景（精确名称）"><select v-model="rowFilters.input_modality"><option value="">文字 / VQA</option><option value="text">文字</option><option value="text_image">VQA</option></select><select v-model="rowFilters.evidence_mode"><option value="">全部证据类型</option><option value="video_frames">录屏</option><option value="long_screenshot">长截图</option></select><label><input type="checkbox" v-model="rowFilters.state_mismatch">状态不一致</label><button @click="run(()=>loadRows(1))">筛选</button></div>
          <div class="table-scroll"><table class="grid"><thead><tr><th>题号 / 任务</th><th>产品 / 维度</th><th>人工 / 模型 / 分差</th><th>状态与排除原因</th><th>评分依据与证据</th></tr></thead><tbody><tr v-for="r in rows" :key="r.match_id"><td>{{r.case_id}}<small>{{r.task_id}}</small><details><summary>问题</summary>{{r.query}}</details></td><td>{{pname(r.product_id)}}<small>{{dimensions[r.dimension_id]}}</small></td><td>{{number(r.human_score)}} / {{number(r.model_score)}} / {{number(r.difference)}}</td><td>{{statusLabel(r.human_status)}} / {{r.model_status}}<small>{{statusLabel(r.match_status)}} / {{statusLabel(r.result_input_binding)}}</small><strong>{{statusLabel(r.exclusion)}}</strong></td><td><details><summary>查看理由与来源</summary><p>人工：{{r.human_reason||'无批注'}} · {{r.human_source.sheet}} {{r.human_source.selected_cell}}</p><p>模型：{{r.model_reason||'未记录'}}</p><p>证据引用：{{r.evidence||'未记录'}}</p><button @click="showEvidence(r)">按需查看证据</button><div v-if="evidence[r.match_id]"><p class="err" v-if="evidence[r.match_id].error">{{evidence[r.match_id].error}}</p><video v-else-if="evidence[r.match_id].preview_url && r.evidence_mode!=='long_screenshot'" :src="evidence[r.match_id].preview_url" controls preload="metadata"></video><img v-else-if="evidence[r.match_id].preview_url" :src="evidence[r.match_id].preview_url" alt="原始回答证据"><p v-else>证据不可用；冻结统计仍可读取。</p></div></details></td></tr></tbody></table></div>
          <div class="human-toolbar"><span>共 {{rowTotal}} 条 · 第 {{rowPage}} 页</span><button :disabled="rowPage<=1" @click="run(()=>loadRows(rowPage-1))">上一页</button><button :disabled="rowPage*30>=rowTotal" @click="run(()=>loadRows(rowPage+1))">下一页</button></div>
        </template>
      </section>
    </section>
  </div>`
};
