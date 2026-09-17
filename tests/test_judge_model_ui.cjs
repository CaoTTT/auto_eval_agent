const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let app, mounted;
  const requests = [];
  const profiles = [
    {id: 'bailian_35', display: 'Qwen 3.5', model: 'qwen3.5-397b-a17b', supports_thinking: true,
      default_enable_thinking: true, recommended_concurrency: 128, request_pacing: true},
    {id: 'bailian_38', display: 'Qwen 3.8 Flash', model: 'qwen3.8-flash', supports_thinking: true,
      default_enable_thinking: false, recommended_concurrency: 128, request_pacing: true},
    {id: 'other', display: 'Other provider', model: 'another-model', supports_thinking: false,
      default_enable_thinking: null, recommended_concurrency: 4, request_pacing: false},
  ];
  const runtime = {version: 1, profile_id: 'bailian_38', judges: [
    {name: 'judge_2', model: 'qwen3.8-flash', enable_thinking: false},
  ]};
  let snapshot = {task_id: 'saved', mode: 'rich_content', status: 'paused',
    items: [], results: [], judge_runtime: runtime};
  let queue = {};
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
      if (url === '/api/config') return {ok: true, json: async () => ({
        judges: [{name: 'judge_2', display: '终端用户', recommended_concurrency: 4}],
        judge_model_profiles: profiles, default_judge_model_profile: 'bailian_35',
      })};
      if (url === '/api/history?limit=50') return {ok: true, json: async () => ({items: []})};
      if (url.startsWith('/api/history/')) return {ok: true, json: async () => snapshot};
      if (url === '/api/queue') return {ok: true, json: async () => queue};
      if (url === '/api/request-pacing') return {ok: true, json: async () => ({
        enabled: true, active: true,
        controller: {model: profiles[0].model, requests_last_second: 1},
        controllers: [{model: profiles[0].model, requests_last_second: 1},
          {model: profiles[1].model, requests_last_second: 5}],
      })};
      throw Error(`Unexpected request: ${url}`);
    },
  };
  const strip = code => code.replace(/^import[^\n]*\n/gm, '').replace(/^export /gm, '');
  const read = file => fs.readFileSync(path.join(__dirname, '../src/auto_eval/web/static', file), 'utf8');
  vm.runInNewContext(strip(read('compare-cases.js')), context);
  vm.runInNewContext(strip(read('app.js')), context);
  await mounted();
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_35');
  assert.equal(app.enableThinking.value, true);
  assert.equal(app.concurrency.value, 128);
  assert.equal(app.selectedJudges.value.join(), 'judge_2');
  app.concurrency.value = 16;
  app.selectedJudgeModelProfile.value = 'bailian_38';
  app.changeJudgeModel({type: 'change'});
  assert.equal(app.concurrency.value, 16, 'model changes preserve manually chosen concurrency');
  app.selectedJudgeModelProfile.value = 'bailian_35';
  app.changeJudgeModel();
  assert.equal(app.concurrency.value, 16, 'switching back does not reset concurrency');

  for (const mode of ['rich_content', 'compare']) {
    app.mode.value = mode;
    app.opItems.value = [{query: 'Evaluate this', videoPath: 'sample.mp4', productCount: 2,
      screenshot1Path: 'a.png', screenshot2Path: 'b.png'}];
    for (const profile of profiles.slice(0, 2)) {
      app.selectedJudgeModelProfile.value = profile.id;
      app.changeJudgeModel();
      for (const concurrency of [1, 16, 128]) {
        app.concurrency.value = concurrency;
        for (const thinking of [true, false]) {
          app.enableThinking.value = thinking;
          assert.equal(app.concurrency.value, concurrency, 'thinking changes preserve manual concurrency');
          await app.submit();
          const {body} = requests.at(-1);
          assert.equal(body.mode, mode);
          assert.equal(body.options.judge_model_profile, profile.id);
          assert.equal(body.options.enable_thinking, thinking, 'false must be sent explicitly');
          assert.equal(body.options.concurrency, concurrency, 'new tasks submit the chosen concurrency');
          assert.deepEqual(body.options.judges, ['judge_2']);
        }
      }
    }
  }
  for (const invalid of [0, 129, 1.5, '', NaN]) {
    app.concurrency.value = invalid;
    const before = requests.length;
    await app.submit();
    assert.equal(requests.length, before, 'invalid concurrency cannot be submitted');
    assert.match(app.runError.value, /评估并发数必须为 1–128 的整数/);
  }
  app.concurrency.value = 128;
  app.selectedJudgeModelProfile.value = 'other';
  app.changeJudgeModel();
  assert.equal(app.concurrency.value, 4, 'lower model capacity caps the previous concurrency');
  app.enableThinking.value = true;
  await app.submit();
  assert.equal(requests.at(-1).body.options.judge_model_profile, 'other');
  assert.equal('enable_thinking' in requests.at(-1).body.options, false, 'unsupported providers omit thinking');
  assert.equal(app.requestPacing.value, false);

  app.selectedJudgeModelProfile.value = 'removed-profile';
  const count = requests.length;
  await app.submit();
  assert.equal(requests.length, count, 'unknown model profile cannot be submitted');
  assert.match(app.runError.value, /请选择/);

  await app.loadHistoryTask('saved');
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_38');
  assert.equal(app.enableThinking.value, false);
  assert.equal(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), 'qwen3.8-flash · 思考关闭');
  app.selectedJudgeModelProfile.value = 'bailian_35';
  app.changeJudgeModel();
  assert.equal(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), 'qwen3.8-flash · 思考关闭',
    'editing next task settings cannot relabel the loaded task');
  assert.equal(app.concurrency.value, 4, 'moving back to a higher model capacity preserves the smaller value');
  app.concurrency.value = 11;
  app.resumeConcurrency.value = 67;
  await app.controlTask('resume');
  assert.equal(requests.at(-1).body.concurrency, 67, 'resume uses its own concurrency rather than the new-task setting');
  assert.equal('judge_model_profile' in requests.at(-1).body, false);
  assert.equal('enable_thinking' in requests.at(-1).body, false);
  await app.retryFailedCases([0]);
  assert.deepEqual(requests.at(-1).body.options, {}, 'retry cannot inherit next-task model overrides');

  queue = {running: {task_id: 'another-running-task', status: 'running', judge_runtime: runtime}};
  await app.loadQueue();
  assert.equal(app.pacingStatus.value.model, 'qwen3.8-flash', 'monitor follows the running task, not the model form');
  assert.equal(app.pacingStatus.value.requests_last_second, 5);
  queue = {running: {task_id: 'legacy-running-task', status: 'running'}};
  await app.loadQueue();
  assert.equal(app.pacingStatus.value, null, 'unknown running model must not borrow another task’s controller');

  snapshot = {...snapshot, task_id: 'legacy', judge_runtime: undefined};
  await app.loadHistoryTask('legacy');
  assert.match(app.judgeRuntimeLabel(app.taskJudgeRuntime.value), /模型未记录.*思考未记录/);
  assert.equal(app.selectedJudgeModelProfile.value, 'bailian_35', 'new-task form restores its default for legacy data');
  assert.equal(app.thinkingLabel(undefined), '思考未记录');
  assert.equal(app.thinkingLabel(null), '思考未记录');
  assert.equal(app.thinkingLabel('false'), '思考未记录', 'untrusted strings are not interpreted as booleans');
  assert.equal(app.judgeRuntimeLabel({}, {judge_model: 'legacy-model', enable_thinking_label: '未记录'}),
    'legacy-model · 思考未记录', 'old records preserve known model without inventing thinking state');

  const html = read('index.html');
  assert.match(html, /id="judge-model-profile"/);
  assert.match(html, /id="judge-enable-thinking"[^>]*role="switch"/);
  assert.match(html, /本任务实际配置/);
  assert.match(html, /评估并发数/);
  assert.match(html, /运行中调整请先暂停，再修改“恢复并发数”继续/);
  assert.doesNotMatch(html, /任意连续 1 秒最多启动 9 次请求/);
  console.log('Judge model UI: model/thinking combinations, manual concurrency, resume concurrency, capabilities, frozen display, legacy records and per-model pacing passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
