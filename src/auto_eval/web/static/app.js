import { createApp, ref, computed, onMounted, onUnmounted, nextTick, selectEvidenceMode } from "./compare-data.js?v=20260920_audit";

createApp({
  setup() {
    const modes = [
      { key: "rich_content", label: "垂域视觉评测" },
      { key: "compare", label: "垂域视觉对比" },
    ];
    function modeLabel(key) {
      return modes.find((item) => item.key === key)?.label || key;
    }
    const mode = ref("rich_content");
    const isVideoMode = computed(() => true);
    const datasetName = ref("");
    const datasetSourceTaskId = ref("");
    const datasetRevision = ref(0);
    let datasetBaseline = "", datasetImportVersion = 0;
    const items = ref([]);
    let opItemSequence = 0;
    const opItems = ref([newOpItem()]);
    const opPage = ref(1);
    const opJumpPage = ref("");
    const opPreparing = ref(false);
    const errors = ref([]);
    const importReport = ref(null);
    const conversationPreflight = ref(null);
    const judges = ref([]);
    const selectedJudges = ref([]);
    const visibleJudges = computed(() => judges.value);
    const judgeModelProfiles = ref([]);
    const defaultJudgeModelProfile = ref("");
    const selectedJudgeModelProfile = ref("");
    const enableThinking = ref(false);
    const taskJudgeRuntime = ref(null);
    const taskJudgeSummary = ref({});
    const selectedModelProfile = computed(() =>
      judgeModelProfiles.value.find(profile => profile.id === selectedJudgeModelProfile.value) || null
    );
    function changeJudgeModel({ resetConcurrency = false } = {}) {
      const profile = selectedModelProfile.value;
      enableThinking.value = profile?.supports_thinking === true && profile.default_enable_thinking === true;
      const recommended = Math.max(1, Math.min(128, profile?.recommended_concurrency || 4));
      concurrency.value = resetConcurrency || !Number.isInteger(concurrency.value) || concurrency.value < 1
        ? recommended : Math.min(concurrency.value, recommended);
    }
    function thinkingLabel(value) {
      return value === true ? "思考开启" : value === false ? "思考关闭" : "思考未记录";
    }
    function judgeRuntimeLabel(runtime, fallback = {}) {
      const judge = runtime?.judges?.[0];
      const model = judge?.model || fallback.judge_model || "模型未记录";
      return `${model} · ${judge ? thinkingLabel(judge.enable_thinking) : `思考${fallback.enable_thinking_label || "未记录"}`}`;
    }
    function restoreJudgeModel(runtime) {
      const profile = judgeModelProfiles.value.find(candidate => candidate.id === runtime?.profile_id);
      selectedJudgeModelProfile.value = profile?.id || defaultJudgeModelProfile.value;
      changeJudgeModel();
      const thinking = runtime?.judges?.[0]?.enable_thinking;
      if (profile?.supports_thinking === true && typeof thinking === "boolean") enableThinking.value = thinking;
    }
    const evaluationProfiles = ref([]);
    const selectedEvaluationProfile = ref("");
    const compareProfiles = computed(() =>
      evaluationProfiles.value.filter((profile) => (profile.modes || []).includes("compare"))
    );
    const concurrency = ref(4);
    const mediaConcurrency = ref(4);
    const requestPacing = computed(() => {
      if (selectedModelProfile.value) return !!selectedModelProfile.value.request_pacing;
      const selected = judges.value.filter((j) => selectedJudges.value.includes(j.name));
      return selected.length > 0 && selected.every((j) => j.request_pacing);
    });
    const pacingStatus = ref(null);
    const pacingError = ref("");
    let pacingLoading = false;

    function pacingNumber(value, digits = 0) {
      return typeof value === "number" && Number.isFinite(value)
        ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
    }

    function pacingWaitLabel(reason) {
      return ({
        cooldown: "限流冷却", second_window: "连续 1 秒请求额度",
        request_window: "连续 60 秒请求额度", minute_window: "连续 60 秒请求额度",
        token_budget: "Token 预算", pacing: "平滑发送间隔", warmup: "逐步升速",
        inflight: "等待响应槽位", inflight_limit: "等待响应槽位",
      })[reason] || (reason ? "等待发送额度" : "额度可用");
    }

    function pacingLimitLabel(kind) {
      return ({
        requests: "请求频率", request: "请求频率", tokens: "Token 额度",
        token: "Token 额度", burst: "增速", overload: "服务拥塞",
        congestion: "服务拥塞", unknown: "未分类限流",
      })[kind] || (kind ? "未分类限流" : "无");
    }
    const evalTimeout = ref(900);
    const submitting = ref(false);
    const running = ref(false);
    const progress = ref(0);
    const total = ref(0);
    const results = ref([]);
    const summary = ref(null);
    const taskId = ref("");
    const runError = ref("");
    const itemProgress = ref({});
    const progressEvents = ref({});
    const expandedProgressLogs = ref({});
    const resultBrowser = ref(null);
    const activeSkill = ref("");
    const resultQuery = ref("");
    const modalityFilter = ref("");
    const failedOnly = ref(false);
    const failedCaseCount = computed(() => results.value.filter(r => r.error).length);
    function selectFailureFilter(value) {
      failedOnly.value = value;
      activeSkill.value = "";
      resetResultPage();
    }
    const turnFilter = ref(''), diagnosticFilter = ref('');
    const liveInputDiagnostics = ref({});
    const conversationGroups = computed(() => {
      const groups = new Map();
      for (const row of results.value) {
        if (!row.session_id) continue;
        if (!groups.has(row.session_id)) groups.set(row.session_id, []);
        groups.get(row.session_id).push(row);
      }
      return [...groups].map(([id, turns]) => ({id, turns:turns.sort((a,b)=>a.turn_index-b.turn_index)}));
    });
    const modalityCounts = computed(() => ({
      text: opItems.value.filter(it => !(it.queryImages || []).length).length,
      text_image: opItems.value.filter(it => (it.queryImages || []).length).length,
    }));
    function queryImageMetas(r) {
      return r.query_image_meta || items.value[r.index]?.query_image_meta || [];
    }
    const evidenceImageErrors = ref({});
    const collapsedEvidence = ref({});
    function evidenceExpanded(row) {
      return !collapsedEvidence.value[`${taskId.value}:${row.index}`];
    }
    function setEvidenceExpanded(row, expanded) {
      collapsedEvidence.value[`${taskId.value}:${row.index}`] = !expanded;
    }
    function setPageEvidenceExpanded(expanded) {
      pagedResults.value.forEach(row => setEvidenceExpanded(row, expanded));
    }
    const evidenceRevisions = ref({});
    let nextEvidenceRevision = 0;
    function refreshEvidence(resultRows) {
      for (const row of resultRows) {
        if (row && row.index != null) evidenceRevisions.value[row.index] = ++nextEvidenceRevision;
      }
      evidenceImageErrors.value = {};
    }
    function evidenceImages(r) {
      const images = queryImageMetas(r).filter(meta => meta.preview_url).map(meta => ({
        key: meta.preview_url, label: `用户提问图片 ${meta.query_image_id || 'QI1'}`,
        previewUrl: meta.preview_url, downloadUrl: `${meta.preview_url}?original=true`, longScreenshot: false,
      }));
      const index = Number(r.index);
      const item = items.value[index] || {};
      if (!taskId.value || !Number.isInteger(index) || index < 0) return images;
      const source = item.source_data || {};
      if (item.evidence_mode === 'video_frames') return images;
      const count = item.product_count || (item.screenshot3 || source.screenshot3 ? 3 : 2);
      for (let n = 1; n <= count; n++) {
        if (!(item[`screenshot${n}`] || item[`screenshot_meta${n}`]?.original_path || source[`screenshot${n}`])) continue;
        const url = `/api/eval/${encodeURIComponent(taskId.value)}/items/${index}/screenshots/${n}?revision=${evidenceRevisions.value[index] || 0}`;
        images.push({key:url, label:`产品 ${n} 回答长截图`, previewUrl:url, downloadUrl:`${url}&download=true`, longScreenshot:true});
      }
      return images;
    }
    function conversationHistory(r) {
      return items.value.map((item,index)=>({item,index})).filter(({item})=>item.session_id===r.session_id && item.turn_index<r.turn_index)
        .sort((a,b)=>a.item.turn_index-b.item.turn_index).map(({item,index})=>({turn:item.turn_index, query:item.query,
          images:evidenceImages(results.value.find(row=>row.index===index) || {index})}));
    }
    const resultPage = ref(1);
    const resultPageSize = ref(10);
    const progressPage = ref(1);
    const resultJumpPage = ref("");
    const progressJumpPage = ref("");
    const cellTooltip = ref({ visible: false, text: "", style: {} });
    const historyItems = ref([]);
    const historyPage = ref(1);
    const historyTotal = ref(0);
    const historyPageSize = 10;
    const historyPageCount = computed(() => Math.max(1, Math.ceil(historyTotal.value / historyPageSize)));
    const historyError = ref("");
    const historyNoteDrafts = ref({});
    const historyNoteEditing = ref({});
    const loadingHistory = ref(false);
    const loadingTaskId = ref("");
    const exportingTaskId = ref("");
    const exportMessage = ref("");
    const exportError = ref("");
    const exportDownloadUrl = ref("");
    let historyLoadVersion = 0;
    let historyListVersion = 0;
    let historyRequestedPage = 1;
    let historyLoadController = null;
    let disposed = false;
    const queueState = ref({ running: null, queued: [] });
    const selectedTaskStatus = ref("");
    const selectedTaskTiming = ref(null);
    const queueNotice = ref("");
    const repairStatus = ref("idle");
    const retrySubmitting = ref(false);
    const selectedRetryIndexes = ref([]);
    const activeRetry = ref(null);
    const executionControl = ref({});
    const selectedActiveRuns = ref(0);
    const controlSubmitting = ref(false);
    const resumeConcurrency = ref(4);
    const resumeFailed = ref(false);
    const isPausing = computed(() => executionControl.value.state === "pausing");
    const canPauseTask = computed(() => !!taskId.value && (running.value || selectedActiveRuns.value > 0 || ["queued", "running"].includes(repairStatus.value) || executionControl.value.save_error));
    const canResumeTask = computed(() => !!taskId.value && !running.value && !selectedActiveRuns.value && !isPausing.value && !executionControl.value.save_error && !["queued", "running"].includes(repairStatus.value) && ["paused", "error", "cancelled", "done"].includes(selectedTaskStatus.value));
    const clockNow = ref(Date.now());
    let tooltipHideTimer = null;
    let progressClockTimer = null;
    let queueRefreshTimer = null;
    let activeEventSource = null;
    const pageSize = 10;
    const opPageSize = 10;
    const progressStages = ["排队", "分类", "模型/裁判", "聚合", "完成"];
    const queueEntries = computed(() => {
      const entries = [];
      if (queueState.value.running) entries.push(queueState.value.running);
      entries.push(...(queueState.value.queued || []));
      return entries;
    });
    const failedResultIndexes = computed(() =>
      results.value
        .filter((result) => result && result.error && Number.isInteger(Number(result.index)))
        .map((result) => Number(result.index))
    );

    function receiveTaskTiming(timing) {
      return timing && typeof timing === "object" ? { ...timing, _received_at: Date.now() } : null;
    }

    function updateTaskTiming(timing) {
      if (!timing) return;
      const previous = selectedTaskTiming.value;
      if (previous && Number(timing.measured_at) < Number(previous.measured_at)) return;
      selectedTaskTiming.value = receiveTaskTiming(timing);
    }

    function taskElapsedSeconds(timing) {
      if (!timing || typeof timing.elapsed_s !== "number" || !Number.isFinite(timing.elapsed_s)) return null;
      const extra = timing.running && !timing.incomplete && timing.finished_at == null
        ? Math.max(0, (clockNow.value - (timing._received_at ?? clockNow.value)) / 1000) : 0;
      return Math.max(0, timing.elapsed_s + extra);
    }

    function formatTaskDuration(timing) {
      const seconds = taskElapsedSeconds(timing);
      if (seconds == null) return "未记录";
      const whole = Math.floor(seconds);
      const hours = Math.floor(whole / 3600), minutes = Math.floor(whole / 60) % 60, rest = whole % 60;
      const label = hours ? `${hours} 小时 ${minutes} 分 ${rest} 秒`
        : minutes ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
      return timing.incomplete ? `${label}（截至中断前）` : label;
    }

    function retryStatusLabel(status) {
      return ({ idle: "", queued: "补跑排队中", running: "补跑中", completed: "补跑完成", partial: "补跑后仍有失败", error: "补跑异常", cancelled: "补跑已取消", paused: "补跑已暂停" })[status] ?? status;
    }

    function retryIndexSelected(index) {
      return selectedRetryIndexes.value.includes(Number(index));
    }

    function toggleRetryIndex(index) {
      const value = Number(index);
      selectedRetryIndexes.value = retryIndexSelected(value)
        ? selectedRetryIndexes.value.filter((item) => item !== value)
        : [...selectedRetryIndexes.value, value];
    }

    function taskStatusLabel(status) {
      return ({ queued: "排队中", running: "运行中", done: "已完成", error: "失败", cancelled: "已取消", paused: "已暂停", pausing: "正在暂停" })[status] || status;
    }

    function queueKindLabel(kind) {
      return kind === "resume" ? "恢复执行" : kind === "retry" ? "失败补跑" : "全量评测";
    }

    const formatHint = computed(
      () =>
        ({
          compare: "支持文字题与图文题混合导入：query_images 可选填一张提问图片路径；product_count 为2或3。每题优先使用全产品长截图，否则回退全产品录屏；无法统一证据的题目在导入时拒绝。",
          rich_content: "可逐题上传，也可导入 JSON / JSONL / CSV：query、context(可选)、video_path、category/answer_text/task_start_time/task_end_time(均可选)；sessionid 等额外字段会保留到 Excel 导出。普通图片不算挂卡，回答区域蓝色文字按 Superlink 统计。",
        }[mode.value])
    );

    const opPageCount = computed(() => Math.max(1, Math.ceil(opItems.value.length / opPageSize)));
    const pagedOpItems = computed(() => {
      const page = Math.min(opPage.value, opPageCount.value);
      const start = (page - 1) * opPageSize;
      return opItems.value.slice(start, start + opPageSize).map((item, offset) => ({
        item,
        index: start + offset,
      }));
    });

    const progressResultByIndex = computed(() => new Map(results.value.map((entry) => [entry.index, entry])));
    const timingReceipts = new WeakMap();
    function receiveProgress(rows) {
      return Object.fromEntries(Object.entries(rows || {}).map(([key, row]) => [key, {
        ...row, ...(row.timings ? { timings: { ...row.timings, _received_at: Date.now() } } : {}),
      }]));
    }
    function timingLiveExtra(timings, live) {
      if (!timings || !live || timings.finished || !timings.active_stage) return 0;
      if (!timingReceipts.has(timings)) timingReceipts.set(timings, timings._received_at ?? Date.now());
      return Math.max(0, (clockNow.value - timingReceipts.get(timings)) / 1000);
    }
    const progressPageCount = computed(() => Math.max(1, Math.ceil(items.value.length / pageSize)));
    const pagedProgressRows = computed(() => {
      const page = Math.min(progressPage.value, progressPageCount.value);
      const start = (page - 1) * pageSize;
      return items.value.slice(start, start + pageSize).map((item, offset) => {
        const index = start + offset;
        const current = itemProgress.value[index] || {};
        const result = progressResultByIndex.value.get(index);
        const events = progressEvents.value[index] || [];
        const startedAt = Number(current.started_at || 0);
        const terminal = ["done", "error", "paused"].includes(current.status) || selectedTaskStatus.value === "paused";
        const finishedAt = Number(current.finished_at || (terminal && Date.parse(current.updated_at || "")) || 0);
        const resultElapsed = Number(result?.total_s ?? result?.latency_s);
        const timings = terminal && result?.timings ? result.timings : current.timings || result?.timings;
        const elapsedSeconds = timings && (timings.active_stage || timings.finished)
          ? timings.total_s + timingLiveExtra(timings, !terminal)
          : startedAt > 0
          ? Math.max(0, ((finishedAt || clockNow.value) - startedAt) / 1000)
          : (!current.status || terminal) && Number.isFinite(resultElapsed) ? resultElapsed : null;
        return {
          index,
          itemId: item.id || `q${index}`,
          query: item.query || item.question || "",
          percent: current.percent ?? 0,
          status: current.status || (result ? (result.error ? "error" : "done") : "pending"),
          message: current.message || "排队中",
          requestId: current.request_id || "",
          module: current.module || "",
          judge: current.judge || "",
          round: Number(current.round || 0),
          stageRank: current.stage_rank ?? progressStageRank(current),
          elapsedSeconds,
          timings,
          events,
          latestEvents: events.slice(-2),
        };
      });
    });

    function progressStageRank(progressItem) {
      if (progressItem.status === "done") return 4;
      if (progressItem.module === "结果聚合") return 3;
      if (["模型裁判", "被测模型", "单题评测"].includes(progressItem.module)) return 2;
      if (progressItem.module === "垂域分类") return 1;
      return 0;
    }

    function mergeItemProgress(incoming) {
      const index = incoming.item_index;
      if (index == null) return;
      const existing = itemProgress.value[index] || {};
      if (incoming.sequence != null && existing.sequence != null && incoming.sequence <= existing.sequence) return;
      if (!incoming.timing_update) appendProgressEvent(incoming);
      const newAttempt = incoming.request_id && incoming.request_id !== existing.request_id;
      const previous = newAttempt ? {} : existing;
      const previousRank = previous.stage_rank ?? progressStageRank(previous);
      const incomingRank = progressStageRank(incoming);
      const terminal = ["done", "error", "paused"].includes(incoming.status);
      const updatedAt = Date.parse(incoming.updated_at || "");
      itemProgress.value[index] = {
        ...previous,
        ...incoming,
        ...(incoming.timings ? { timings: { ...incoming.timings, _received_at: Date.now() } } : {}),
        // 同一次请求的阶段只前进；补跑使用新的 request_id 重新计时。
        stage_rank: incoming.status === "done"
          ? 4
          : Math.max(previousRank, incomingRank),
        finished_at: terminal
          ? (previous.finished_at || (Number.isFinite(updatedAt) ? updatedAt : Date.now()))
          : undefined,
      };
    }

    function progressEventKey(incoming) {
      return incoming.sequence != null
        ? `seq:${incoming.sequence}`
        : [incoming.request_id, incoming.updated_at, incoming.module, incoming.event,
            incoming.judge, incoming.round, incoming.message].join("|");
    }

    function normalizeProgressEvents(events) {
      const unique = new Map((events || []).map((event) => {
        const key = progressEventKey(event);
        return [key, { ...event, _key: key }];
      }));
      return [...unique.values()].slice(-100);
    }

    function restoreProgressEvents(events) {
      progressEvents.value = Object.fromEntries(Object.entries(events || {}).map(
        ([index, rows]) => [index, normalizeProgressEvents(rows)]
      ));
    }

    function appendProgressEvent(incoming) {
      const index = incoming.item_index;
      if (index == null) return;
      const previous = progressEvents.value[index] || [];
      const eventKey = progressEventKey(incoming);
      if (previous.some((entry) => (entry._key || progressEventKey(entry)) === eventKey)) return;
      progressEvents.value[index] = [...previous, { ...incoming, _key: eventKey }].slice(-100);
    }

    function progressStageClass(row, stageIndex) {
      if (row.status === "done") return "completed";
      if (stageIndex < row.stageRank) return "completed";
      if (stageIndex === row.stageRank) return row.status === "error" ? "error" : "active";
      return "pending";
    }

    function progressDisplay(row) {
      if (selectedTaskStatus.value === "paused" && !["done", "error"].includes(row.status)) return "已暂停，等待继续执行";
      const message = row.message || "排队中";
      const parts = [];
      if (row.judge && !message.includes(row.judge)) parts.push(row.judge);
      const roundLabel = row.round > 0 ? `第${row.round}轮` : "";
      if (roundLabel && !message.includes(roundLabel)) parts.push(roundLabel);
      parts.push(message);
      return parts.join(" · ");
    }

    function progressStageLabel(row) {
      if (selectedTaskStatus.value === "paused" && !["done", "error"].includes(row.status)) return "暂停";
      if (row.status === "error") return "失败";
      if (row.status === "done") return "完成";
      return progressStages[Math.max(0, Math.min(4, row.stageRank))];
    }

    function progressStatusClass(row) {
      if (row.status === "error") return "status-error";
      if (row.status === "done") return "status-done";
      if (row.stageRank === 0) return "status-pending";
      return "status-running";
    }

    function progressMeta(row) {
      const parts = [];
      if (row.judge) parts.push(row.judge);
      if (row.round > 0) parts.push(`第${row.round}轮`);
      return parts.join(" · ");
    }

    const progressTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    function formatProgressEventTime(value) {
      const date = new Date(value || "");
      if (Number.isNaN(date.getTime())) return "--:--:--";
      return progressTimeFormatter.format(date);
    }

    function progressEventMeta(event) {
      const parts = [];
      if (event.module) parts.push(event.module);
      if (event.judge) parts.push(event.judge);
      if (Number(event.round || 0) > 0) parts.push(`第${event.round}轮`);
      return parts.join(" · ");
    }

    function progressEventMessage(event) {
      let message = String(event.message || "");
      const prefixes = [
        event.judge,
        Number(event.round || 0) > 0 ? `第${event.round}轮` : "",
        event.module,
      ].filter(Boolean);
      for (const prefix of prefixes) {
        message = message
          .replace(new RegExp(`^${escapeRegExp(prefix)}\\s*[·|｜]\\s*`), "")
          .replace(new RegExp(`^${escapeRegExp(prefix)}\\s*[：:]\\s*`), "");
      }
      return message.trim();
    }

    function escapeRegExp(value) {
      return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function scrollProgressLog(event, index) {
      const details = event.currentTarget;
      expandedProgressLogs.value[index] = details.open;
      if (!details.open) return;
      nextTick(() => {
        const panel = details.querySelector(".progress-log-scroll");
        if (panel) panel.scrollTop = panel.scrollHeight;
      });
    }

    function formatProgressElapsed(seconds, status) {
      if (seconds == null || !Number.isFinite(seconds)) return "—";
      if (status === "done" || status === "error") {
        if (seconds < 60) return `${seconds.toFixed(1)}s`;
      }
      const whole = Math.max(0, Math.floor(seconds));
      if (whole < 60) return `${whole}s`;
      return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
    }

    function timingStageLabel(stage) {
      return ({ media: "图片 / 视频处理", media_queue: "等待媒体处理", request_wait: "等待调用额度",
        model: "模型与网络传输", retry_wait: "重试等待", other: "其他处理" })[stage] || "";
    }

    function timingEntries(timings, live = false) {
      if (!timings) return [];
      const extra = timingLiveExtra(timings, live);
      return ["media_queue", "media", "request_wait", "model", "retry_wait", "other"].map(stage => ({
        key: stage, label: timingStageLabel(stage),
        seconds: Number(timings[`${stage}_s`] || 0) + (stage === timings.active_stage ? extra : 0),
      }));
    }

    function shortRequestId(requestId) {
      if (!requestId) return "等待生成";
      return requestId.length > 12 ? `…${requestId.slice(-11)}` : requestId;
    }

    async function copyRequestId(requestId) {
      if (!requestId) return;
      try {
        await navigator.clipboard.writeText(requestId);
      } catch (_) {}
    }

    const skillTabs = computed(() => {
      const map = new Map();
      results.value.forEach((r) => {
        if (r.error) {
          const failed = map.get("__error__") || { key: "__error__", label: "评估失败", count: 0 };
          failed.count += 1;
          map.set("__error__", failed);
          return;
        }
        if (!r.category) return;
        const key = r.category;
        const displayLabel = r.category_display || key;
        const current = map.get(key) || { key, label: displayLabel, count: 0 };
        current.count += 1;
        map.set(key, current);
      });
      return Array.from(map.values()).sort((a, b) => {
        if (a.key === "__error__") return 1;
        if (b.key === "__error__") return -1;
        return b.count - a.count;
      });
    });

    const skillResults = computed(() => {
      if (mode.value === "compare" || !activeSkill.value) return results.value;
      if (activeSkill.value === "__error__") return results.value.filter((r) => r.error);
      return results.value.filter((r) => !r.error && r.category === activeSkill.value);
    });

    const resultCols = computed(() => {
      const contextCols = results.value.some((r) => r.context != null && r.context !== "")
        ? [{ key: "context", label: "背景" }]
        : [];
      if (mode.value === "compare")
        return [
          { key: "item_id", label: "题号" },
          { key: "query", label: "题目" },
          ...contextCols,
          { key: "product_count", label: "产品数" },
          { key: "input_status_summary", label: "输入状态" },
          { key: "response_gate_summary", label: "响应体验Gate" },
          { key: "safety_gate_summary", label: "安全稳定Gate" },
          { key: "understanding_summary", label: "准确理解需求" },
          { key: "accuracy_summary", label: "内容准确（暂不汇总）" },
          { key: "service_closure_summary", label: "服务闭环" },
          { key: "scenario_fulfillment_summary", label: "场景化满足" },
          { key: "intuitive_efficiency_summary", label: "直观高效" },
          { key: "evidence_quality_summary", label: "有理有据" },
          { key: "guided_recommendation_summary", label: "引导推荐" },
          { key: "has_conflict", label: "内容冲突" },
          { key: "needs_human_review", label: "需人工复核" },
          { key: "rationale", label: "理由" },
          { key: "latency_s", label: "总耗时" },
        ];
      // rich_content（默认）
      return [
        { key: "item_id", label: "题号" },
        { key: "query", label: "Query" },
        ...contextCols,
        { key: "category_display", label: "垂域" },
        { key: "answer_text", label: "answer_text" },
        { key: "card_presence", label: "是否有卡片" },
        { key: "card_count", label: "卡片数量" },
        { key: "card_types", label: "卡片种类" },
        { key: "card_contents", label: "卡片内容" },
        { key: "superlink_presence", label: "Superlink是否存在" },
        { key: "superlink_count", label: "Superlink数量" },
        { key: "superlink_texts", label: "Superlink文字" },
        { key: "card_suitability", label: "卡片是否合适" },
        { key: "card_suitability_reason", label: "卡片不合适原因" },
        { key: "superlink_suitability", label: "Superlink是否合适" },
        { key: "superlink_suitability_reason", label: "Superlink不合适原因" },
        { key: "answer_coverage", label: "回答覆盖" },
        { key: "needs_review", label: "需人工复核" },
        { key: "review_reason", label: "复核原因" },
        { key: "problem_solved", label: "是否解决用户问题" },
        { key: "problem_solved_reason", label: "评价原因" },
        { key: "answer_issues", label: "回答内容问题" },
        { key: "rationale", label: "识别结论" },
        { key: "latency_s", label: "总耗时" },
      ];
    });

    function columnWidth(c) {
      if (c.key === "latency_s") return 120;
      const compact = [
        "latency_s", "card_presence", "card_count", "superlink_presence",
        "superlink_count", "answer_coverage", "needs_review", "problem_solved",
      ].includes(c.key);
      const textColumn = ["query", "context", "answer_text", "rationale", "answer_issues", "problem_solved_reason"].includes(c.key);
      let minWidth = compact ? 80 : textColumn ? 150 : 96;
      let maxWidth = compact ? 120 : c.key === "rationale" ? 380 : textColumn ? 320 : 200;
      if (c.key === "item_id") {
        minWidth = 110;
        maxWidth = 160;
      }
      const visualLength = (value) => Array.from(String(value ?? "")).reduce(
        (sum, char) => sum + (char.charCodeAt(0) > 255 ? 2 : 1),
        0,
      );
      const sampleLengths = skillResults.value
        .slice(0, 200)
        .map((result) => visualLength(cell(result, c)))
        .sort((a, b) => a - b);
      const representativeIndex = Math.max(0, Math.ceil(sampleLengths.length * 0.8) - 1);
      const representativeLength = sampleLengths[representativeIndex] || 1;
      const desired = (Math.max(visualLength(c.label), representativeLength) * 7) + 28;
      return Math.max(minWidth, Math.min(maxWidth, desired));
    }

    const resultTableWidth = computed(
      () => 48 + resultCols.value.reduce((sum, c) => sum + columnWidth(c), 0) + (isVideoMode.value ? 300 : 0)
    );

    const filteredResults = computed(() => {
      const q = resultQuery.value.trim().toLowerCase();
      return skillResults.value.filter((r) => {
        if (failedOnly.value && !r.error) return false;
        if (turnFilter.value && String(r.turn_index) !== turnFilter.value) return false;
        if (diagnosticFilter.value==='warning' && !r.image_warning_count) return false;
        if (diagnosticFilter.value==='blocked' && r.input_diagnostic_status!=='blocked') return false;
        const modality = r.input_modality || ((items.value[r.index]?.query_images || []).length ? "text_image" : "text");
        if (modalityFilter.value && modality !== modalityFilter.value) return false;
        if (q && !`${r.session_id || ""} ${r.turn_index || ""} ${r.item_id || ""} ${r.query || ""} ${r.context || ""} ${r.answer_text || ""} ${r.answer1 || ""} ${r.answer2 || ""} ${r.answer3 || ""} ${(r.card_contents || []).join(" ")} ${(r.superlink_texts || []).join(" ")} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
        return true;
      });
    });

    const pageCount = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / resultPageSize.value)));
    const pagedResults = computed(() => {
      const safePage = Math.min(resultPage.value, pageCount.value);
      const start = (safePage - 1) * resultPageSize.value;
      return filteredResults.value.slice(start, start + resultPageSize.value);
    });

    function selectSkill(key) {
      activeSkill.value = key;
      resultPage.value = 1;
      progressPage.value = 1;
    }
    function resetResultPage() {
      resultPage.value = 1;
    }

    function paginationPages(current, total) {
      if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
      const pages = new Set([1, total]);
      for (let page = Math.max(2, current - 1); page <= Math.min(total - 1, current + 1); page += 1) {
        pages.add(page);
      }
      const sorted = [...pages].sort((a, b) => a - b);
      const result = [];
      sorted.forEach((page, index) => {
        if (index > 0 && page - sorted[index - 1] > 1) result.push(`ellipsis-${page}`);
        result.push(page);
      });
      return result;
    }

    function setTablePage(kind, requestedPage) {
      const configs = {
        result: [resultPage, pageCount, resultJumpPage],
        operation: [opPage, opPageCount, opJumpPage],
        progress: [progressPage, progressPageCount, progressJumpPage],
      };
      const config = configs[kind];
      if (!config || requestedPage === "" || requestedPage == null) return;
      const [pageRef, countRef, jumpRef] = config;
      const page = Math.trunc(Number(requestedPage));
      if (!Number.isFinite(page)) return;
      pageRef.value = Math.min(countRef.value, Math.max(1, page));
      jumpRef.value = "";
    }

    function changePage(delta) {
      setTablePage("result", resultPage.value + delta);
    }
    function changeOpPage(delta) {
      setTablePage("operation", opPage.value + delta);
    }
    function changeProgressPage(delta) {
      setTablePage("progress", progressPage.value + delta);
    }
    function jumpTablePage(kind) {
      const jumpValues = {
        result: resultJumpPage.value,
        operation: opJumpPage.value,
        progress: progressJumpPage.value,
      };
      setTablePage(kind, jumpValues[kind]);
    }

    function changeResultPageSize() {
      if (![10, 20, 50].includes(resultPageSize.value)) resultPageSize.value = 10;
      resultPage.value = 1;
      resultJumpPage.value = "";
    }

    function defaultJudgeSelection() {
      const judge = judges.value.find(candidate => candidate.name === "judge_2") || judges.value[0];
      return judge ? [judge.name] : [];
    }

    function defaultEvaluationProfile() {
      return compareProfiles.value.find((profile) => profile.status === "stable")?.id
        || compareProfiles.value[0]?.id
        || "";
    }

    function evaluationProfileLabel(id) {
      if (!id) return "—";
      return evaluationProfiles.value.find((profile) => profile.id === id)?.display || id;
    }

    function switchMode(k) {
      datasetImportVersion++;
      importReport.value = null;
      opPreparing.value=false;
      datasetSourceTaskId.value = "";
      datasetBaseline = "";
      cancelHistoryLoad();
      mode.value = k;
      selectedJudges.value = defaultJudgeSelection();
      if (k === "compare") selectedEvaluationProfile.value = defaultEvaluationProfile();
      items.value = [];
      progressPage.value = 1;
      errors.value = [];
      datasetName.value = "";
      opItems.value = [newOpItem()];
      opPage.value = 1;
      opJumpPage.value = "";
    }

    // —— 视频评测：逐题卡片（query + 可选 context + 视频上传 + 可选 answer_text）——
    function newOpItem() {
      return { _uiKey: ++opItemSequence, id: "", query: "", queryImages: [], queryImageMeta: [], queryUploading: false, queryUploadError: "", evidenceMode: "video_frames", screenshot1Path: "", screenshot2Path: "", screenshot3Path: "", context: "", category: "", productCount: 2, videoName: "", videoPath: "", video1Path: "", video2Path: "", video3Path: "", frames: [], frameCount: 0, duration: 0, answer: "", answer1: "", answer2: "", answer3: "", context1: "", context2: "", context3: "", taskStartTime: null, taskEndTime: null, sourceLine: null, sourceData: null, sessionGroup: null, turnIndex: null, uploading: false, uploadError: "" };
    }
    function draftFingerprint() {
      const keys = ['id','query','queryImages','context','category','productCount','evidenceMode',
        'taskStartTime','taskEndTime','video1Path','video2Path','video3Path','screenshot1Path',
        'screenshot2Path','screenshot3Path','answer1','answer2','answer3','context1','context2','context3'];
      return JSON.stringify(opItems.value.map(item => Object.fromEntries(keys.map(key => [key,item[key]]))));
    }
    function confirmDatasetReplacement() {
      const changed = datasetBaseline ? draftFingerprint() !== datasetBaseline : opItems.value.length > 1 || opItems.value.some(item =>
        item.queryImages.length || item.productCount !== 2 || item.evidenceMode !== 'video_frames' ||
        ['id','query','context','category','video1Path','video2Path','video3Path','screenshot1Path',
          'screenshot2Path','screenshot3Path','answer1','answer2','answer3','context1','context2','context3'].some(key=>item[key]));
      return !changed || confirm('当前数据有未提交的修改。使用新数据会替换输入区，是否继续？');
    }
    function comparisonDraftRows(rows) {
      return rows.map(raw => {
        const item = {...(raw.source_data || {}), ...raw};
        const conversation = raw.session_id && Number.isInteger(raw.turn_index) && raw.turn_index >= 1;
        return {...newOpItem(), id:item.id || '', query:item.query || item.question || '',
          context:item.context || '', category:item.category || '',
          queryImages:[...(item.query_images || [])], queryImageMeta:item.query_image_meta || [],
          productCount:item.product_count || (item.video3 || item.screenshot3 ? 3 : 2),
          evidenceMode:item.evidence_mode || (item.screenshot1 ? 'long_screenshot' : 'video_frames'),
          ...Object.fromEntries([1,2,3].flatMap(n => [
            [`video${n}Path`,item[`video${n}`] || ''], [`screenshot${n}Path`,item[`screenshot${n}`] || ''],
            [`answer${n}`,item[`answer${n}`] || ''], [`context${n}`,item[`context${n}`] || ''],
            [`screenshotMeta${n}`,item[`screenshot_meta${n}`] || {}],
            [`videoSource${n}`,item[`video_source${n}`] || {}],
          ])), taskStartTime:item.task_start_time ?? null, taskEndTime:item.task_end_time ?? null,
          sourceLine:item.source_line ?? null, sourceData:raw.source_data || null,
          sessionId:conversation ? raw.session_id : '', screenshotScope:conversation ? raw.screenshot_scope || '' : '',
          sessionGroup:conversation ? raw.session_group ?? null : null, turnIndex:conversation ? raw.turn_index : null};
      });
    }
    function detachResultView() {
      closeActiveStream();taskId.value='';results.value=[];summary.value=null;
      taskJudgeRuntime.value = null;taskJudgeSummary.value = {};
      selectedTaskTiming.value = null;
      itemProgress.value={};progressEvents.value={};running.value=false;selectedTaskStatus.value='';
      repairStatus.value='idle';activeRetry.value=null;selectedRetryIndexes.value=[];
      runError.value='';queueNotice.value='';progress.value=0;total.value=0;
    }
    function useComparisonDataset(data) {
      if (mode.value !== 'compare' || !confirmDatasetReplacement()) return;
      datasetImportVersion++;opPreparing.value=false;cancelHistoryLoad();detachResultView();
      items.value=JSON.parse(JSON.stringify(data.items));
      opItems.value=comparisonDraftRows(items.value);
      datasetName.value=data.dataset_name || '历史测评数据';datasetSourceTaskId.value=data.task_id;
      opPage.value=1;errors.value=[];importReport.value=null;datasetBaseline=draftFingerprint();datasetRevision.value++;
    }
    async function onQueryImage(event, index) {
      const item = opItems.value[index];
      const file = event.target.files?.[0];
      if (!file || !item) return;
      item.queryUploading = true;
      item.queryUploadError = "";
      try {
        const form = new FormData();
        form.append("file", file);
        const response = await fetch("/api/upload/query-image", { method: "POST", body: form });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || "上传失败");
        item.queryImages = [data.original_path];
        item.queryImageMeta = [data];
      } catch (error) { item.queryUploadError = error.message; }
      finally { item.queryUploading = false; event.target.value = ""; }
    }
    function setQueryImagePath(item, path) {
      item.queryImages = path.trim() ? [path.trim()] : [];
      item.queryImageMeta = [];
      item.queryUploadError = "";
    }
    function addOpItem() {
      opItems.value.push(newOpItem());
      opPage.value = Math.ceil(opItems.value.length / opPageSize);
      opJumpPage.value = "";
    }
    function removeOpItem(i) {
      if (opItems.value.length <= 1) return;
      opItems.value.splice(i, 1);
      opPage.value = Math.min(opPage.value, Math.max(1, Math.ceil(opItems.value.length / opPageSize)));
      opJumpPage.value = "";
    }
    async function uploadVideo(i, file) {
      const it = opItems.value[i];
      if (!file) return;
      if (file.size > 20 * 1024 * 1024) { it.uploadError = "视频超过 20MB 限制"; return; }
      it.uploading = true; it.uploadError = "";
      const fd = new FormData(); fd.append("file", file);
      try {
        const r = await fetch(`/api/upload/video?mode=${encodeURIComponent(mode.value)}`, { method: "POST", body: fd });
        if (!r.ok) { it.uploadError = "上传失败 " + r.status; return; }
        const d = await r.json();
        it.videoName = file.name;
        it.videoPath = d.video_path;
        it.frames = d.frames || [];
        it.frameCount = d.frame_count || 0;
        it.duration = d.duration || 0;
      } catch (e) {
        it.uploadError = "上传出错：" + e;
      } finally {
        it.uploading = false;
      }
    }
    function onOpVideo(e, i) { uploadVideo(i, e.target.files[0]); e.target.value = ""; }
    function onOpDrop(e, i) {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadVideo(i, f);
    }

    async function onOpManifestFile(e) {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!file) return;
      const importVersion = ++datasetImportVersion;
      const importMode = mode.value;
      opPreparing.value = true;
      errors.value = [];
      importReport.value = null;
      try {
        const content = await file.text();
        const isCsv = /\.csv$/i.test(file.name || "");
        console.log("[onOpManifestFile] mode:", mode.value, "csv:", isCsv, "file size:", content.length);
        const parseBody = isCsv
          ? { mode: mode.value, csv: content }
          : { mode: mode.value, jsonl: content };
        const parseResponse = await fetch("/api/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(parseBody),
        });
        const parsed = await parseResponse.json().catch(() => ({}));
        if (importVersion !== datasetImportVersion || importMode !== mode.value) return;
        console.log("[onOpManifestFile] response ok:", parseResponse.ok, "items:", (parsed.items || []).length, "errors:", (parsed.errors || []).length);
        if (!parseResponse.ok) throw new Error(parsed.detail || (isCsv ? "CSV 解析请求失败" : "JSON / JSONL 解析请求失败"));
        const importErrors = [...(parsed.errors || [])];
        const report = {filename: file.name || '', accepted: (parsed.items || []).length,
          rejected: parsed.rejected_count ?? importErrors.length,
          screenshots: parsed.evidence_counts?.long_screenshot ?? (parsed.items || []).filter(it => it.evidence_mode === 'long_screenshot').length,
          videos: parsed.evidence_counts?.video_frames ?? (parsed.items || []).filter(it => it.evidence_mode === 'video_frames').length};
        if (!(parsed.items || []).length) {
          importReport.value = report;
          errors.value = importErrors.length ? importErrors : ["文件中没有可导入的数据"];
          console.warn("[onOpManifestFile] no items parsed");
          return;
        }

        if (mode.value === 'compare' && !confirmDatasetReplacement()) return;
        importReport.value = report;
        if (mode.value === 'compare') { cancelHistoryLoad();detachResultView(); }
        datasetName.value = file.name || '';
        datasetSourceTaskId.value = '';
        errors.value = importErrors;
        const imported = parsed.items || [];
        if (imported.length) {
          items.value = imported;
          opItems.value = imported.map((item) => ({
            ...newOpItem(),
            id: item.id || "",
            query: item.query || "",
            queryImages: [...(item.query_images || [])],
            queryImageMeta: item.query_image_meta || [],
            context: item.context || "",
            category: item.category === "default" ? "" : (item.category || ""),
            videoName: String(item.video_path || "").split(/[\\/]/).pop(),
            videoPath: item.video_path || item.video1 || "",
            productCount: item.product_count || (item.video3 || item.screenshot3 ? 3 : 2),
            evidenceMode: item.evidence_mode || (item.screenshot1 ? "long_screenshot" : "video_frames"),
            screenshot1Path: item.screenshot1 || "",
            screenshot2Path: item.screenshot2 || "",
            screenshot3Path: item.screenshot3 || "",
            answer: mode.value === "compare" ? (item.answer1 || "") : (item.answer_text || ""),
            answer1: item.answer1 || "",
            answer2: item.answer2 || "",
            answer3: item.answer3 || "",
            context1: item.context1 || "",
            context2: item.context2 || "",
            context3: item.context3 || "",
            video1Path: item.video1 || "",
            video2Path: item.video2 || "",
            video3Path: item.video3 || "",
            taskStartTime: item.task_start_time ?? null,
            taskEndTime: item.task_end_time ?? null,
            sourceLine: item.source_line ?? null,
            sourceData: item.source_data || null,
            sessionGroup: item.session_group ?? null,
            turnIndex: item.turn_index ?? null,
          }));
          opPage.value = 1;
          if (mode.value === 'compare') opItems.value = comparisonDraftRows(imported);
          datasetBaseline = draftFingerprint();datasetRevision.value++;
          console.log("[onOpManifestFile] opItems mapped:", opItems.value.length, "first videoPath:", opItems.value[0]?.videoPath, "first query:", opItems.value[0]?.query);
        }
      } catch (error) {
        console.error("[onOpManifestFile] error:", error);
        if (importVersion !== datasetImportVersion) return;
        errors.value = ["批量导入失败：" + (error?.message || String(error))];
      } finally {
        if (importVersion === datasetImportVersion) opPreparing.value = false;
      }
    }

    function opItemReady(it) {
      if (!it.query.trim()) return false;
      if (mode.value !== "compare") return Boolean((it.frames || []).length || it.videoPath);
      return Boolean(selectEvidenceMode(it));
    }

    const canSubmit = computed(() =>
      !opPreparing.value && !opItems.value.some(it => it.queryUploading)
      && (mode.value === 'compare' ? opItems.value.length > 0 && opItems.value.every(opItemReady) : opItems.value.some(opItemReady))
    );

    async function submit() {
      if (submitting.value) return;
      cancelHistoryLoad();
      const submitViewVersion = historyLoadVersion;
      runError.value = "";
      if (judgeModelProfiles.value.length && !selectedModelProfile.value) {
        runError.value = "请选择可用的裁判模型。";
        return;
      }
      if (!Number.isInteger(concurrency.value) || concurrency.value < 1 || concurrency.value > 128) {
        runError.value = "评估并发数必须为 1–128 的整数";
        return;
      }
      const valid = mode.value === 'compare' ? opItems.value : opItems.value.filter(opItemReady);
      if (mode.value === 'compare' && valid.some(item => !opItemReady(item))) {
        runError.value = '存在未填写问题或缺少产品证据的 Case，请展开检查；不会跳过这些条目提交。';
        return;
      }
      if (!valid.length) {
        alert("请为每题填写 query，并导入完整的长截图或录屏路径后再评估。");
        return;
      }
      const submittedItems = valid.map((it, idx) => {
        const prefix = mode.value === "compare" ? "cmp" : "rich";
        const item = {
          id: it.id || `${prefix}${idx + 1}`,
          query: it.query.trim(),
          context: (it.context || "").trim(),
        };
        if (mode.value === "compare") {
          item.query_images = [...(it.queryImages || [])];
          const productCount = Number(it.productCount) === 3 ? 3 : 2;
          item.product_count = productCount;
          item.evidence_mode = selectEvidenceMode(it);
          const evidencePrefix = item.evidence_mode === 'long_screenshot' ? 'screenshot' : 'video';
          for (let n = 1; n <= productCount; n++) {
            item[`${evidencePrefix}${n}`] = it[`${evidencePrefix}${n}Path`].trim();
          }
          item.context1 = (it.context1 || "").trim();
          item.context2 = (it.context2 || "").trim();
          item.answer1 = (it.answer1 || it.answer || "").trim();
          item.answer2 = (it.answer2 || "").trim();
          if (productCount === 3) {
            item.context3 = (it.context3 || "").trim();
            item.answer3 = (it.answer3 || "").trim();
          }
          item.category = (it.category || "").trim() || "default";
        } else {
          item.video_path = it.videoPath;
          item.category = (it.category || "").trim() || "default";
          item.answer_text = (it.answer || "").trim();
        }
        if ((it.frames || []).length) {
          item.media = [it.videoPath];
          item.frames = it.frames;
        }
        if (Number.isFinite(it.taskStartTime)) item.task_start_time = it.taskStartTime;
        if (Number.isFinite(it.taskEndTime)) item.task_end_time = it.taskEndTime;
        if (Number.isFinite(it.sourceLine)) item.source_line = it.sourceLine;
        if (it.sourceData) item.source_data = it.sourceData;
        if (it.sessionGroup != null) item.session_group = it.sessionGroup;
        if (it.turnIndex != null) item.turn_index = it.turnIndex;
        if (it.sessionId) {item.session_id = it.sessionId; item.screenshot_scope = it.screenshotScope;}
        return item;
      });
      const body = {
        mode: mode.value,
        items: submittedItems,
        dataset_name: datasetName.value || "手动录入",
        evaluation_profile: mode.value === "compare" ? selectedEvaluationProfile.value : null,
        options: {
          ...(mode.value === 'compare' && datasetSourceTaskId.value ? {dataset_source_task_id:datasetSourceTaskId.value} : {}),
          judges: selectedJudges.value,
          ...(selectedModelProfile.value ? {
            judge_model_profile: selectedModelProfile.value.id,
            ...(selectedModelProfile.value.supports_thinking === true ? { enable_thinking: enableThinking.value } : {}),
          } : {}),
          concurrency: concurrency.value,
          eval_timeout_s: evalTimeout.value,
        },
      };
      let r;
      submitting.value = true;
      try {
        conversationPreflight.value = null;
        if (submittedItems.some(item => item.session_id)) {
          const checked = await fetch('/api/compare/preflight', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
          const report = await checked.json();
          if (!checked.ok) throw Error(typeof report.detail === 'string' ? report.detail : '多轮预检查失败');
          conversationPreflight.value = report;
          if (report.blocked_turn_count) throw Error(`多轮预检查：${report.blocked_turn_count} 轮被阻断，请查看图片诊断。`);
        }
        r = await fetch("/api/eval", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (error) {
        submitting.value = false;
        runError.value = "无法启动评估：" + (error?.message || "网络错误");
        return;
      }
      const d = await r.json().catch(() => ({}));
      submitting.value = false;
      if (!r.ok || !d.task_id) {
        const detail = typeof d.detail === "string" ? d.detail : "服务端拒绝了评估请求";
        runError.value = "无法启动评估：" + detail;
        return;
      }
      if (submitViewVersion !== historyLoadVersion) {
        loadQueue();
        loadHistory();
        return;
      }
      closeActiveStream();
      items.value = submittedItems;
      datasetBaseline = draftFingerprint();
      errors.value = [];
      results.value = [];
      summary.value = null;
      progressEvents.value = {};
      expandedProgressLogs.value = {};
      activeSkill.value = "";
      resultQuery.value = "";
      failedOnly.value = false;
      resultPage.value = 1;
      progress.value = 0;
      total.value = submittedItems.length;
      itemProgress.value = Object.fromEntries(
        submittedItems.map((item, index) => [
          index,
          {
            item_index: index,
            item_id: item.id || `q${index}`,
            status: "pending",
            percent: 0,
            message: "等待前序任务完成",
            stage_rank: 0,
          },
        ])
      );
      running.value = true;
      taskId.value = d.task_id;
      taskJudgeRuntime.value = d.judge_runtime || null;
      taskJudgeSummary.value = {judge_model: d.judge_model, enable_thinking_label: d.enable_thinking_label};
      executionControl.value = {};
      selectedActiveRuns.value = 1;
      resumeConcurrency.value = concurrency.value;
      selectedTaskTiming.value = receiveTaskTiming(d.task_timing);
      repairStatus.value = "idle";
      activeRetry.value = null;
      selectedRetryIndexes.value = [];
      selectedTaskStatus.value = d.status || "queued";
      queueNotice.value = d.queue_position > 1
        ? `已加入队列，当前排在第 ${d.queue_position} 位。`
        : "任务已提交，等待调度器启动。";
      connectSSE(taskId.value);
      loadQueue();
      loadHistory();
    }

    async function retryFailedCases(indexes = null) {
      if (!taskId.value || retrySubmitting.value || loadingTaskId.value) return;
      const retryTaskId = taskId.value;
      const viewVersion = historyLoadVersion;
      const selected = indexes == null ? null : [...new Set(indexes.map(Number))];
      if (selected && !selected.length) return;
      retrySubmitting.value = true;
      runError.value = "";
      const idempotencyKey = globalThis.crypto?.randomUUID?.()
        || `retry-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let response;
      try {
        response = await fetch(`/api/eval/${encodeURIComponent(retryTaskId)}/retries`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            indexes: selected,
            include_unfinished: true,
            idempotency_key: idempotencyKey,
            options: {},
          }),
        });
      } catch (error) {
        retrySubmitting.value = false;
        if (viewVersion !== historyLoadVersion || taskId.value !== retryTaskId) return;
        runError.value = "无法提交失败补跑：" + (error?.message || "网络错误");
        return;
      }
      const data = await response.json().catch(() => ({}));
      retrySubmitting.value = false;
      if (viewVersion !== historyLoadVersion || taskId.value !== retryTaskId) return;
      if (!response.ok) {
        const detail = typeof data.detail === "string"
          ? data.detail
          : (data.detail?.message || "服务端拒绝了补跑请求");
        runError.value = "无法提交失败补跑：" + detail;
        return;
      }
      selectedRetryIndexes.value = [];
      activeRetry.value = data;
      repairStatus.value = data.status || "queued";
      queueNotice.value = `已提交 ${data.selected} 条失败补跑，当前排在第 ${data.queue_position} 位。`;
      connectSSE(taskId.value);
      await loadQueue();
      await loadHistory();
    }

    async function reconcileTaskAfterError(message, errorTaskId, viewVersion) {
      let snapshot = null;
      try {
        const response = await fetch(`/api/history/${encodeURIComponent(errorTaskId)}`);
        if (response.ok) snapshot = await response.json();
      } catch (_) {}
      if (viewVersion !== historyLoadVersion || taskId.value !== errorTaskId) return false;
      updateTaskTiming(snapshot?.task_timing);
      executionControl.value = snapshot?.execution_control || {};
      selectedActiveRuns.value = snapshot?.active_runs || 0;
      if (snapshot?.status) selectedTaskStatus.value = snapshot.status;
      const snapshotResults = snapshot?.results || results.value;
      const resultByIndex = new Map(snapshotResults.map((entry) => [entry.index, entry]));
      const snapshotProgress = snapshot?.item_progress || {};
      if (snapshot?.progress_events) restoreProgressEvents(snapshot.progress_events);
      const reconciled = {};
      items.value.forEach((item, index) => {
        const previous = itemProgress.value[index] || {};
        const remote = snapshotProgress[index] || snapshotProgress[String(index)] || {};
        const result = resultByIndex.get(index);
        let status = remote.status || previous.status || "error";
        let rowMessage = remote.message || previous.message || "";
        if (result) {
          status = result.error ? "error" : "done";
          rowMessage = result.error ? "评测失败" : "评测完成";
        } else if (status !== "done" && status !== "error") {
          status = "error";
          rowMessage = `任务中断：${message}`;
        }
        const updatedAt = Date.parse(remote.updated_at || "");
        reconciled[index] = {
          ...previous,
          ...remote,
          status,
          message: rowMessage,
          percent: status === "done" || status === "error" ? 100 : (remote.percent ?? previous.percent ?? 0),
          stage_rank: status === "done" ? 4 : (remote.stage_rank ?? previous.stage_rank ?? 0),
          finished_at: previous.finished_at
            || (Number.isFinite(updatedAt) ? updatedAt : Date.now()),
        };
      });
      results.value = snapshotResults;
      refreshEvidence(snapshotResults);
      progress.value = snapshotResults.length;
      itemProgress.value = receiveProgress(reconciled);
      if (snapshot?.summary) summary.value = snapshot.summary;
      return true;
    }

    function closeActiveStream() {
      if (activeEventSource) activeEventSource.close();
      activeEventSource = null;
    }

    async function controlTask(action) {
      const id = taskId.value, version = historyLoadVersion;
      if (!id || controlSubmitting.value || loadingTaskId.value) return;
      if (action === "resume" && (!Number.isInteger(resumeConcurrency.value) || resumeConcurrency.value < 1 || resumeConcurrency.value > 128)) {
        runError.value = "恢复并发数必须为 1–128 的整数";
        return;
      }
      controlSubmitting.value = true;
      runError.value = "";
      try {
        const response = await fetch(`/api/eval/${encodeURIComponent(id)}/${action}`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          ...(action === "resume" ? {body: JSON.stringify({concurrency: resumeConcurrency.value, include_failed: resumeFailed.value, idempotency_key: `resume-${Date.now()}-${Math.random().toString(16).slice(2)}`})} : {}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请刷新后重试");
        if (id !== taskId.value || version !== historyLoadVersion || disposed) return;
        await loadHistoryTask(id);
        await loadQueue();
        await loadHistory();
      } catch (error) {
        if (id === taskId.value && version === historyLoadVersion) runError.value = error.message || "操作失败";
      } finally {
        controlSubmitting.value = false;
      }
    }

    function connectSSE(streamTaskId = taskId.value) {
      closeActiveStream();
      const es = new EventSource(`/api/eval/${streamTaskId}/stream?compact=true`);
      activeEventSource = es;
      const isSelected = () => !loadingTaskId.value && taskId.value === streamTaskId && activeEventSource === es;
      es.addEventListener("replay_state", (e) => {
        if (!isSelected()) return;
        const data = JSON.parse(e.data);
        // 一次恢复结果和当前进度，旧结果不能覆盖正在补跑的状态。
        updateTaskTiming(data.task_timing);
        executionControl.value = data.execution_control || {};
        selectedActiveRuns.value = data.active_runs || 0;
        if (data.status) {
          selectedTaskStatus.value = data.status;
          running.value = ["pending", "queued", "running"].includes(data.status);
          if (data.status !== "queued") queueNotice.value = "";
        }
        results.value = data.results || [];
        refreshEvidence(results.value);
        itemProgress.value = receiveProgress(data.item_progress);
        progressEvents.value = {};
        progress.value = data.progress;
        repairStatus.value = data.repair_status || "idle";
        activeRetry.value = data.retry || null;
      });
      es.addEventListener("progress_history", (e) => {
        if (!isSelected()) return;
        const data = JSON.parse(e.data);
        progressEvents.value[data.item_index] = normalizeProgressEvents(data.events);
      });
      es.addEventListener("start", (e) => {
        if (!isSelected()) return;
        updateTaskTiming(JSON.parse(e.data || "{}").task_timing);
        selectedTaskStatus.value = "running";
        queueNotice.value = "";
        running.value = true;
        selectedActiveRuns.value = 1;
        loadQueue();
      });
      es.addEventListener("item_progress", (e) => {
        if (!isSelected()) return;
        const d = JSON.parse(e.data);
        mergeItemProgress(d);
      });
      es.addEventListener('input_diagnostics', e => {
        if (!isSelected()) return;
        const data=JSON.parse(e.data);liveInputDiagnostics.value[data.item_index]=data;
        if(items.value[data.item_index]) Object.assign(items.value[data.item_index],data);
      });
      es.addEventListener("progress_event", (e) => {
        if (!isSelected()) return;
        appendProgressEvent(JSON.parse(e.data));
      });
      es.addEventListener("retry_start", (e) => {
        if (!isSelected()) return;
        activeRetry.value = JSON.parse(e.data);
        repairStatus.value = "running";
        queueNotice.value = "失败补跑正在执行。";
        loadQueue();
      });
      es.addEventListener("result", (e) => {
        if (!isSelected()) return;
        const d = JSON.parse(e.data);
        const result = d.result;
        refreshEvidence([result]);
        const index = result && result.index;
        if (index == null) {
          results.value.push(result);
        } else {
          // 断线重连时服务端会整段回放 results：按 index 替换去重，
          // 防止重连一次就全量翻倍（重复行 + 内存线性增长）
          const pos = results.value.findIndex((x) => x && x.index === index);
          if (pos >= 0) results.value.splice(pos, 1, result);
          else results.value.push(result);
        }
        progress.value = d.progress;
        if (index != null) {
          const previous = itemProgress.value[index] || {};
          itemProgress.value[index] = {
            ...previous,
            status: d.result.error ? "error" : "done",
            percent: 100,
            message: d.result.error ? "评测失败" : "评测完成",
            stage_rank: d.result.error ? (previous.stage_rank ?? 0) : 4,
            finished_at: Date.now(),
          };
        }
      });
      es.addEventListener("retry_result", (e) => {
        if (!isSelected()) return;
        const data = JSON.parse(e.data);
        const index = Number(data.index);
        if (!Number.isInteger(index)) return;
        const previous = itemProgress.value[index] || {};
        const status = data.status === "succeeded" ? "done" : (data.status === "failed" ? "error" : previous.status);
        itemProgress.value[index] = {
          ...previous,
          status,
          percent: 100,
          message: data.status === "succeeded" ? "补跑成功" : (data.status === "failed" ? "补跑仍失败" : "补跑已跳过"),
          finished_at: Date.now(),
        };
      });
      es.addEventListener("done", (e) => {
        if (!isSelected()) return;
        const doneData = JSON.parse(e.data);
        executionControl.value = doneData.execution_control || {};
        selectedActiveRuns.value = 0;
        updateTaskTiming(doneData.task_timing);
        summary.value = doneData.summary;
        if (doneData.retry) {
          activeRetry.value = doneData.retry;
          repairStatus.value = doneData.retry.status || "completed";
          queueNotice.value = `失败补跑完成：成功 ${doneData.retry.succeeded || 0}，仍失败 ${doneData.retry.failed || 0}，跳过 ${doneData.retry.skipped || 0}。`;
        }
        if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
        resultPage.value = 1;
        running.value = false;
        selectedTaskStatus.value = "done";
        if (!doneData.retry) queueNotice.value = "";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        loadQueue();
        loadHistory();
      });
      es.addEventListener("error", async (e) => {
        // 原生 EventSource 网络错误没有 data，让浏览器按协议自动重连并回放状态。
        if (!e.data) return;
        if (!isSelected()) return;
        let message = "未知错误";
        try {
          const d = JSON.parse(e.data);
          updateTaskTiming(d.task_timing);
          message = d.message || message;
        } catch (_) {}
        running.value = false;
        selectedTaskStatus.value = "error";
        selectedActiveRuns.value = 0;
        queueNotice.value = "";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        if (!await reconcileTaskAfterError(message, streamTaskId, historyLoadVersion)) return;
        runError.value = "评估出错：" + message;
        loadQueue();
        loadHistory();
      });
      es.addEventListener("cancelled", (e) => {
        if (!isSelected()) return;
        let message = "排队任务已取消";
        try {
          const data = JSON.parse(e.data);
          updateTaskTiming(data.task_timing);
          message = data.message || message;
        } catch (_) {}
        running.value = false;
        selectedActiveRuns.value = 0;
        executionControl.value = {};
        selectedTaskStatus.value = "cancelled";
        queueNotice.value = message;
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        loadQueue();
        loadHistory();
      });
      es.addEventListener("retry_cancelled", () => {
        if (!isSelected()) return;
        repairStatus.value = "cancelled";
        selectedActiveRuns.value = 0;
        queueNotice.value = "失败补跑已取消";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        loadQueue();
        loadHistory();
      });
      es.addEventListener("pausing", (e) => {
        if (!isSelected()) return;
        const data = JSON.parse(e.data);
        executionControl.value = data.execution_control || {state: "pausing"};
        updateTaskTiming(data.task_timing);
      });
      es.addEventListener("paused", (e) => {
        if (!isSelected()) return;
        const data = JSON.parse(e.data);
        executionControl.value = data.execution_control || {state: "paused"};
        selectedTaskStatus.value = "paused";
        selectedActiveRuns.value = 0;
        running.value = false;
        if (["queued", "running"].includes(repairStatus.value)) repairStatus.value = "paused";
        updateTaskTiming(data.task_timing);
        summary.value = data.summary || summary.value;
        queueNotice.value = "已暂停，已完成记录已保存。";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        loadQueue();
        loadHistory();
      });
    }

    function cell(r, c) {
      const v = r[c.key];
      if (c.key === "category") return r.category_display || (!v || v === "default" ? "通用" : v);
      if (c.key === "latency_s") return r.total_s != null ? r.total_s + "秒" : v != null ? v + "秒（旧记录）" : "";
      if (["input_status_summary", "response_gate_summary", "safety_gate_summary"].includes(c.key)) {
        const field = c.key.replace("_summary", "");
        const labels = { complete: "完整", partial: "不完整", failed: "失败", pass: "通过", fail: "失败", unclear: "不清楚" };
        const count = Number(r.product_count || 2);
        return Array.from({ length: count }, (_, index) => {
          const productNo = index + 1;
          const key = field === "input_status" ? `answer${productNo}_input_status` : `answer${productNo}_${field}`;
          const value = r[key];
          return `P${productNo}:${labels[value] || value || "N/A"}`;
        }).join("；");
      }
      if (c.key.endsWith("_summary") && !["input_status_summary", "response_gate_summary", "safety_gate_summary"].includes(c.key)) {
        const dimension = c.key.slice(0, -"_summary".length);
        if (r[`${dimension}_applicable`] === false) return "N/A";
        const count = Number(r.product_count || 2);
        const scores = Array.from({ length: count }, (_, index) => {
          const score = r[`answer${index + 1}_${dimension}_score`];
          return `P${index + 1}:${score == null ? "N/A" : score}`;
        }).join("；");
        const groups = r[`${dimension}_rank_groups`] || [];
        const ranking = groups.map((group) => group.map((product) => product.replace("product", "P")).join("=")).join(">");
        const verification = r[`${dimension}_verification_status`] || "";
        return [scores, ranking ? `排名:${ranking}` : "", verification === "unverifiable" ? "无法核验" : ""].filter(Boolean).join("；");
      }
      // 垂域视觉对比维度渲染
      if (["relevance", "safety", "content_quality", "need_closure", "personalization"].includes(c.key)) {
        if (v === "answer1") return "产品1更优";
        if (v === "answer2") return "产品2更优";
        if (v === "tie") return "平手";
        if (v == null) return "N/A";
        return v || "";
      }
      if (c.key === "has_conflict") {
        if (v === "yes") return "有冲突";
        if (v === "no") return "无冲突";
        if (v === "unclear") return "不清楚";
        return v || "";
      }
      if (["card_types", "card_contents", "superlink_texts"].includes(c.key)) {
        return Array.isArray(v) ? v.join("；") : (v || "");
      }
      if (c.key === "card_presence" || c.key === "superlink_presence") {
        return ({ present: "是", absent: "否", unclear: "不清楚" }[v] || v) || "";
      }
      if (c.key === "card_suitability" || c.key === "superlink_suitability") {
        if (v === "ok") return "OK";
        if (v === "nok") return "NOK";
        return v || "";
      }
      if (c.key === "problem_solved") {
        return ({ ok: "OK", nok: "NOK", need_review: "需复查" }[v] || v) || "";
      }
      if (c.key === "answer_coverage") {
        return ({ complete: "完整", partial: "部分", unclear: "不确定" }[v] || v) || "";
      }
      if (c.key === "needs_review" || c.key === "needs_human_review") return v ? "T" : "F";
      if (v == null) return "";
      return v;
    }

    function showCellTooltip(event, value) {
      const text = value == null ? "" : String(value);
      if (!text || text.length < 12) return;
      if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
      const rect = event.currentTarget.getBoundingClientRect();
      const width = Math.min(560, Math.max(260, window.innerWidth - 24));
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      const estimatedHeight = Math.min(360, Math.max(80, Math.ceil(text.length / 30) * 22));
      const below = rect.bottom + 8;
      const top = below + estimatedHeight < window.innerHeight
        ? below
        : Math.max(12, rect.top - estimatedHeight - 8);
      cellTooltip.value = {
        visible: true,
        text,
        style: { left: `${left}px`, top: `${top}px`, width: `${width}px` },
      };
    }

    function scheduleHideCellTooltip() {
      tooltipHideTimer = setTimeout(() => {
        cellTooltip.value.visible = false;
      }, 120);
    }

    function keepCellTooltip() {
      if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
    }

    function hideCellTooltip() {
      cellTooltip.value.visible = false;
    }

    function formatTime(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      if (Number.isNaN(d.getTime())) return String(ts);
      return d.toLocaleString();
    }

    async function loadHistory(requestedPage = historyRequestedPage) {
      requestedPage = Math.max(1, Math.trunc(Number(requestedPage)) || 1);
      historyRequestedPage = requestedPage;
      const version = ++historyListVersion;
      loadingHistory.value = true;
      historyError.value = "";
      try {
        const r = await fetch(`/api/history?page=${requestedPage}`);
        if (!r.ok) throw new Error("请稍后重试");
        const d = await r.json();
        if (version !== historyListVersion || disposed) return;
        historyItems.value = (d.items || []).map(item => ({ ...item, task_timing: receiveTaskTiming(item.task_timing) }));
        historyTotal.value = d.total ?? historyItems.value.length;
        historyPage.value = d.page || requestedPage;
        historyRequestedPage = historyPage.value;
        const selected = historyItems.value.find(item => item.task_id === taskId.value);
        if (selected) {
          updateTaskTiming(selected.task_timing);
          if (selected.judge_runtime) taskJudgeRuntime.value = selected.judge_runtime;
          taskJudgeSummary.value = {judge_model: selected.judge_model, enable_thinking_label: selected.enable_thinking_label};
        }
        historyNoteDrafts.value = Object.fromEntries([
          // Keep unfinished note edits when their rows are on another page.
          ...Object.entries(historyNoteDrafts.value).filter(([id]) => historyNoteEditing.value[id]),
          ...historyItems.value.map((item) => [item.task_id,
            historyNoteEditing.value[item.task_id] ? historyNoteDrafts.value[item.task_id] : item.note || ""]),
        ]);
        historyNoteEditing.value = Object.fromEntries(Object.entries(historyNoteEditing.value)
          .filter(([, editing]) => editing));
      } catch (error) {
        if (version !== historyListVersion || disposed) return;
        historyRequestedPage = historyPage.value;
        historyError.value = "历史记录加载失败：" + (error?.message || "网络错误");
      } finally {
        if (version === historyListVersion) loadingHistory.value = false;
      }
    }

    async function loadQueue() {
      try {
        const response = await fetch("/api/queue");
        if (!response.ok) return;
        const data = await response.json();
        const previousRunning = queueState.value.running;
        const previousQueued = queueState.value.queued || [];
        queueState.value = {
          running: data.running || null,
          queued: data.queued || [],
        };
        const selected = queueEntries.value.find(item => item.task_id === taskId.value);
        if (selected) {
          updateTaskTiming(selected.task_timing);
          if (selected.judge_runtime) taskJudgeRuntime.value = selected.judge_runtime;
          if (selected.kind !== "retry" && selectedTaskStatus.value !== "paused") {
            selectedTaskStatus.value = selected.status;
            running.value = ["pending", "queued", "running"].includes(selected.status);
            if (selected.status !== "queued") queueNotice.value = "";
          }
        }
        historyItems.value = historyItems.value.map(item => {
          const active = queueEntries.value.find(entry => entry.task_id === item.task_id);
          return active ? { ...item, task_timing: receiveTaskTiming(active.task_timing),
            ...(active.kind === "retry" ? {} : { status: active.status }) } : item;
        });
        const jobKey = entry => entry?.job_id || entry?.task_id;
        const currentKeys = new Set(queueEntries.value.map(jobKey));
        if (jobKey(previousRunning) !== jobKey(queueState.value.running)
            || previousQueued.some(entry => !currentKeys.has(jobKey(entry)))) {
          await loadHistory().catch(() => {});
        }
        await loadRequestPacing();
      } catch (_) {}
    }

    async function loadRequestPacing() {
      if (disposed) return;
      if (!(judges.value.some((judge) => judge.request_pacing) || judgeModelProfiles.value.some(profile => profile.request_pacing))
          || !(queueState.value.running || running.value || repairStatus.value === "running")) {
        pacingStatus.value = null;
        pacingError.value = "";
        return;
      }
      if (pacingLoading || (typeof document !== "undefined" && document.hidden)) return;
      pacingLoading = true;
      try {
        const response = await fetch("/api/request-pacing", { cache: "no-store" });
        if (!response.ok) throw new Error("调度状态暂不可用");
        const data = await response.json();
        if (disposed || !(queueState.value.running || running.value || repairStatus.value === "running")) return;
        const activeTask = queueState.value.running;
        const runtime = activeTask ? activeTask.judge_runtime : taskJudgeRuntime.value;
        const model = runtime?.judges?.[0]?.model;
        const controllers = data.controllers || [];
        const controller = model && controllers.length
          ? controllers.find(entry => entry.model === model || (entry.models || []).includes(model))
          : controllers.length ? (controllers.length === 1 ? controllers[0] : null) : data.controller;
        pacingStatus.value = data.enabled && data.active ? controller || null : null;
        pacingError.value = "";
      } catch (_) {
        if (!disposed && (queueState.value.running || running.value || repairStatus.value === "running")) {
          pacingStatus.value = null;
          pacingError.value = "调度状态暂不可用；任务继续执行。";
        }
      } finally {
        pacingLoading = false;
      }
    }

    async function cancelQueuedTask(entry) {
      if (!entry || entry.status !== "queued") return;
      if (!confirm(`确认取消排队任务“${entry.dataset_name || entry.task_id}”？`)) return;
      const jobId = entry.job_id || entry.task_id;
      const response = await fetch(`/api/queue/${encodeURIComponent(jobId)}`, {
        method: "DELETE",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert("取消失败：" + (data.detail || "任务状态已变化"));
        await loadQueue();
        return;
      }
      if (entry.kind !== "retry" && taskId.value === entry.task_id) {
        running.value = false;
        selectedTaskStatus.value = "cancelled";
        queueNotice.value = "排队任务已取消";
        closeActiveStream();
      }
      await loadQueue();
      await loadHistory();
    }

    async function reprioritizeQueuedTask(entry, action) {
      if (!entry || entry.status !== "queued") return;
      const jobId = entry.job_id || entry.task_id;
      const response = await fetch(`/api/queue/${encodeURIComponent(jobId)}/position`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert("调整优先级失败：" + (data.detail || "任务状态已变化"));
      }
      await loadQueue();
    }

    function editHistoryNote(item) {
      historyNoteDrafts.value[item.task_id] = item.note || "";
      historyNoteEditing.value[item.task_id] = true;
    }

    function cancelHistoryNote(item) {
      historyNoteDrafts.value[item.task_id] = item.note || "";
      historyNoteEditing.value[item.task_id] = false;
    }

    async function saveHistoryNote(item) {
      const note = String(historyNoteDrafts.value[item.task_id] || "").trim();
      const response = await fetch(`/api/history/${item.task_id}/note`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert("备注保存失败：" + (data.detail || "未知错误"));
        return;
      }
      item.note = data.note || "";
      historyNoteDrafts.value[item.task_id] = item.note;
      historyNoteEditing.value[item.task_id] = false;
    }

    async function delHistory(id) {
      if (!confirm("确认删除这条历史记录？删除后不可恢复。")) return;
      const r = await fetch(`/api/history/${id}`, { method: "DELETE" });
      if (!r.ok) {
        let detail = "";
        try {
          detail = (await r.json()).detail || "";
        } catch (e) {}
        alert(detail ? `删除失败：${detail}` : "删除失败");
        return;
      }
      if (taskId.value === id) {
        taskId.value = "";
        taskJudgeRuntime.value = null;
        taskJudgeSummary.value = {};
        selectedTaskTiming.value = null;
        results.value = [];
        summary.value = null;
      }
      delete historyNoteDrafts.value[id];
      delete historyNoteEditing.value[id];
      await loadHistory();
    }

    function cancelHistoryLoad() {
      historyLoadVersion++;
      if (historyLoadController) historyLoadController.abort();
      historyLoadController = null;
      loadingTaskId.value = "";
    }

    async function loadHistoryTask(id) {
      datasetImportVersion++;
      opPreparing.value=false;
      cancelHistoryLoad();
      const version = historyLoadVersion;
      let loaded = false;
      loadingTaskId.value = id;
      historyLoadController = typeof AbortController !== "undefined" ? new AbortController() : null;
      try {
        const r = await fetch(`/api/history/${encodeURIComponent(id)}`, historyLoadController ? {signal: historyLoadController.signal} : {});
        if (version !== historyLoadVersion) return;
        if (!r.ok) {
          alert("历史记录加载失败");
          return;
        }
        const d = await r.json();
        if (version !== historyLoadVersion) return;
        if (!modes.some((m) => m.key === d.mode)) {
          alert("该历史记录使用已下线的评测模式，无法加载。");
          return;
        }
        loaded = true;
        closeActiveStream();
        taskId.value = d.task_id || id;
        taskJudgeRuntime.value = d.judge_runtime || null;
        taskJudgeSummary.value = {judge_model: d.judge_model, enable_thinking_label: d.enable_thinking_label};
        restoreJudgeModel(d.judge_runtime);
        selectedTaskTiming.value = receiveTaskTiming(d.task_timing);
        mode.value = d.mode;
        if (d.mode === "compare") {
          selectedEvaluationProfile.value = d.evaluation_profile || defaultEvaluationProfile();
        }
        datasetName.value = d.dataset_name || "";
        items.value = d.items || [];
        if (d.mode === "compare") {
          opItems.value = items.value.map(item => ({
            ...newOpItem(), id: item.id || "", query: item.query || item.question || "",
            context: item.context || "", category: item.category || "",
            queryImages: [...(item.query_images || [])], queryImageMeta: item.query_image_meta || [],
            productCount: item.product_count || (item.video3 || item.screenshot3 ? 3 : 2),
            evidenceMode: item.evidence_mode || (item.screenshot1 ? "long_screenshot" : "video_frames"),
            ...Object.fromEntries([1, 2, 3].flatMap(n => [
              [`video${n}Path`, item[`video${n}`] || ""], [`screenshot${n}Path`, item[`screenshot${n}`] || ""],
              [`answer${n}`, item[`answer${n}`] || ""], [`context${n}`, item[`context${n}`] || ""],
            ])), taskStartTime: item.task_start_time ?? null, taskEndTime: item.task_end_time ?? null,
            sourceData: item.source_data || null, sourceLine: item.source_line ?? null,
            sessionGroup: item.session_group ?? null, turnIndex: item.turn_index ?? null,
          }));
          opPage.value = 1;
        }
        if (d.mode === 'compare') {
          opItems.value = comparisonDraftRows(items.value);
          datasetSourceTaskId.value = d.task_id || id;
          datasetBaseline = draftFingerprint();datasetRevision.value++;
        }
        modalityFilter.value = "";
        results.value = d.results || [];
        refreshEvidence(results.value);
        itemProgress.value = receiveProgress(d.item_progress);
        restoreProgressEvents(d.progress_events);
        expandedProgressLogs.value = {};
        summary.value = d.summary || null;
        repairStatus.value = d.repair_status || "idle";
        executionControl.value = d.execution_control || {};
        selectedActiveRuns.value = d.active_runs || 0;
        resumeConcurrency.value = Number(executionControl.value.concurrency || d.options?.concurrency || 4);
        resumeFailed.value = false;
        const retryRuns = Object.values(d.retry_runs || {});
        activeRetry.value = retryRuns.sort(
          (a, b) => Number(b.created_at || 0) - Number(a.created_at || 0)
        )[0] || null;
        total.value = items.value.length || results.value.length;
        progress.value = results.value.length;
        selectedTaskStatus.value = d.status || "";
        running.value = ["pending", "queued", "running"].includes(selectedTaskStatus.value);
        queueNotice.value = selectedTaskStatus.value === "queued" ? "该任务正在等待前序任务完成。" : "";
        activeSkill.value = "";
        resultQuery.value = "";
        failedOnly.value = false;
        resultPage.value = 1;
        progressPage.value = 1;
        if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
        if (running.value || selectedActiveRuns.value || ["queued", "running"].includes(repairStatus.value)) connectSSE(taskId.value);
        nextTick(() => resultBrowser.value && resultBrowser.value.scrollIntoView({ behavior: "smooth", block: "start" }));
      } catch (error) {
        if (version === historyLoadVersion && error?.name !== "AbortError") {
          runError.value = "历史记录加载失败：" + (error?.message || "网络错误");
        }
      } finally {
        if (version === historyLoadVersion) {
          loadingTaskId.value = "";
          historyLoadController = null;
          if (!loaded && (running.value || ["queued", "running"].includes(repairStatus.value))) connectSSE(taskId.value);
        }
      }
    }

    function exportCsv() {
      if (loadingTaskId.value) return;
      window.open(`/api/eval/${taskId.value}/export?format=csv`);
    }
    function exportJson() {
      if (loadingTaskId.value) return;
      window.open(`/api/eval/${taskId.value}/export?format=json`);
    }
    async function exportXlsx(requestedId = "") {
      if (exportingTaskId.value || (!requestedId && loadingTaskId.value)) return;
      const id = requestedId || taskId.value;
      if (!id) return;
      exportingTaskId.value = id;
      exportError.value = "";
      exportDownloadUrl.value = "";
      exportMessage.value = `正在为任务 ${id} 准备 Excel…`;
      try {
        const response = await fetch(`/api/eval/${encodeURIComponent(id)}/exports`, {method: "POST"});
        let data = await response.json();
        if (!response.ok) throw new Error(data.detail || "导出请求失败");
        while (!disposed && ["queued", "generating"].includes(data.status)) {
          exportMessage.value = `任务 ${id}：${data.status === 'queued' ? '等待生成' : '正在生成 Excel'}…`;
          await new Promise(resolve => window.setTimeout(resolve, 1000));
          if (disposed) return;
          const status = await fetch(`/api/exports/${encodeURIComponent(data.export_id)}`);
          data = await status.json();
          if (!status.ok) throw new Error(data.detail || "无法读取导出状态");
        }
        if (disposed) return;
        if (data.status !== "ready") throw new Error(data.error || "生成 Excel 失败");
        exportDownloadUrl.value = `/api/exports/${encodeURIComponent(data.export_id)}/download`;
        exportMessage.value = `任务 ${id}：Excel 已生成。若未开始下载，请点击下方链接（30 分钟内有效）。`;
        // Use a same-origin download link: no popup and no full-file blob in memory.
        const link = document.createElement("a");
        link.href = exportDownloadUrl.value;
        link.download = data.filename || "";
        link.hidden = true;
        document.body.appendChild(link);
        try {
          link.click();
        } catch (error) {
          // A browser may block automatic downloads; keep the manual link available.
          console.warn("自动下载未能触发，请使用下载链接", error);
        } finally {
          link.remove();
        }
      } catch (error) {
        exportMessage.value = "";
        exportError.value = `任务 ${id} 导出失败：${error?.message || "网络错误"}。可再次点击导出重试。`;
      } finally {
        exportingTaskId.value = "";
      }
    }
    function exportFrames() {
      if (loadingTaskId.value) return;
      window.open(`/api/eval/${taskId.value}/export?format=frames_zip`);
    }
    function itemArtifactUrl(result, format) {
      const index = Number(result && result.index);
      if (!taskId.value || !Number.isInteger(index) || index < 0) return "";
      const item = items.value[index] || {};
      if (format === 'video' && (item.evidence_mode === 'long_screenshot' || item.screenshot1)) return "";
      return `/api/eval/${taskId.value}/items/${index}/export?format=${encodeURIComponent(format)}`;
    }

    onMounted(async () => {
      progressClockTimer = window.setInterval(() => {
        clockNow.value = Date.now();
      }, 1000);
      const r = await fetch("/api/config");
      const d = await r.json();
      judges.value = d.judges || [];
      judgeModelProfiles.value = d.judge_model_profiles || [];
      defaultJudgeModelProfile.value = d.default_judge_model_profile || judgeModelProfiles.value[0]?.id || "";
      selectedJudgeModelProfile.value = defaultJudgeModelProfile.value;
      mediaConcurrency.value = d.media_concurrency || 4;
      evaluationProfiles.value = d.evaluation_profiles || [];
      selectedEvaluationProfile.value = defaultEvaluationProfile();
      selectedJudges.value = defaultJudgeSelection();
      const selectedJudge = judges.value.find((j) => j.name === selectedJudges.value[0]);
      concurrency.value = selectedJudge?.recommended_concurrency || 4;
      if (selectedModelProfile.value) changeJudgeModel({ resetConcurrency: true });
      loadHistory();
      loadQueue();
      queueRefreshTimer = window.setInterval(loadQueue, 2000);
    });

    onUnmounted(() => {
      disposed = true;
      historyListVersion++;
      datasetImportVersion++;
      cancelHistoryLoad();
      if (progressClockTimer != null) window.clearInterval(progressClockTimer);
      if (queueRefreshTimer != null) window.clearInterval(queueRefreshTimer);
      closeActiveStream();
    });

    return {
      modes, mode, modeLabel, isVideoMode, items, errors, importReport, conversationPreflight, judges, visibleJudges, selectedJudges, datasetName,
      turnFilter, diagnosticFilter, conversationGroups, conversationHistory, liveInputDiagnostics,
      datasetSourceTaskId, datasetRevision, useComparisonDataset,
      evaluationProfiles, compareProfiles, selectedEvaluationProfile, evaluationProfileLabel,
      judgeModelProfiles, selectedJudgeModelProfile, selectedModelProfile, enableThinking, changeJudgeModel,
      taskJudgeRuntime, taskJudgeSummary, thinkingLabel, judgeRuntimeLabel,
      concurrency, mediaConcurrency, requestPacing, evalTimeout, submitting, running, progress, total, results, summary, taskId, runError,
      pacingStatus, pacingError, pacingNumber, pacingWaitLabel, pacingLimitLabel,
      queueState, queueEntries, selectedTaskStatus, queueNotice, taskStatusLabel, queueKindLabel,
      selectedTaskTiming, taskElapsedSeconds, formatTaskDuration,
      executionControl, controlSubmitting, resumeConcurrency, resumeFailed, isPausing, canPauseTask, canResumeTask, controlTask,
      repairStatus, retryStatusLabel, retrySubmitting, selectedRetryIndexes, activeRetry,
      failedResultIndexes, retryIndexSelected, toggleRetryIndex, retryFailedCases,
      itemProgress, progressEvents, expandedProgressLogs, pagedProgressRows, progressStages,
      historyItems, historyNoteDrafts, historyNoteEditing, loadingHistory, pageSize,
      historyPage, historyTotal, historyPageSize, historyPageCount, historyError,
      loadingTaskId, exportingTaskId, exportMessage, exportError, exportDownloadUrl,
      opPage, opPageSize, opPageCount, opJumpPage,
      progressPage, progressPageCount, progressJumpPage,
      resultJumpPage,
      resultBrowser,
      activeSkill, resultQuery, resultPage, resultPageSize,
      modalityFilter, modalityCounts, queryImageMetas, onQueryImage, setQueryImagePath,
      failedOnly, failedCaseCount, selectFailureFilter,
      evidenceRevisions,
      evidenceImages, evidenceImageErrors, evidenceExpanded, setEvidenceExpanded, setPageEvidenceExpanded,
      skillTabs, filteredResults, pagedResults, pageCount, resultTableWidth,
      formatHint, resultCols, opItems, pagedOpItems, opPreparing, canSubmit,
      switchMode, onOpManifestFile, submit, cell, columnWidth, exportCsv, exportJson, exportXlsx, exportFrames, itemArtifactUrl, addOpItem, removeOpItem, onOpVideo, onOpDrop,
      loadHistory, loadQueue, cancelQueuedTask, reprioritizeQueuedTask, loadHistoryTask, delHistory, editHistoryNote, cancelHistoryNote, saveHistoryNote, formatTime,
      selectSkill, resetResultPage, changePage,
      changeProgressPage, changeOpPage, changeResultPageSize, paginationPages, setTablePage, jumpTablePage,
      progressStageClass, progressDisplay, progressStageLabel, progressStatusClass,
      progressMeta, formatProgressEventTime, progressEventMeta, progressEventMessage, scrollProgressLog,
      formatProgressElapsed, timingEntries, timingStageLabel, shortRequestId, copyRequestId,
      cellTooltip, showCellTooltip, scheduleHideCellTooltip, keepCellTooltip, hideCellTooltip,
    };
  },
}).mount("#app");
