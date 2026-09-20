import {ref, computed, onUnmounted} from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

export const ScreenshotEvidence = {
  props:{taskId:String,itemIndex:Number},
  setup(props) {
    const opened=ref(false), loading=ref(false), error=ref(''), record=ref(null), imageErrors=ref({});
    const base=computed(()=>`/api/eval/${encodeURIComponent(props.taskId)}/items/${props.itemIndex}`);
    let controller;
    async function toggle() {
      opened.value=!opened.value;
      if(!opened.value){controller?.abort();loading.value=false;return;}
      if(record.value)return;
      controller=new AbortController();const active=controller;
      loading.value=true;error.value='';
      try {
        const response=await fetch(`${base.value}/screenshot-evidence`,{signal:active.signal});
        const data=await response.json();
        if(!response.ok)throw Error(data.detail || '证据记录加载失败');
        if(controller===active && !active.signal.aborted)record.value=data;
      } catch(e) {if(e.name!=='AbortError' && controller===active)error.value=e.message;}
      finally {if(controller===active)loading.value=false;}
    }
    onUnmounted(()=>controller?.abort());
    const pretty=value=>JSON.stringify(value,null,2);
    const state=value=>({assembled:'请求已组装',model_call_started:'已开始模型调用',
      model_response_received:'已收到模型响应',request_not_recorded:'历史任务：实际请求未记录'})[value] || value;
    return {opened,loading,error,record,imageErrors,base,toggle,pretty,state};
  },
  template:`<section class="screenshot-audit">
    <div class="dataset-toolbar"><button @click="toggle" :aria-expanded="opened">{{opened?'收起':'查看'}}长截图预处理与输入序列</button>
      <a :href="base+'/export?format=screenshot_evidence'">下载本 Case 证据 ZIP</a></div>
    <div v-if="opened">
      <p v-if="loading">正在加载证据记录…</p><p v-if="error" class="err">{{error}}；可收起后重试。</p>
      <template v-if="record">
        <p><strong>{{state(record.record_status)}}</strong> · {{record.images.length}} 张已记录证据图片</p>
        <details><summary>请求版本、模型与预算</summary><pre class="case-text">{{pretty({version:record.version,judge_model:record.judge_model,protocol:record.protocol,request_sha256:record.request_sha256,composition:record.composition,request_budget_report:record.request_budget_report,image_findings:record.image_findings})}}</pre></details>
        <p v-if="record.notice" class="err">{{record.notice}}</p>
        <p class="hint">原图或切片按序输入，无像素拼接。图片顺序与下方 Prompt 中的图片编号对应；文件变化或缺失时不提供替代图片。</p>
        <details v-for="(entry,i) in record.preprocessing" :key="i" class="audit-parameters">
          <summary>产品 {{entry.product_no}} <span v-if="entry.source_turn">· 来源第 {{entry.source_turn}} 轮</span> · {{entry.metadata.split_status==='original'?'未切分，原图输入':entry.metadata.split_count?'已切分':'未准备'}} · {{entry.metadata.split_count || 0}} 张</summary>
          <p>原图 {{entry.metadata.original_width}} × {{entry.metadata.original_height}} px · {{entry.metadata.original_file_bytes}} 字节 · 算法 {{entry.metadata.algorithm_version || '未记录'}}</p>
          <p>切分状态 {{entry.metadata.split_status || '未记录'}} · 重叠 {{entry.metadata.overlap_pixels ?? '未记录'}} px · 耗时 {{entry.metadata.prepare_latency_ms ?? '未记录'}} ms</p>
          <pre class="case-text">{{pretty(entry.metadata)}}</pre>
        </details>
        <div class="evidence-gallery">
          <figure v-for="picture in record.images" :key="picture.image_no" class="evidence-card">
            <figcaption>图片 {{picture.image_no}} · {{picture.image_role==='query_image'?'共享题图':'产品 '+picture.product_no}} <span v-if="picture.source_turn">· 第 {{picture.source_turn}} 轮</span> · {{picture.request_role==='history'?'历史上下文':picture.request_role==='current'?'当前轮':''}} <span v-if="picture.part_no">· 第 {{picture.part_no}}/{{picture.part_count}} 块</span></figcaption>
            <p v-if="imageErrors[picture.image_no]" class="err">图片缺失、已变化或无法校验，请查看 ZIP 清单。</p>
            <a v-else :href="base+'/screenshot-evidence/'+picture.image_no" target="_blank" rel="noopener" class="evidence-image-scroll is-long-screenshot"><img :src="base+'/screenshot-evidence/'+picture.image_no" :alt="'证据图片 '+picture.image_no" loading="lazy" @error="imageErrors[picture.image_no]=true"></a>
            <small>{{picture.width}} × {{picture.height}} px <span v-if="picture.start_y!=null">· 原图纵坐标 [{{picture.start_y}}, {{picture.end_y}})</span></small>
            <details><summary>图片完整参数</summary><pre class="case-text">{{pretty(picture)}}</pre></details>
          </figure>
        </div>
        <details><summary>实际输入 Prompt 与图片位置</summary>
          <p v-if="record.record_status==='request_not_recorded'">此历史任务未记录实际 Prompt。</p>
          <template v-else><strong>System Prompt</strong><pre class="case-text">{{record.system_prompt}}</pre>
          <div v-for="(part,i) in record.content_sequence" :key="i"><strong>User 第 {{i+1}} 段</strong><pre v-if="part.type==='text'" class="case-text">{{part.text}}</pre><p v-else>【插入图片 {{part.image_no}}】</p></div></template>
        </details>
      </template>
    </div>
  </section>`
};
