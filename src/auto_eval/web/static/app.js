import { createApp, ref, computed, onMounted, onUnmounted, nextTick } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

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
    const items = ref([]);
    let opItemSequence = 0;
    const opItems = ref([newOpItem()]);
    const opPage = ref(1);
    const opJumpPage = ref("");
    const opPreparing = ref(false);
    const errors = ref([]);
    const judges = ref([]);
    const selectedJudges = ref([]);
    const visibleJudges = computed(() => judges.value);
    const evaluationProfiles = ref([]);
    const selectedEvaluationProfile = ref("");
    const compareProfiles = computed(() =>
      evaluationProfiles.value.filter((profile) => (profile.modes || []).includes("compare"))
    );
    const concurrency = ref(4);
    const evalTimeout = ref(300);
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
    const resultBrowser = ref(null);
    const activeSkill = ref("");
    const resultQuery = ref("");
    const resultPage = ref(1);
    const resultPageSize = ref(10);
    const progressPage = ref(1);
    const resultJumpPage = ref("");
    const progressJumpPage = ref("");
    const cellTooltip = ref({ visible: false, text: "", style: {} });
    const historyItems = ref([]);
    const historyNoteDrafts = ref({});
    const historyNoteEditing = ref({});
    const loadingHistory = ref(false);
    const queueState = ref({ running: null, queued: [] });
    const selectedTaskStatus = ref("");
    const queueNotice = ref("");
    const repairStatus = ref("idle");
    const retrySubmitting = ref(false);
    const selectedRetryIndexes = ref([]);
    const activeRetry = ref(null);
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

    function retryStatusLabel(status) {
      return ({ idle: "", queued: "补跑排队中", running: "补跑中", completed: "补跑完成", partial: "补跑后仍有失败", error: "补跑异常", cancelled: "补跑已取消" })[status] || status;
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
      return ({ queued: "排队中", running: "运行中", done: "已完成", error: "失败", cancelled: "已取消" })[status] || status;
    }

    function queueKindLabel(kind) {
      return kind === "retry" ? "失败补跑" : "全量评测";
    }

    const formatHint = computed(
      () =>
        ({
          compare: "逐题导入 JSONL：product_count可为2或3；双产品填写video1/2，三产品再填写video3；context1/2/3、answer1/2/3可选。",
          rich_content: "可逐题上传，也可导入 JSONL：query、context(可选)、video_path、category/answer_text/task_start_time/task_end_time(均可选)；普通图片不算挂卡，回答区域蓝色文字按 Superlink 统计。",
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

    const progressRows = computed(() =>
      items.value.map((item, index) => {
        const current = itemProgress.value[index] || {};
        const result = results.value.find((entry) => entry.index === index);
        const events = progressEvents.value[index] || [];
        const startedAt = Number(current.started_at || 0);
        const finishedAt = Number(current.finished_at || 0);
        const resultElapsed = Number(result?.latency_s);
        const elapsedSeconds = Number.isFinite(resultElapsed)
          ? resultElapsed
          : startedAt > 0
            ? Math.max(0, ((finishedAt || clockNow.value) - startedAt) / 1000)
            : null;
        return {
          index,
          itemId: item.id || `q${index}`,
          query: item.query || item.question || "",
          percent: current.percent ?? 0,
          status: current.status || "pending",
          message: current.message || "排队中",
          requestId: current.request_id || "",
          module: current.module || "",
          judge: current.judge || "",
          round: Number(current.round || 0),
          stageRank: current.stage_rank ?? progressStageRank(current),
          elapsedSeconds,
          events,
          latestEvents: events.slice(-2),
        };
      })
    );
    const progressPageCount = computed(() => Math.max(1, Math.ceil(progressRows.value.length / pageSize)));
    const pagedProgressRows = computed(() => {
      const page = Math.min(progressPage.value, progressPageCount.value);
      const start = (page - 1) * pageSize;
      return progressRows.value.slice(start, start + pageSize);
    });

    function progressStageRank(progressItem) {
      if (progressItem.status === "done") return 4;
      if (progressItem.module === "结果聚合") return 3;
      if (["模型裁判", "被测模型", "单题评测"].includes(progressItem.module)) return 2;
      if (progressItem.module === "垂域分类") return 1;
      return 0;
    }

    function mergeItemProgress(incoming) {
      appendProgressEvent(incoming);
      const index = incoming.item_index;
      const previous = itemProgress.value[index] || {};
      const previousRank = previous.stage_rank ?? progressStageRank(previous);
      const incomingRank = progressStageRank(incoming);
      const terminal = incoming.status === "done" || incoming.status === "error";
      const updatedAt = Date.parse(incoming.updated_at || "");
      itemProgress.value = {
        ...itemProgress.value,
        [index]: {
          ...previous,
          ...incoming,
          // Agent Loop 总轮数未知，宏观阶段只前进、不倒退。
          stage_rank: incoming.status === "done"
            ? 4
            : Math.max(previousRank, incomingRank),
          finished_at: terminal
            ? (previous.finished_at || (Number.isFinite(updatedAt) ? updatedAt : Date.now()))
            : previous.finished_at,
        },
      };
    }

    function appendProgressEvent(incoming) {
      const index = incoming.item_index;
      if (index == null) return;
      const previous = progressEvents.value[index] || [];
      const eventKey = incoming.sequence != null
        ? `seq:${incoming.sequence}`
        : [
            incoming.updated_at, incoming.module, incoming.event,
            incoming.judge, incoming.round, incoming.message,
          ].join("|");
      if (previous.some((entry) => entry._key === eventKey)) return;
      progressEvents.value = {
        ...progressEvents.value,
        [index]: [...previous, { ...incoming, _key: eventKey }].slice(-100),
      };
    }

    function progressStageClass(row, stageIndex) {
      if (row.status === "done") return "completed";
      if (stageIndex < row.stageRank) return "completed";
      if (stageIndex === row.stageRank) return row.status === "error" ? "error" : "active";
      return "pending";
    }

    function progressDisplay(row) {
      const message = row.message || "排队中";
      const parts = [];
      if (row.judge && !message.includes(row.judge)) parts.push(row.judge);
      const roundLabel = row.round > 0 ? `第${row.round}轮` : "";
      if (roundLabel && !message.includes(roundLabel)) parts.push(roundLabel);
      parts.push(message);
      return parts.join(" · ");
    }

    function progressStageLabel(row) {
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

    function formatProgressEventTime(value) {
      const date = new Date(value || "");
      if (Number.isNaN(date.getTime())) return "--:--:--";
      return date.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
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

    function scrollProgressLog(event) {
      if (!event.currentTarget.open) return;
      nextTick(() => {
        const panel = event.currentTarget.querySelector(".progress-log-scroll");
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
          { key: "latency_s", label: "耗时" },
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
        { key: "latency_s", label: "耗时" },
      ];
    });

    function columnWidth(c) {
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
        if (q && !`${r.item_id || ""} ${r.query || ""} ${r.context || ""} ${r.answer_text || ""} ${r.answer1 || ""} ${r.answer2 || ""} ${r.answer3 || ""} ${(r.card_contents || []).join(" ")} ${(r.superlink_texts || []).join(" ")} ${r.rationale || ""}`.toLowerCase().includes(q)) return false;
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
      return judges.value.length ? [judges.value[0].name] : [];
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
      return { _uiKey: ++opItemSequence, id: "", query: "", context: "", category: "", productCount: 2, videoName: "", videoPath: "", video1Path: "", video2Path: "", video3Path: "", frames: [], frameCount: 0, duration: 0, answer: "", answer1: "", answer2: "", answer3: "", context1: "", context2: "", context3: "", taskStartTime: null, taskEndTime: null, sourceLine: null, sourceData: null, sessionGroup: null, turnIndex: null, uploading: false, uploadError: "" };
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
      datasetName.value = file.name || "";
      opPreparing.value = true;
      errors.value = [];
      items.value = [];
      opItems.value = [newOpItem()];
      opPage.value = 1;
      opJumpPage.value = "";
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
        console.log("[onOpManifestFile] response ok:", parseResponse.ok, "items:", (parsed.items || []).length, "errors:", (parsed.errors || []).length);
        if (!parseResponse.ok) throw new Error(parsed.detail || (isCsv ? "CSV 解析请求失败" : "JSONL 解析请求失败"));
        const importErrors = [...(parsed.errors || [])];
        if (!(parsed.items || []).length) {
          errors.value = importErrors.length ? importErrors : ["JSONL 中没有可导入的数据"];
          console.warn("[onOpManifestFile] no items parsed");
          return;
        }

        errors.value = importErrors;
        const imported = parsed.items || [];
        if (imported.length) {
          items.value = imported;
          opItems.value = imported.map((item) => ({
            ...newOpItem(),
            id: item.id || "",
            query: item.query || "",
            context: item.context || "",
            category: item.category === "default" ? "" : (item.category || ""),
            videoName: String(item.video_path || "").split(/[\\/]/).pop(),
            videoPath: item.video_path || item.video1 || "",
            productCount: item.product_count || (item.video3 ? 3 : 2),
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
          console.log("[onOpManifestFile] opItems mapped:", opItems.value.length, "first videoPath:", opItems.value[0]?.videoPath, "first query:", opItems.value[0]?.query);
        }
      } catch (error) {
        console.error("[onOpManifestFile] error:", error);
        errors.value = ["批量导入失败：" + (error?.message || String(error))];
      } finally {
        opPreparing.value = false;
      }
    }

    function opItemReady(it) {
      if (!it.query.trim()) return false;
      if (mode.value !== "compare") return Boolean((it.frames || []).length || it.videoPath);
      const productCount = Number(it.productCount) === 3 || it.video3Path ? 3 : 2;
      return Boolean(
        (it.video1Path || it.videoPath)
        && it.video2Path
        && (productCount === 2 || it.video3Path)
      );
    }

    const canSubmit = computed(() =>
      !opPreparing.value && opItems.value.some(opItemReady)
    );

    async function submit() {
      if (submitting.value) return;
      runError.value = "";
      const valid = opItems.value.filter(opItemReady);
      if (!valid.length) {
        alert("请为每题填写 query，并提供视频路径或上传视频后再评估。");
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
          const productCount = Number(it.productCount) === 3 || it.video3Path ? 3 : 2;
          item.product_count = productCount;
          item.video1 = it.video1Path || it.videoPath || "";
          item.video2 = it.video2Path || "";
          item.context1 = (it.context1 || "").trim();
          item.context2 = (it.context2 || "").trim();
          item.answer1 = (it.answer1 || it.answer || "").trim();
          item.answer2 = (it.answer2 || "").trim();
          if (productCount === 3) {
            item.video3 = it.video3Path || "";
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
        return item;
      });
      const body = {
        mode: mode.value,
        items: submittedItems,
        dataset_name: datasetName.value || "手动录入",
        evaluation_profile: mode.value === "compare" ? selectedEvaluationProfile.value : null,
        options: {
          judges: selectedJudges.value,
          concurrency: concurrency.value,
          eval_timeout_s: evalTimeout.value,
        },
      };
      let r;
      submitting.value = true;
      try {
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
      closeActiveStream();
      items.value = submittedItems;
      errors.value = [];
      results.value = [];
      summary.value = null;
      progressEvents.value = {};
      activeSkill.value = "";
      resultQuery.value = "";
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
      if (!taskId.value || retrySubmitting.value) return;
      const selected = indexes == null ? null : [...new Set(indexes.map(Number))];
      if (selected && !selected.length) return;
      retrySubmitting.value = true;
      runError.value = "";
      const idempotencyKey = globalThis.crypto?.randomUUID?.()
        || `retry-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let response;
      try {
        response = await fetch(`/api/eval/${encodeURIComponent(taskId.value)}/retries`, {
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
        runError.value = "无法提交失败补跑：" + (error?.message || "网络错误");
        return;
      }
      const data = await response.json().catch(() => ({}));
      retrySubmitting.value = false;
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

    async function reconcileTaskAfterError(message) {
      let snapshot = null;
      try {
        const response = await fetch(`/api/history/${taskId.value}`);
        if (response.ok) snapshot = await response.json();
      } catch (_) {}
      const snapshotResults = snapshot?.results || results.value;
      const resultByIndex = new Map(snapshotResults.map((entry) => [entry.index, entry]));
      const snapshotProgress = snapshot?.item_progress || {};
      progressEvents.value = snapshot?.progress_events || progressEvents.value;
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
      progress.value = snapshotResults.length;
      itemProgress.value = reconciled;
      if (snapshot?.summary) summary.value = snapshot.summary;
    }

    function closeActiveStream() {
      if (activeEventSource) activeEventSource.close();
      activeEventSource = null;
    }

    function connectSSE(streamTaskId = taskId.value) {
      closeActiveStream();
      const es = new EventSource(`/api/eval/${streamTaskId}/stream`);
      activeEventSource = es;
      const isSelected = () => taskId.value === streamTaskId;
      es.addEventListener("start", () => {
        if (!isSelected()) return;
        selectedTaskStatus.value = "running";
        queueNotice.value = "";
        running.value = true;
        loadQueue();
      });
      es.addEventListener("item_progress", (e) => {
        if (!isSelected()) return;
        const d = JSON.parse(e.data);
        mergeItemProgress(d);
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
          itemProgress.value = {
            ...itemProgress.value,
            [index]: {
              ...previous,
              status: d.result.error ? "error" : "done",
              percent: 100,
              message: d.result.error ? "评测失败" : "评测完成",
              stage_rank: d.result.error ? (previous.stage_rank ?? 0) : 4,
              finished_at: Date.now(),
            },
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
        itemProgress.value = {
          ...itemProgress.value,
          [index]: {
            ...previous,
            status,
            percent: 100,
            message: data.status === "succeeded" ? "补跑成功" : (data.status === "failed" ? "补跑仍失败" : "补跑已跳过"),
            finished_at: Date.now(),
          },
        };
      });
      es.addEventListener("done", (e) => {
        if (!isSelected()) return;
        const doneData = JSON.parse(e.data);
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
          message = d.message || message;
        } catch (_) {}
        running.value = false;
        selectedTaskStatus.value = "error";
        queueNotice.value = "";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        await reconcileTaskAfterError(message);
        runError.value = "评估出错：" + message;
        loadQueue();
        loadHistory();
      });
      es.addEventListener("cancelled", (e) => {
        if (!isSelected()) return;
        let message = "排队任务已取消";
        try {
          message = JSON.parse(e.data).message || message;
        } catch (_) {}
        running.value = false;
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
        queueNotice.value = "失败补跑已取消";
        es.close();
        if (activeEventSource === es) activeEventSource = null;
        loadQueue();
        loadHistory();
      });
    }

    function cell(r, c) {
      const v = r[c.key];
      if (c.key === "category") return r.category_display || (!v || v === "default" ? "通用" : v);
      if (c.key === "latency_s") return v != null ? v + "秒" : "";
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

    async function loadHistory() {
      loadingHistory.value = true;
      try {
        const r = await fetch("/api/history?limit=50");
        const d = await r.json();
        historyItems.value = d.items || [];
        historyNoteDrafts.value = Object.fromEntries(
          historyItems.value.map((item) => [item.task_id, item.note || ""]),
        );
        historyNoteEditing.value = {};
      } finally {
        loadingHistory.value = false;
      }
    }

    async function loadQueue() {
      try {
        const response = await fetch("/api/queue");
        if (!response.ok) return;
        const data = await response.json();
        queueState.value = {
          running: data.running || null,
          queued: data.queued || [],
        };
      } catch (_) {}
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
        results.value = [];
        summary.value = null;
      }
      await loadHistory();
    }

    async function loadHistoryTask(id) {
      const r = await fetch(`/api/history/${id}`);
      if (!r.ok) {
        alert("历史记录加载失败");
        return;
      }
      const d = await r.json();
      if (!modes.some((m) => m.key === d.mode)) {
        alert("该历史记录使用已下线的评测模式，无法加载。");
        return;
      }
      closeActiveStream();
      taskId.value = d.task_id || id;
      mode.value = d.mode;
      if (d.mode === "compare") {
        selectedEvaluationProfile.value = d.evaluation_profile || defaultEvaluationProfile();
      }
      datasetName.value = d.dataset_name || "";
      items.value = d.items || [];
      results.value = d.results || [];
      itemProgress.value = d.item_progress || {};
      progressEvents.value = d.progress_events || {};
      summary.value = d.summary || null;
      repairStatus.value = d.repair_status || "idle";
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
      resultPage.value = 1;
      progressPage.value = 1;
      if (mode.value !== "compare" && skillTabs.value.length) activeSkill.value = skillTabs.value[0].key;
      if (running.value || ["queued", "running"].includes(repairStatus.value)) connectSSE(taskId.value);
      nextTick(() => resultBrowser.value && resultBrowser.value.scrollIntoView({ behavior: "smooth", block: "start" }));
    }

    function exportCsv() {
      window.open(`/api/eval/${taskId.value}/export?format=csv`);
    }
    function exportJson() {
      window.open(`/api/eval/${taskId.value}/export?format=json`);
    }
    function exportXlsx() {
      window.open(`/api/eval/${taskId.value}/export?format=xlsx`);
    }
    function exportFrames() {
      window.open(`/api/eval/${taskId.value}/export?format=frames_zip`);
    }
    function itemArtifactUrl(result, format) {
      const index = Number(result && result.index);
      if (!taskId.value || !Number.isInteger(index) || index < 0) return "";
      return `/api/eval/${taskId.value}/items/${index}/export?format=${encodeURIComponent(format)}`;
    }

    onMounted(async () => {
      progressClockTimer = window.setInterval(() => {
        clockNow.value = Date.now();
      }, 1000);
      const r = await fetch("/api/config");
      const d = await r.json();
      judges.value = d.judges || [];
      evaluationProfiles.value = d.evaluation_profiles || [];
      selectedEvaluationProfile.value = defaultEvaluationProfile();
      selectedJudges.value = defaultJudgeSelection();
      loadHistory();
      loadQueue();
      queueRefreshTimer = window.setInterval(loadQueue, 2000);
    });

    onUnmounted(() => {
      if (progressClockTimer != null) window.clearInterval(progressClockTimer);
      if (queueRefreshTimer != null) window.clearInterval(queueRefreshTimer);
      closeActiveStream();
    });

    return {
      modes, mode, modeLabel, isVideoMode, items, errors, judges, visibleJudges, selectedJudges, datasetName,
      evaluationProfiles, compareProfiles, selectedEvaluationProfile, evaluationProfileLabel,
      concurrency, evalTimeout, submitting, running, progress, total, results, summary, taskId, runError,
      queueState, queueEntries, selectedTaskStatus, queueNotice, taskStatusLabel, queueKindLabel,
      repairStatus, retryStatusLabel, retrySubmitting, selectedRetryIndexes, activeRetry,
      failedResultIndexes, retryIndexSelected, toggleRetryIndex, retryFailedCases,
      itemProgress, progressEvents, progressRows, pagedProgressRows, progressStages,
      historyItems, historyNoteDrafts, historyNoteEditing, loadingHistory, pageSize,
      opPage, opPageSize, opPageCount, opJumpPage,
      progressPage, progressPageCount, progressJumpPage,
      resultJumpPage,
      resultBrowser,
      activeSkill, resultQuery, resultPage, resultPageSize,
      skillTabs, filteredResults, pagedResults, pageCount, resultTableWidth,
      formatHint, resultCols, opItems, pagedOpItems, opPreparing, canSubmit,
      switchMode, onOpManifestFile, submit, cell, columnWidth, exportCsv, exportJson, exportXlsx, exportFrames, itemArtifactUrl, addOpItem, removeOpItem, onOpVideo, onOpDrop,
      loadHistory, loadQueue, cancelQueuedTask, reprioritizeQueuedTask, loadHistoryTask, delHistory, editHistoryNote, cancelHistoryNote, saveHistoryNote, formatTime,
      selectSkill, resetResultPage, changePage,
      changeProgressPage, changeOpPage, changeResultPageSize, paginationPages, setTablePage, jumpTablePage,
      progressStageClass, progressDisplay, progressStageLabel, progressStatusClass,
      progressMeta, formatProgressEventTime, progressEventMeta, progressEventMessage, scrollProgressLog,
      formatProgressElapsed, shortRequestId, copyRequestId,
      cellTooltip, showCellTooltip, scheduleHideCellTooltip, keepCellTooltip, hideCellTooltip,
    };
  },
}).mount("#app");
