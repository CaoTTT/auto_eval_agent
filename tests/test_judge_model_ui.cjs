const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let app, mounted;
  const requests = [];
  const profiles = [
    {id: 'bailian_35', display: 'Qwen 3.5', model: 'qwen3.5-397b-a17b', supports_thinking: true,
      default_enable_thinking: true},
    {id: 'bailian_38', display: 'Qwen 3.8 Flash', model: 'qwen3.8-flash', supports_thinking: true,
      default_enable_thinking: false},
    {id: 'other', display: 'Other provider', model: 'another-model', supports_thinking: false,
      default_enable_thinking: null},
  ];
  const runtime = {version: 1, profile_id: 'bailian_38', judges: [
    {name: 'judge_2', model: 'qwen3.8-flash', enable_thinking: false},
  ]};
  let snapshot = {task_id: 'saved', mode: 'rich_content', status: 'paused',
    items: [], results: [], judge_runtime: runtime};
  const context = {
    ref: value => ({value}), computed: fn => ({get value() { return fn(); }}),
    onMounted(fn) { mounted = fn; }, onUnmounted() {}, nextTick() {}, console,
    document: {hidden: false}, window: {setInterval() {}, clearInterval() {}},
    alert(message) { throw Error(message); },
    createApp(options) { app = options.setup(); return {mount() {}}; },
    async fetch(url, options) {
      if (options?.method === 'POST') {
        requests.push({url, body: JSON.parse(options.body)});
        return {ok: false, json: async () => ({detail: 'test stop'})};
      }
      if (options?.method === 'PUT' && url === '/api/settings') {
        return {ok: true, json: async () => ({})};
      }
      if (url === '/api/config') return {ok: true, json: async () => ({
        judges: [{name: 'judge_2', display: '终端用户'}],
        judge_model_profiles: profiles, default_judge_model_profile: 'bailian_35',
      })};
      if (url === '/api/settings') return {ok: true, json: async () => ({
        concurrency: 5, max_in_flight: 40, waiting_capacity: 10,
        judges: ['judge_2'],
      })};
      if (url.startsWith('/api/history?')) return {ok: true, json: async () => ({items: []})};
      if (url.startsWith('/api/history/')) return {ok: true, json: async () => snapshot};
      if (url === '/api/queue') return {ok: true, json: async () => ({running: null, queued: []})};
      throw Error(`Unexpected request: ${url}`);
    },
  };
  const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');
  const read = file => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', file), 'utf8');
  vm.runInNewContext(strip(read('compare-cases.js')), context);
  vm.runInNewContext(strip(read('app.js')), context);
  await mounted();
  // 模型/思考选择（全局并发设置由 /api/settings 加载，不再有每任务并发）
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_35');
  assert.equal(app.enableThinking.value, true);
  assert.equal(app.selectedJudges.value.join(), 'judge_2');
  assert.equal(app.globalConcurrency.value, 5);
  assert.equal(app.maxInFlight.value, 40);

  for (const mode of ['rich_content', 'compare']) {
    app.mode.value = mode;
    app.opItems.value = [{query: 'Evaluate this', videoPath: 'sample.mp4', productCount: 2,
      screenshot1Path: 'a.png', screenshot2Path: 'b.png'}];
    for (const profile of profiles.slice(0, 2)) {
      app.selectedJudgeModelProfile.value = profile.id;
      app.changeJudgeModel();
      for (const thinking of [true, false]) {
        app.enableThinking.value = thinking;
        await app.submit();
        const {body} = requests.at(-1);
        assert.equal(body.mode, mode);
        assert.equal(body.options.judge_model_profile, profile.id);
        assert.equal(body.options.enable_thinking, thinking, 'false must be sent explicitly');
        assert.equal('concurrency' in body.options, false, '新任务不再提交每任务并发');
        assert.deepEqual(body.options.judges, ['judge_2']);
      }
    }
  }
  app.selectedJudgeModelProfile.value = 'other';
  app.changeJudgeModel();
  app.enableThinking.value = true;
  await app.submit();
  assert.equal(requests.at(-1).body.options.judge_model_profile, 'other');
  assert.equal('enable_thinking' in requests.at(-1).body.options, false, 'unsupported providers omit thinking');

  app.selectedJudgeModelProfile.value = 'removed-profile';
  const count = requests.length;
  await app.submit();
  assert.equal(requests.length, count, 'unknown model profile cannot be submitted');
  assert.match(app.runError.value, /请选择/);

  // 冻结展示：加载历史任务恢复其冻结的模型/思考；改下一个任务设置不改载入任务的标签
  await app.loadHistoryTask('saved');
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_38');
  assert.equal(app.enableThinking.value, false);
  assert.equal(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), 'qwen3.8-flash · 思考关闭');
  app.selectedJudgeModelProfile.value = 'bailian_35';
  app.changeJudgeModel();
  assert.equal(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), 'qwen3.8-flash · 思考关闭',
    'editing next task settings cannot relabel the loaded task');

  await app.retryFailedCases([0]);
  assert.deepEqual(requests.at(-1).body.options, {}, 'retry cannot inherit next-task model overrides');

  // legacy 记录
  snapshot = {...snapshot, task_id: 'legacy', judge_runtime: undefined};
  await app.loadHistoryTask('legacy');
  assert.match(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), /模型未记录.*思考未记录/);
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_35', 'new-task form restores its default for legacy data');
  assert.equal(app.thinkingLabel(undefined), '思考未记录');
  assert.equal(app.thinkingLabel(null), '思考未记录');
  assert.equal(app.thinkingLabel('false'), '思考未记录', 'untrusted strings are not interpreted as booleans');
  assert.equal(app.judgeRuntimeLabel({}, {judge_model: 'legacy-model', enable_thinking_label: '未记录'}),
    'legacy-model · 思考未记录', 'old records preserve known model without inventing thinking state');

  // index.html 结构：模型/思考仍在 ②，全局设置收进可折叠的“系统全局设置” + 保存按钮；不再有每任务并发
  const html = read('index.html');
  assert.match(html, /id="judge-model-profile"/);
  assert.match(html, /id="judge-enable-thinking"[^>]*role="switch"/);
  assert.match(html, /本任务实际配置/);
  assert.match(html, /系统全局设置/);
  assert.match(html, /saveGlobalSettings/);
  assert.match(html, /保存全局设置/);
  assert.doesNotMatch(html, /评估并发数/);
  assert.doesNotMatch(html, /恢复并发数/);
  assert.doesNotMatch(html, /task-endpoint/);
  console.log('Judge model UI: model/thinking, frozen display, legacy records, collapsed global settings with Save passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
